"""Run actual worker/options handlers against fake Chrome session-only APIs."""
from pathlib import Path
import subprocess
import unittest


class ExtensionSessionTests(unittest.TestCase):
    def test_worker_and_options_session_boundaries(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('extension-session.test.cjs'))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
