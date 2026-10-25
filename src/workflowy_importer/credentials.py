from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_SECRET_FILE = Path("~/.config/codex/secrets/workflowy-api-key").expanduser()
SYSTEMD_CREDENTIAL_NAME = "workflowy-api-key"
SECRET_SERVICE_ATTRIBUTES = (
    "application",
    "workflowy-importer",
    "credential",
    "workflowy-api-key",
)
PROVIDERS = ("auto", "bitwarden", "systemd", "secret-service", "env", "legacy-file")


class CredentialError(RuntimeError):
    pass


def _bitwarden_configured() -> bool:
    path = Path.home() / ".config/bws/references.json"
    if not path.exists():
        return False
    try:
        return "FEDORA_WORKFLOWY_ENV" in json.loads(path.read_text())["references"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CredentialError("Bitwarden reference configuration is invalid") from exc


def _bitwarden_credential() -> str:
    # The existing host runtime validates the project, strips its machine token,
    # and injects only this field. Child output stays in this private pipe.
    runtime = Path.home() / ".local/bin/bws-credential"
    environment = dict(os.environ)
    for name in ("BWS_ACCESS_TOKEN", "WORKFLOWY_API_KEY", "RUST_LOG", "RUST_BACKTRACE"):
        environment.pop(name, None)
    try:
        result = subprocess.run(
            [str(runtime), "--ref", "FEDORA_WORKFLOWY_ENV", "--var", "WORKFLOWY_API_KEY", "--",
             sys.executable, "-c", "import os,sys;sys.stdout.write(os.environ['WORKFLOWY_API_KEY'])"],
            capture_output=True, text=True, env=environment, timeout=55, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialError("Bitwarden Workflowy credential is unavailable") from None
    if result.returncode:
        raise CredentialError("Bitwarden Workflowy credential retrieval failed")
    return _nonempty(result.stdout, "Bitwarden")


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
    if selected == "auto" and _bitwarden_configured():
        selected = "bitwarden"
    if selected not in PROVIDERS:
        raise CredentialError(
            f"Unsupported Workflowy credential provider {selected!r}; expected one of {', '.join(PROVIDERS)}"
        )

    legacy_path = Path(secret_file).expanduser()

    def resolve(name: str) -> str | None:
        if name == "bitwarden":
            return _bitwarden_credential()
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
