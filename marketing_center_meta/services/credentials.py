import dataclasses
import os
import re
import stat
from pathlib import Path

_ENVIRONMENT_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_FILE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_ROOT_ENVIRONMENT_KEY = "ODOO_META_API_SECRET_DIR"
_MAX_SECRET_BYTES = 64 * 1024


class MetaCredentialResolutionError(Exception):
    """Safely reportable failure while resolving an external secret."""


@dataclasses.dataclass(frozen=True)
class MetaResolvedCredentials:
    app_secret: str
    access_token: str


def _validated_secret(value):
    if not isinstance(value, str):
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    value = value.strip()
    if len(value) < 16 or len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    if any(ord(character) < 32 for character in value):
        raise MetaCredentialResolutionError("Meta credential is unavailable")
    return value


def _environment_secret(reference):
    if not _ENVIRONMENT_REF_RE.fullmatch(reference or ""):
        raise MetaCredentialResolutionError("Meta credential reference is invalid")
    return _validated_secret(os.environ.get(reference))


def _file_secret(reference):
    if not _FILE_REF_RE.fullmatch(reference or ""):
        raise MetaCredentialResolutionError("Meta credential reference is invalid")
    root_value = os.environ.get(_SECRET_ROOT_ENVIRONMENT_KEY, "")
    if not root_value:
        raise MetaCredentialResolutionError("Meta credential backend is unavailable")
    try:
        root = Path(root_value).resolve(strict=True)
    except (OSError, RuntimeError):
        raise MetaCredentialResolutionError(
            "Meta credential backend is unavailable"
        ) from None
    target = root.joinpath(reference)
    try:
        if target.is_symlink():
            raise MetaCredentialResolutionError("Meta credential file is unsafe")
        resolved = target.resolve(strict=True)
        if resolved.parent != root:
            raise MetaCredentialResolutionError("Meta credential file is unsafe")
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except MetaCredentialResolutionError:
        raise
    except (OSError, RuntimeError):
        raise MetaCredentialResolutionError("Meta credential is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise MetaCredentialResolutionError("Meta credential file is unsafe")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_SECRET_BYTES:
            raise MetaCredentialResolutionError("Meta credential is unavailable")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(_MAX_SECRET_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MetaCredentialResolutionError("Meta credential is unavailable") from None
    return _validated_secret(value)


def resolve_secret(backend, reference):
    if backend == "environment":
        return _environment_secret(reference)
    if backend == "file":
        return _file_secret(reference)
    raise MetaCredentialResolutionError("Meta credential backend is unsupported")


def resolve_profile_credentials(profile):
    profile.ensure_one()
    if not profile.active:
        raise MetaCredentialResolutionError("Meta credential profile is paused")
    return MetaResolvedCredentials(
        app_secret=resolve_secret(profile.credential_backend, profile.app_secret_ref),
        access_token=resolve_secret(
            profile.credential_backend,
            profile.access_token_ref,
        ),
    )
