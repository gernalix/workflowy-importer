import contextlib
import io
import unittest

from workflowy_importer import bridge, bridge_cli


class RetirementTests(unittest.TestCase):
    def test_roadmap_command_is_removed_and_personal_capture_remains(self):
        parser = bridge_cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(['roadmap-sync'])
        self.assertEqual(parser.parse_args(['serve']).command, 'serve')
        self.assertTrue(callable(bridge._capture_payload))
        self.assertFalse(hasattr(bridge, '_roadmap_prompt_text'))
        self.assertFalse(hasattr(bridge, '_roadmap_fix_packet'))
