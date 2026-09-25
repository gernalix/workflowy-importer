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

    def test_c2_daily_mirror_runs_each_day_and_is_persistent(self) -> None:
        timer = (
            ROOT / "deploy/systemd/workflowy-c2-daily-mirror.timer"
        ).read_text(encoding="utf-8")
        service = (
            ROOT / "deploy/systemd/workflowy-c2-daily-mirror.service"
        ).read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 00:05:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(
            "ensure-daily-mirror b8d749c7-7e22-4024-bf11-98c00c618b80 --since 2026-09-25",
            service,
        )


if __name__ == "__main__":
    unittest.main()
