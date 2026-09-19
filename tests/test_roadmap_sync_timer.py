from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RoadmapSyncTimerTests(unittest.TestCase):
    def test_timer_respects_workflowy_export_rate_limit(self) -> None:
        timer = (
            ROOT / "deploy/systemd/workflowy-roadmap-sync.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=75s", timer)
        self.assertNotIn("OnUnitActiveSec=30s", timer)


if __name__ == "__main__":
    unittest.main()
