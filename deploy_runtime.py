#!/usr/bin/env python3
"""Deploy the Workflowy bridge/runtime after an autosync fast-forward."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
UNIT_SOURCE = ROOT / "deploy" / "systemd"
UNITS = tuple(sorted(path.name for path in UNIT_SOURCE.iterdir() if path.suffix in {".service", ".timer"}))
CORE_UNITS = ("workflowy-bridge.service", "workflowy-roadmap-sync.timer")


def checked(*args: str) -> None:
    subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def main() -> int:
    target = Path.home() / ".config" / "systemd" / "user"
    target.mkdir(parents=True, exist_ok=True)

    for name in UNITS:
        source = UNIT_SOURCE / name
        destination = target / name
        if not destination.exists() or destination.read_bytes() != source.read_bytes():
            shutil.copyfile(source, destination)

    checked("systemctl", "--user", "daemon-reload")
    for unit in CORE_UNITS:
        checked("systemctl", "--user", "enable", "--now", unit)

    # The long-lived bridge must reload Python code from the freshly updated
    # checkout. The one-shot roadmap sync is kicked immediately so the
    # WorkFlowy dashboard does not wait for the next timer tick.
    checked("systemctl", "--user", "restart", "workflowy-bridge.service")
    checked("systemctl", "--user", "start", "workflowy-roadmap-sync.service")
    print("Workflowy runtime deployed; bridge restarted and roadmap sync triggered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
