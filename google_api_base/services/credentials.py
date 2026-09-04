import dataclasses
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from .errors import GoogleApiPausedError

_ENVIRONMENT_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_ROOT_ENVIRONMENT_KEY = "ODOO_GOOGLE_API_SECRET_DIR"
_MAX_VALUE_BYTES = 64 * 1024
_SERVICE_ACCOUNT_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}@[A-Za-z0-9.-]+\.gserviceaccount\.com$"
)
_SERVICE_ACCOUNT_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,62}$")
_SERVICE_ACCOUNT_KEY_ID_RE = re.compile(r"^[A-Fa-f0-9]{16,128}$")
_SERVICE_ACCOUNT_CLIENT_ID_RE = re.compile(r"^[0-9]{6,64}$")
_SERVICE_ACCOUNT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SERVICE_ACCOUNT_KEYS = frozenset(
    {
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
        "auth_uri",
        "token_uri",
        "auth_provider_x509_cert_url",
        "client_x509_cert_url",
        "universe_domain",
    }
)


class GoogleCredentialResolutionError(GoogleApiPausedError):
    """Safely reportable failure while resolving an external credential."""


@dataclasses.dataclass(frozen=True)
class GoogleRuntimeIdentity:
    """Immutable in-memory identity consumed by the REST facade."""

    active: bool
    api_version: str
    developer_token: str = dataclasses.field(repr=False, compare=False)
    login_customer_id: str
    public_ref: str
    revision: int
    company_id: int
    auth_mode: str = "authorized_user"
    oauth_client_id: str = dataclasses.field(default="", repr=False, compare=False)
    oauth_client_secret: str = dataclasses.field(default="", repr=False, compare=False)
    refresh_token: str = dataclasses.field(default="", repr=False, compare=False)
    service_account_info: Mapping = dataclasses.field(
        default_factory=lambda: MappingProxyType({}), repr=False, compare=False
    )

    def ensure_one(self):
        """Mirror the singleton contract exposed by an Odoo record."""

        return self

    def __reduce__(self):
        raise TypeError("Google runtime identity is not serializable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("Google runtime identity is not serializable")


def validate_credential_reference(backend, reference):
    """Validate one opaque reference without resolving its value."""

    if not isinstance(reference, str):
        raise GoogleCredentialResolutionError("Google credential reference is invalid")
    pattern = {
        "environment": _ENVIRONMENT_REF_RE,
        "file": _FILE_REF_RE,
    }.get(backend)
    if pattern is None:
        raise GoogleCredentialResolutionError(
            "Google credential backend is unsupported"
        )
    if not pattern.fullmatch(reference):
        raise GoogleCredentialResolutionError("Google credential reference is invalid")
    return reference


def _validated_value(value):
    if not isinstance(value, str):
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    value = value.strip()
    encoded = None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = None
    if encoded is None:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    if len(value) < 8 or len(encoded) > _MAX_VALUE_BYTES:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    return value


def _secret_root():
    value = os.environ.get(_SECRET_ROOT_ENVIRONMENT_KEY, "")
    if not value:
        raise GoogleCredentialResolutionError(
            "Google credential backend is unavailable"
        )
    root = None
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        root = None
    if root is None:
        raise GoogleCredentialResolutionError(
            "Google credential backend is unavailable"
        )
    if not root.is_dir():
        raise GoogleCredentialResolutionError(
            "Google credential backend is unavailable"
        )
    return root


def _open_credential_file(root, reference):
    target = root.joinpath(validate_credential_reference("file", reference))
    descriptor = None
    try:
        if target.is_symlink():
            raise GoogleCredentialResolutionError("Google credential file is unsafe")
        resolved = target.resolve(strict=True)
        if resolved.parent != root:
            raise GoogleCredentialResolutionError("Google credential file is unsafe")
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except GoogleCredentialResolutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        descriptor = None
    if descriptor is None:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    return descriptor


def _read_private_regular_file(descriptor):
    raw = None
    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise GoogleCredentialResolutionError(
                    "Google credential file is unsafe"
                )
            if metadata.st_size <= 0 or metadata.st_size > _MAX_VALUE_BYTES:
                raise GoogleCredentialResolutionError(
                    "Google credential is unavailable"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(_MAX_VALUE_BYTES + 1)
        except GoogleCredentialResolutionError:
            raise
        except OSError:
            raw = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if raw is None:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    value = None
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        value = None
    if value is None:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    return _validated_value(value)


def _read_private_regular_file_text(descriptor):
    """Read one private UTF-8 file without applying scalar-secret rules."""

    raw = None
    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise GoogleCredentialResolutionError(
                    "Google credential file is unsafe"
                )
            if metadata.st_size <= 0 or metadata.st_size > _MAX_VALUE_BYTES:
                raise GoogleCredentialResolutionError(
                    "Google credential is unavailable"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(_MAX_VALUE_BYTES + 1)
        except GoogleCredentialResolutionError:
            raise
        except OSError:
            raw = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if raw is None or len(raw) > _MAX_VALUE_BYTES:
        raise GoogleCredentialResolutionError("Google credential is unavailable")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GoogleCredentialResolutionError(
            "Google credential is unavailable"
        ) from None


def resolve_credential(backend, reference):
    """Resolve one value while keeping it out of errors and logs."""

    if backend == "environment":
        reference = validate_credential_reference(backend, reference)
        return _validated_value(os.environ.get(reference))
    if backend == "file":
        descriptor = _open_credential_file(_secret_root(), reference)
        return _read_private_regular_file(descriptor)
    raise GoogleCredentialResolutionError("Google credential backend is unsupported")


def _service_account_info(value):
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        raise GoogleCredentialResolutionError(
            "Google service account credential is unavailable"
        ) from None
    if not isinstance(payload, dict) or set(payload) - _SERVICE_ACCOUNT_KEYS:
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    required = {
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
        "token_uri",
    }
    if not required.issubset(payload):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if payload.get("type") != "service_account":
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if not _SERVICE_ACCOUNT_PROJECT_RE.fullmatch(str(payload.get("project_id") or "")):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if not _SERVICE_ACCOUNT_KEY_ID_RE.fullmatch(
        str(payload.get("private_key_id") or "")
    ):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if not _SERVICE_ACCOUNT_EMAIL_RE.fullmatch(str(payload.get("client_email") or "")):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if not _SERVICE_ACCOUNT_CLIENT_ID_RE.fullmatch(str(payload.get("client_id") or "")):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if payload.get("token_uri") != _SERVICE_ACCOUNT_TOKEN_URI:
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    private_key = payload.get("private_key")
    try:
        private_key_bytes = private_key.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        private_key_bytes = b""
    if (
        not isinstance(private_key, str)
        or not private_key.startswith("-----BEGIN PRIVATE KEY-----\n")
        or not private_key.endswith("\n-----END PRIVATE KEY-----\n")
        or not 256 <= len(private_key_bytes) <= 32 * 1024
    ):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    if payload.get("universe_domain") not in (None, "", "googleapis.com"):
        raise GoogleCredentialResolutionError(
            "Google service account credential is invalid"
        )
    return MappingProxyType(dict(payload))


def resolve_service_account_info(backend, reference):
    """Resolve and validate one service-account JSON only in process memory."""

    reference = validate_credential_reference(backend, reference)
    if backend == "environment":
        value = os.environ.get(reference)
        try:
            encoded = value.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            encoded = b""
        if not value or not encoded or len(encoded) > _MAX_VALUE_BYTES:
            raise GoogleCredentialResolutionError(
                "Google service account credential is unavailable"
            )
    elif backend == "file":
        descriptor = _open_credential_file(_secret_root(), reference)
        value = _read_private_regular_file_text(descriptor)
    else:
        raise GoogleCredentialResolutionError(
            "Google credential backend is unsupported"
        )
    return _service_account_info(value)
