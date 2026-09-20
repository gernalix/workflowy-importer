from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deploy_runtime


class DeployRuntimeTests(unittest.TestCase):
    def test_main_copies_units_and_refreshes_core_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            calls: list[tuple[str, ...]] = []

            def fake_run(args, **kwargs):
                calls.append(tuple(args))
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(deploy_runtime.subprocess, "run", side_effect=fake_run),
            ):
                self.assertEqual(0, deploy_runtime.main())

            target = home / ".config" / "systemd" / "user"
            for name in deploy_runtime.UNITS:
                self.assertEqual(
                    (deploy_runtime.UNIT_SOURCE / name).read_bytes(),
                    (target / name).read_bytes(),
                )

            self.assertIn(("systemctl", "--user", "daemon-reload"), calls)
            self.assertIn(
                ("systemctl", "--user", "enable", "--now", "workflowy-bridge.service"),
                calls,
            )
            self.assertIn(
                ("systemctl", "--user", "enable", "--now", "workflowy-roadmap-sync.timer"),
                calls,
            )
            self.assertIn(
                ("systemctl", "--user", "restart", "workflowy-bridge.service"),
                calls,
            )
            self.assertIn(
                ("systemctl", "--user", "start", "workflowy-roadmap-sync.service"),
                calls,
            )


if __name__ == "__main__":
    unittest.main()
