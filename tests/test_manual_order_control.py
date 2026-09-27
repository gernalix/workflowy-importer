from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from workflowy_importer import manual_order_control


class ManualOrderControlTests(unittest.TestCase):
    @patch('workflowy_importer.manual_order_control._canonical_modules')
    def test_clear_uses_canonical_fenced_control_with_generation_key(self, modules):
        control = Mock()
        control.submit_control.return_value = {'submission': 'queued'}
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 7)
        modules.return_value = (control, helper)
        with redirect_stdout(StringIO()) as output:
            result = manual_order_control.main([
                'clear', '--scope', 'roadmap',
                '--request-token', 'reset-generation-two', 'two', 'one',
            ])
        self.assertEqual(0, result)
        call = control.submit_control.call_args.kwargs
        self.assertEqual('clear_manual_order', call['operation'])
        self.assertEqual({'scope': 'roadmap', 'ids': ['one', 'two']}, call['arguments'])
        self.assertEqual('supervisor-one', call['supervisor_id'])
        self.assertEqual(7, call['fencing_token'])
        self.assertTrue(call['request_key'].startswith('c2-workflowy-order-v2-'))
        self.assertEqual('queued', json.loads(output.getvalue())['status'])

    @patch('workflowy_importer.manual_order_control._canonical_modules')
    def test_same_semantic_request_conflict_is_an_idempotent_replay(self, modules):
        control = Mock()
        control.submit_control.side_effect = RuntimeError(
            'request_key_conflict:c2-workflowy-order-v2-key')
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 8)
        modules.return_value = (control, helper)
        with redirect_stdout(StringIO()) as output:
            result = manual_order_control.main([
                'set', '--scope', 'inbox', '--source-modified-at', 'wf:9',
                'issue:two', 'issue:one',
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual(0, result)
        self.assertEqual('already_applied', payload['outcome'])
        self.assertTrue(payload['idempotent'])


if __name__ == '__main__':
    unittest.main()
