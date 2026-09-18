from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_SECRET_FILE = Path("~/.config/codex/secrets/workflowy-api-key").expanduser()


class CredentialError(RuntimeError):
    pass


def load_api_key(
    *,
    secret_file: Path | str = DEFAULT_SECRET_FILE,
    env_var: str = "WORKFLOWY_API_KEY",
    allow_env_fallback: bool = True,
) -> str:
    """Load the Workflowy API key without ever logging it.

    If the canonical secret file exists it is authoritative and must be a regular,
    non-symlink file owned by the current user with mode 0600. Environment fallback
    is allowed only when the file does not exist.
    """
    path = Path(secret_file).expanduser()
    try:
        st = path.lstat()
    except FileNotFoundError:
        if allow_env_fallback:
            value = os.environ.get(env_var, "").strip()
            if value:
                return value
        raise CredentialError(
            f"Workflowy API key missing: expected {path} or non-empty {env_var}"
        )

    if stat.S_ISLNK(st.st_mode):
        raise CredentialError(f"Workflowy secret file must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError(f"Workflowy secret path is not a regular file: {path}")
    if st.st_uid != os.getuid():
        raise CredentialError(f"Workflowy secret file has the wrong owner: {path}")
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise CredentialError(
            f"Workflowy secret file permissions must be 0600, found {mode:04o}: {path}"
        )

    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CredentialError(f"Cannot read Workflowy secret file {path}: {exc}") from exc
    if not value:
        raise CredentialError(f"Workflowy secret file is empty: {path}")
    return value
