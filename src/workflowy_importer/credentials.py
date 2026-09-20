from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

DEFAULT_SECRET_FILE = Path("~/.config/codex/secrets/workflowy-api-key").expanduser()
SYSTEMD_CREDENTIAL_NAME = "workflowy-api-key"
SECRET_SERVICE_ATTRIBUTES = (
    "application",
    "workflowy-importer",
    "credential",
    "workflowy-api-key",
)
PROVIDERS = ("auto", "systemd", "secret-service", "env", "legacy-file")


class CredentialError(RuntimeError):
    pass


def _nonempty(value: str, source: str) -> str:
    value = value.strip()
    if not value:
        raise CredentialError(f"Workflowy API key from {source} is empty")
    return value


def _read_protected_file(path: Path, *, systemd_credential: bool = False) -> str:
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        raise CredentialError(f"Workflowy credential file is missing: {path}") from exc

    if stat.S_ISLNK(st.st_mode):
        raise CredentialError(f"Workflowy credential file must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError(f"Workflowy credential path is not a regular file: {path}")
    allowed_owners = {os.getuid()}
    if systemd_credential:
        allowed_owners.add(0)
    if st.st_uid not in allowed_owners:
        raise CredentialError(f"Workflowy credential file has the wrong owner: {path}")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise CredentialError(
            f"Workflowy credential file permissions expose group/other bits ({mode:04o}): {path}"
        )
    try:
        return _nonempty(path.read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise CredentialError(f"Cannot read Workflowy credential file {path}: {exc}") from exc


def _systemd_credential() -> str | None:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not directory:
        return None
    path = Path(directory) / SYSTEMD_CREDENTIAL_NAME
    if not path.exists():
        return None
    return _read_protected_file(path, systemd_credential=True)


def _secret_service_lookup() -> str | None:
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", *SECRET_SERVICE_ATTRIBUTES],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialError(f"Secret Service lookup failed: {exc.__class__.__name__}") from exc
    if proc.returncode != 0:
        return None
    return _nonempty(proc.stdout, "Secret Service")


def _environment(env_var: str) -> str | None:
    value = os.environ.get(env_var, "")
    return value.strip() or None


def load_api_key(
    *,
    secret_file: Path | str = DEFAULT_SECRET_FILE,
    env_var: str = "WORKFLOWY_API_KEY",
    provider: str | None = None,
    allow_env_fallback: bool = True,
) -> str:
    """Resolve the Workflowy API key through a context-appropriate provider.

    Auto order is systemd credential -> Secret Service/libsecret -> ephemeral
    environment -> protected legacy file. The legacy file remains only for
    migration/backward compatibility. An explicitly selected provider fails
    closed instead of silently downgrading to another provider.
    """

    selected = (provider or os.environ.get("WORKFLOWY_CREDENTIAL_PROVIDER") or "auto").strip().lower()
    if selected not in PROVIDERS:
        raise CredentialError(
            f"Unsupported Workflowy credential provider {selected!r}; expected one of {', '.join(PROVIDERS)}"
        )

    legacy_path = Path(secret_file).expanduser()

    def resolve(name: str) -> str | None:
        if name == "systemd":
            return _systemd_credential()
        if name == "secret-service":
            return _secret_service_lookup()
        if name == "env":
            return _environment(env_var) if allow_env_fallback else None
        if name == "legacy-file":
            if not legacy_path.exists():
                return None
            return _read_protected_file(legacy_path)
        raise AssertionError(name)

    if selected != "auto":
        value = resolve(selected)
        if value:
            return value
        raise CredentialError(f"Workflowy API key unavailable from provider: {selected}")

    for name in ("systemd", "secret-service", "legacy-file", "env"):
        value = resolve(name)
        if value:
            return value

    raise CredentialError(
        "Workflowy API key missing: configure a systemd credential, Secret Service entry, "
        f"{env_var}, or the legacy protected file {legacy_path}"
    )
