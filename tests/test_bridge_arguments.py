import unittest
from workflowy_importer import bridge_arguments, bridge_cli


class BridgeArgumentsTests(unittest.TestCase):
    def test_parser_reexport_and_safe_read_only_commands(self):
        self.assertIs(bridge_cli.build_parser, bridge_arguments.build_parser)
        args=bridge_cli.build_parser().parse_args(['find','hello','--limit','3'])
        self.assertEqual((args.command,args.query,args.limit),('find','hello',3))
        self.assertEqual(bridge_cli.DEFAULT_CACHE,bridge_arguments.DEFAULT_CACHE)
    def test_retired_roadmap_command_not_resurrected(self):
        with self.assertRaises(SystemExit):
            bridge_arguments.build_parser().parse_args(['roadmap-sync'])
        self.assertEqual(bridge_arguments.build_parser().parse_args(['serve']).command,'serve')


if __name__ == '__main__':
    unittest.main()
