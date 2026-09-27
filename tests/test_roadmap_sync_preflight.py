from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from workflowy_importer import roadmap_sync_preflight


class RoadmapSyncPreflightTests(unittest.TestCase):
    def test_service_checks_authority_before_sync(self):
        service = (Path(__file__).resolve().parents[1] /
                   'deploy/systemd/workflowy-roadmap-sync.service').read_text()
        self.assertIn(
            'ExecCondition=/usr/bin/python3 -m workflowy_importer.roadmap_sync_preflight',
            service,
        )
        self.assertIn(
            'ExecStart=/usr/bin/python3 -m workflowy_importer.bridge_cli roadmap-sync',
            service,
        )

    @patch.object(roadmap_sync_preflight, 'local_lease_matches', return_value=True)
    @patch.object(roadmap_sync_preflight.subprocess, 'run')
    def test_current_authority_allows_sync(self, run, matches):
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            'outcome': 'ALREADY_ACTIVE', 'phase': 'active',
        }))
        self.assertEqual(0, roadmap_sync_preflight.main())
        matches.assert_called_once()

    @patch.object(roadmap_sync_preflight, 'local_lease_matches', return_value=False)
    @patch.object(roadmap_sync_preflight.subprocess, 'run')
    def test_remote_active_but_local_expired_defers_sync(self, run, _matches):
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            'outcome': 'ALREADY_ACTIVE', 'phase': 'active',
        }))
        with redirect_stderr(StringIO()):
            self.assertEqual(1, roadmap_sync_preflight.main())

    @patch.object(roadmap_sync_preflight.subprocess, 'run')
    def test_new_claim_defers_sync_until_writer_applies_it(self, run):
        for state in (
            {'outcome': 'STALE_TAKEOVER'},
            {'outcome': 'RESUMED'},
            {'outcome': 'ALREADY_ACTIVE', 'phase': 'canonical_successor'},
        ):
            with self.subTest(state=state), redirect_stderr(StringIO()):
                run.return_value = Mock(returncode=0, stdout=json.dumps(state))
                self.assertEqual(1, roadmap_sync_preflight.main())

    @patch.object(roadmap_sync_preflight.subprocess, 'run')
    def test_recovery_failure_fails_service(self, run):
        for result in (
            Mock(returncode=2, stdout=json.dumps({'outcome': 'BLOCKED',
                                                   'reason': 'pointer_blocker'})),
            Mock(returncode=0, stdout='not-json'),
        ):
            with self.subTest(result=result), redirect_stderr(StringIO()):
                run.return_value = result
                self.assertEqual(255, roadmap_sync_preflight.main())


if __name__ == '__main__':
    unittest.main()
