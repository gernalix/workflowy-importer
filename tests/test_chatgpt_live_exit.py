"""The live dashboard must not report success if browser capture failed."""
import argparse
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workflowy_importer import bridge_cli


class ChatGPTLiveExitTests(unittest.TestCase):
    def run_live(self, error):
        args = argparse.Namespace(command="chatgpt-live-sync", cache=None)
        output = StringIO()
        with patch.object(bridge_cli, "_db", return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(bridge_cli, "_client", return_value=nullcontext(object())), \
             patch.object(bridge_cli, "_run_chatgpt_live", return_value={"error": error, "total": 4}), \
             redirect_stdout(output):
            code = bridge_cli.run(args)
        self.assertEqual(json.loads(output.getvalue())["error"], error)
        return code

    def test_hidden_collector_error_exits_failure(self):
        self.assertEqual(self.run_live("BrowserType.connect_over_cdp ECONNREFUSED"), 1)

    def test_success_exits_success(self):
        self.assertEqual(self.run_live(None), 0)


if __name__ == "__main__":
    unittest.main()
