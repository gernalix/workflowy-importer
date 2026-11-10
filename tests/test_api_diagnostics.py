import traceback
import unittest
from unittest.mock import patch

import httpx

from workflowy_importer.api import WorkflowyAPIError, WorkflowyClient


class ApiDiagnosticTests(unittest.TestCase):
    def test_malformed_credentials_fail_before_client_construction_without_echo(self):
        for separator in ('\r', '\n', '\r\n', '\x00', '\u200b', '\u00e9'):
            key = 'SYNTHETIC_PRIVATE_CREDENTIAL' + separator + 'suffix'
            with self.subTest(separator=repr(separator)), patch('workflowy_importer.api.httpx.Client') as factory:
                with self.assertRaises(WorkflowyAPIError) as caught:
                    WorkflowyClient(api_key=key)
                factory.assert_not_called()
                self.assertNotIn('SYNTHETIC_PRIVATE_CREDENTIAL', str(caught.exception))
                self.assertIsNone(caught.exception.status_code)

    def test_http_error_body_and_dynamic_node_path_are_not_public_diagnostics(self):
        marker = 'SYNTHETIC_PRIVATE_RESPONSE'
        node_id = 'SYNTHETIC_PRIVATE_NODE'
        for status in (400, 401, 404, 429, 500, 503):
            with self.subTest(status=status), WorkflowyClient(api_key='synthetic-key', max_retries=0) as client:
                client._client.close()
                client._client = httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=marker)), base_url='https://example.invalid')
                try:
                    client.get_node(node_id)
                except WorkflowyAPIError as error:
                    self.assertEqual(error.status_code, status)
                    self.assertIn(str(status), str(error))
                    rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
                    self.assertNotIn(marker, rendered)
                    self.assertNotIn(node_id, rendered)
                else:
                    self.fail('HTTP error required')

    def test_credential_bearing_and_unprintable_transport_errors_remain_safe(self):
        class Unprintable(httpx.ConnectError):
            def __str__(self):
                raise AssertionError('transport error must not be formatted')
        marker = 'SYNTHETIC_PRIVATE_CREDENTIAL'
        for failure in (httpx.LocalProtocolError(marker), Unprintable(marker)):
            with self.subTest(kind=type(failure).__name__), WorkflowyClient(api_key='synthetic-key', max_retries=0) as client:
                client._client.close()
                def fail(request):
                    raise failure
                client._client = httpx.Client(transport=httpx.MockTransport(fail), base_url='https://example.invalid')
                try:
                    client.create_node('inbox', 'synthetic')
                except WorkflowyAPIError as error:
                    self.assertIsNone(error.status_code)
                    self.assertEqual(str(error), 'Workflowy API transport failed')
                    rendered = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
                    self.assertNotIn(marker, rendered)
                    self.assertNotIn(marker, repr(error))
                else:
                    self.fail('transport error required')

    def test_normal_client_repr_does_not_include_api_key(self):
        key = 'SYNTHETIC_PRIVATE_CREDENTIAL'
        with WorkflowyClient(api_key=key) as client:
            self.assertNotIn(key, repr(client))
            self.assertEqual(client._client.headers['Authorization'], 'Bearer ' + key)


if __name__ == '__main__':
    unittest.main()
