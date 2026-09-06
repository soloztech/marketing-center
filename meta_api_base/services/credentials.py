import dataclasses
import os
import re
import stat
from pathlib import Path

from .errors import MetaApiPausedError

_ENVIRONMENT_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_ROOT_ENVIRONMENT_KEY = "ODOO_META_API_SECRET_DIR"
_MAX_SECRET_BYTES = 64 * 1024


class MetaCredentialResolutionError(MetaApiPausedError):
    """Safely reportable failure while resolving an external secret."""


@dataclasses.dataclass(frozen=True)
class MetaRuntimeApp:
    """Immutable, in-memory App contract consumed by the Graph helpers."""

    active: bool
    external_app_id: str
    graph_version: str
    app_secret: str = dataclasses.field(repr=False, compare=False)
    public_ref: str
    revision: int
    company_id: int

    def ensure_one(self):
        """Mirror the minimal singleton contract exposed by an Odoo record."""

        return self

    def __reduce__(self):
        """Keep the resolved App Secret out of queue/RPC serialization."""

        raise TypeError("Meta runtime App is not serializable")

    def __reduce_ex__(self, _protocol):
        """Keep the resolved App Secret out of queue/RPC serialization."""

        raise TypeError("Meta runtime App is not serializable")


def _validated_secret(value):
    if not isinstance(value, str):
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    value = value.strip()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise MetaCredentialResolutionError("Meta credential is unavailable") from None
    if len(value) < 16 or len(encoded) > _MAX_SECRET_BYTES:
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    return value


def validate_secret_reference(backend, reference):
    """Validate and return one opaque secret reference without resolving it."""

    if not isinstance(reference, str):
        raise MetaCredentialResolutionError("Meta credential reference is invalid")
    pattern = {
        "environment": _ENVIRONMENT_REF_RE,
        "file": _FILE_REF_RE,
    }.get(backend)
    if pattern is None:
        raise MetaCredentialResolutionError("Meta credential backend is unsupported")
    if not pattern.fullmatch(reference):
        raise MetaCredentialResolutionError("Meta credential reference is invalid")
    return reference


def _environment_secret(reference):
    reference = validate_secret_reference("environment", reference)
    return _validated_secret(os.environ.get(reference))


def _secret_root():
    root_value = os.environ.get(_SECRET_ROOT_ENVIRONMENT_KEY, "")
    if not root_value:
        raise MetaCredentialResolutionError("Meta credential backend is unavailable")
    try:
        root = Path(root_value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise MetaCredentialResolutionError(
            "Meta credential backend is unavailable"
        ) from None
    if not root.is_dir():
        raise MetaCredentialResolutionError("Meta credential backend is unavailable")
    return root


def _open_secret_file(root, reference):
    target = root.joinpath(reference)
    try:
        if target.is_symlink():
            raise MetaCredentialResolutionError("Meta credential file is unsafe")
        resolved = target.resolve(strict=True)
        if resolved.parent != root:
            raise MetaCredentialResolutionError("Meta credential file is unsafe")
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
        )
    except MetaCredentialResolutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise MetaCredentialResolutionError("Meta credential is unavailable") from None
    return descriptor


def _read_private_regular_file(descriptor):
    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise MetaCredentialResolutionError("Meta credential file is unsafe")
            if metadata.st_size <= 0 or metadata.st_size > _MAX_SECRET_BYTES:
                raise MetaCredentialResolutionError("Meta credential is unavailable")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(_MAX_SECRET_BYTES + 1)
        except MetaCredentialResolutionError:
            raise
        except OSError:
            raise MetaCredentialResolutionError(
                "Meta credential is unavailable"
            ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MetaCredentialResolutionError("Meta credential is unavailable") from None
    return _validated_secret(value)


def _file_secret(reference):
    reference = validate_secret_reference("file", reference)
    descriptor = _open_secret_file(_secret_root(), reference)
    return _read_private_regular_file(descriptor)


def resolve_secret(backend, reference):
    """Resolve one referenced secret while keeping values out of errors and logs."""

    if backend == "environment":
        return _environment_secret(reference)
    if backend == "file":
        return _file_secret(reference)
    raise MetaCredentialResolutionError("Meta credential backend is unsupported")
