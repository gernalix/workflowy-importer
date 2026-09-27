from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from workflowy_importer import manual_order_control


class ManualOrderControlTests(unittest.TestCase):
    @patch('workflowy_importer.manual_order_control._canonical_modules')
    @patch('workflowy_importer.manual_order_control.transport_request_key',
           return_value='c2-workflowy-order-transport-set')
    def test_set_uses_correct_arguments_and_loaded_authority(self, _key, modules):
        control = Mock()
        control.submit_control.return_value = {'submission': 'queued'}
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 7)
        modules.return_value = (control, helper)
        with redirect_stdout(StringIO()) as output:
            result = manual_order_control.main([
                'set', '--scope', 'inbox', '--source-modified-at', 'wf:9',
                'issue:two', 'issue:one',
            ])
        self.assertEqual(0, result)
        call = control.submit_control.call_args.kwargs
        self.assertEqual('set_manual_order', call['operation'])
        self.assertEqual({
            'scope': 'inbox',
            'ordered_ids': ['issue:two', 'issue:one'],
            'source': 'workflowy',
            'source_modified_at': 'wf:9',
        }, call['arguments'])
        self.assertEqual('supervisor-one', call['supervisor_id'])
        self.assertEqual(7, call['fencing_token'])
        self.assertEqual('c2-workflowy-order-transport-set', call['request_key'])
        helper.load_runtime_identity.assert_called_once_with()
        self.assertEqual('queued', json.loads(output.getvalue())['status'])

    @patch('workflowy_importer.manual_order_control._canonical_modules')
    @patch('workflowy_importer.manual_order_control.transport_request_key',
           return_value='c2-workflowy-order-transport-clear')
    def test_clear_uses_correct_arguments_and_loaded_authority(self, _key, modules):
        control = Mock()
        control.submit_control.return_value = {'submission': 'queued'}
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 7)
        modules.return_value = (control, helper)
        with redirect_stdout(StringIO()) as output:
            result = manual_order_control.main([
                'clear', '--scope', 'roadmap', 'two', 'one',
            ])
        self.assertEqual(0, result)
        call = control.submit_control.call_args.kwargs
        self.assertEqual('clear_manual_order', call['operation'])
        self.assertEqual({'scope': 'roadmap', 'ids': ['one', 'two']}, call['arguments'])
        self.assertEqual('supervisor-one', call['supervisor_id'])
        self.assertEqual(7, call['fencing_token'])
        self.assertEqual('c2-workflowy-order-transport-clear', call['request_key'])
        helper.load_runtime_identity.assert_called_once_with()
        self.assertEqual('queued', json.loads(output.getvalue())['status'])

    @patch('workflowy_importer.manual_order_control._canonical_modules')
    @patch('workflowy_importer.manual_order_control.transport_request_key',
           side_effect=('c2-workflowy-order-first', 'c2-workflowy-order-second'))
    def test_separate_actual_submissions_use_fresh_transport_keys(self, _keys, modules):
        control = Mock()
        control.submit_control.return_value = {'submission': 'queued'}
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 8)
        modules.return_value = (control, helper)
        args = [
            'set', '--scope', 'roadmap', '--source-modified-at', 'wf:1',
            'two', 'one',
        ]
        with redirect_stdout(StringIO()):
            manual_order_control.main(args)
            manual_order_control.main(args)
        request_keys = [
            call.kwargs['request_key'] for call in control.submit_control.call_args_list
        ]
        self.assertEqual(
            ['c2-workflowy-order-first', 'c2-workflowy-order-second'],
            request_keys,
        )

    @patch('workflowy_importer.manual_order_control._canonical_modules')
    def test_request_key_conflict_propagates_fail_closed(self, modules):
        control = Mock()
        control.submit_control.side_effect = RuntimeError(
            'request_key_conflict:c2-workflowy-order-transport-key')
        helper = Mock()
        helper.load_runtime_identity.return_value = ('supervisor-one', 8)
        modules.return_value = (control, helper)
        with self.assertRaisesRegex(RuntimeError, 'request_key_conflict'):
            manual_order_control.main([
                'set', '--scope', 'inbox', '--source-modified-at', 'wf:9',
                'issue:two', 'issue:one',
            ])


if __name__ == '__main__':
    unittest.main()
