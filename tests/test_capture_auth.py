"""Actual adapter and browser protocol using synthetic keys and fake clients."""
import base64
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from workflowy_importer import bridge, credentials
from workflowy_importer.capture_auth import CaptureAuthority, CaptureAuthError

TOKEN = 'synthetic_capture_pairing_key_' + 'x' * 32
CLIENT_NONCE = bytes(range(32)).hex()
ENDPOINT = 'http://127.0.0.1:8765'


def browser_request(authority, *, challenge=None, post_ok=True):
    challenge = authority.issue(CLIENT_NONCE) if challenge is None else challenge
    fixture = {'token': TOKEN, 'challenge': challenge,
               'payload': {'title': 'Synthetic capture', 'text': 'PRIVATE_FIXTURE_BODY'},
               'postOK': post_ok}
    result = subprocess.run(['node', str(Path(__file__).with_name('capture-client-fixture.cjs'))],
                            input=json.dumps(fixture), text=True, capture_output=True, check=True)
    assert TOKEN not in result.stdout and 'PRIVATE_FIXTURE_BODY' not in result.stdout
    return json.loads(result.stdout)


def headers_for(request):
    headers = Message()
    for key, value in request['headers'].items():
        headers[key] = value
    return headers


class NoRead(io.BytesIO):
    def read(self, *args):
        raise AssertionError('unauthorized body read')


class CaptureProtocolTests(unittest.TestCase):
    def test_real_webcrypto_matches_python_and_replay_is_rejected(self):
        authority = CaptureAuthority(TOKEN, ENDPOINT)
        output = browser_request(authority)
        self.assertEqual(output['reads'], 1)
        self.assertIsNone(output['error'])
        self.assertEqual(len(output['requests']), 2)
        request = output['requests'][1]
        self.assertTrue(all(item['redirect'] == 'error' for item in output['requests']))
        receipt = authority.authorize(headers_for(request))
        plain = authority.decrypt(receipt, base64.b64decode(request['body']))
        self.assertEqual(json.loads(plain), {'title': 'Synthetic capture', 'text': 'PRIVATE_FIXTURE_BODY'})
        with self.assertRaises(CaptureAuthError):
            authority.authorize(headers_for(request))

    def test_fake_or_wrong_endpoint_server_cannot_read_conversation(self):
        authority = CaptureAuthority(TOKEN, ENDPOINT)
        for changed in ({'proof': '0' * 64}, {'endpoint': 'http://127.0.0.1:9999'}, {'server_nonce': 'bad'}):
            with self.subTest(changed=changed):
                challenge = authority.issue(CLIENT_NONCE)
                challenge.update(changed)
                output = browser_request(authority, challenge=challenge)
                self.assertEqual(output['reads'], 0)
                self.assertEqual(len(output['requests']), 1)
                self.assertIsNotNone(output['error'])

    def test_ambiguous_post_failure_is_not_retried(self):
        output = browser_request(CaptureAuthority(TOKEN, ENDPOINT), post_ok=False)
        self.assertEqual(len(output['requests']), 2)
        self.assertEqual(output['reads'], 1)
        self.assertIn('before trying again', output['error'])

    def test_tampering_and_duplicate_headers_are_rejected(self):
        for mutation in ('duplicate', 'proof', 'iv', 'body'):
            with self.subTest(mutation=mutation):
                authority = CaptureAuthority(TOKEN, ENDPOINT)
                request = browser_request(authority)['requests'][1]
                headers = headers_for(request)
                body = bytearray(base64.b64decode(request['body']))
                if mutation == 'duplicate':
                    headers['X-Workflowy-Proof'] = headers['X-Workflowy-Proof']
                elif mutation in ('proof', 'iv'):
                    name = 'X-Workflowy-' + mutation
                    headers.replace_header(name, '0' * len(headers[name]))
                else:
                    body[0] ^= 1
                with self.assertRaises(CaptureAuthError):
                    receipt = authority.authorize(headers)
                    authority.decrypt(receipt, bytes(body))

    def test_parallel_replay_can_authorize_only_one_request(self):
        authority = CaptureAuthority(TOKEN, ENDPOINT)
        request = browser_request(authority)['requests'][1]
        barrier = threading.Barrier(2)
        results = []
        def authorize():
            barrier.wait()
            try:
                authority.authorize(headers_for(request))
                results.append(True)
            except CaptureAuthError:
                results.append(False)
        threads = [threading.Thread(target=authorize) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(sorted(results), [False, True])

    def test_expiry_capacity_and_invalid_token_fail_closed(self):
        with self.assertRaises(CaptureAuthError):
            CaptureAuthority('short', ENDPOINT)
        authority = CaptureAuthority(TOKEN, ENDPOINT)
        request = browser_request(authority)['requests'][1]
        with patch('workflowy_importer.capture_auth.time.monotonic', return_value=float('inf')):
            with self.assertRaises(CaptureAuthError):
                authority.authorize(headers_for(request))
        fresh = CaptureAuthority(TOKEN, ENDPOINT)
        with patch('workflowy_importer.capture_auth.time.monotonic', return_value=1):
            for _ in range(256): fresh.issue(CLIENT_NONCE)
            with self.assertRaises(CaptureAuthError): fresh.issue(CLIENT_NONCE)


class CaptureAdapterTests(unittest.TestCase):
    def exercise(self, script):
        class FakeClient:
            api_key = 'different-synthetic-api-key'
            def __init__(self): self.created = []
            def create_node(self, *args, **kwargs):
                self.created.append((args, kwargs))
                return 'synthetic-created'
        client = FakeClient()
        class Server:
            server_address = ('127.0.0.1', 8765)
            def __init__(self, address, handler): self.handler = handler
            def serve_forever(self): script(self.handler, client)
            def server_close(self): pass
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'ThreadingHTTPServer', Server), patch('builtins.print'):
            bridge.serve(client, None, capture_token=TOKEN, rules_path=Path(directory) / 'absent')

    def request(self, handler, method, path, headers=None, body=b''):
        request = object.__new__(handler)
        request.path = path
        request.headers = headers or Message()
        request.rfile = body if isinstance(body, io.BytesIO) else io.BytesIO(body)
        result = []
        request._reply = lambda status, payload: result.append((status, payload))
        getattr(request, 'do_' + method)()
        return result[0]

    def test_unauthorized_capture_never_reads_body_or_routes(self):
        def script(handler, client):
            for origin in (None, 'chrome-extension://unpaired'):
                headers = Message()
                headers['Content-Type'] = 'application/json'
                if origin: headers['Origin'] = origin
                status, payload = self.request(handler, 'POST', '/capture', headers, NoRead())
                self.assertEqual(status, 401)
                self.assertEqual(payload, {'error': 'capture authorization failed'})
            self.assertFalse(client.created)
        self.exercise(script)

    def test_authenticated_encrypted_capture_reaches_actual_adapter_once(self):
        def script(handler, client):
            status, challenge = self.request(handler, 'GET', '/challenge?client_nonce=' + CLIENT_NONCE)
            self.assertEqual(status, 200)
            # Browser uses the challenge issued by the adapter's own authority.
            output = browser_request(None, challenge=challenge)
            request = output['requests'][1]
            headers = headers_for(request)
            body = base64.b64decode(request['body'])
            headers['Content-Length'] = str(len(body))
            self.assertEqual(self.request(handler, 'POST', '/capture', headers, body)[0], 201)
            self.assertEqual(len(client.created), 2)
            self.assertEqual(client.created[0][0][:2], ('inbox', 'Synthetic capture'))
            self.assertEqual(self.request(handler, 'POST', '/capture', headers, NoRead())[0], 401)
            self.assertEqual(len(client.created), 2)
            self.assertEqual(self.request(handler, 'GET', '/health'), (200, {'status': 'ok'}))
        self.exercise(script)

    def test_invalid_key_does_not_construct_listener(self):
        with patch.object(bridge, 'ThreadingHTTPServer') as server:
            with self.assertRaises(CaptureAuthError):
                bridge.serve(object(), None, capture_token='bad', rules_path=Path('/nonexistent-fixture-rules'))
            server.assert_not_called()


class CaptureCredentialTests(unittest.TestCase):
    def test_only_dedicated_vault_field_and_no_inherited_tokens(self):
        with patch.object(credentials.subprocess, 'run') as run, patch.dict('os.environ', {
            'BWS_ACCESS_TOKEN': 'synthetic-bootstrap', 'WORKFLOWY_API_KEY': 'stale-api', 'WORKFLOWY_CAPTURE_TOKEN': 'stale-capture'
        }):
            run.return_value.returncode = 0
            run.return_value.stdout = TOKEN
            self.assertEqual(credentials.load_capture_token(), TOKEN)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--var') + 1], 'WORKFLOWY_CAPTURE_TOKEN')
            self.assertFalse({'BWS_ACCESS_TOKEN', 'WORKFLOWY_API_KEY', 'WORKFLOWY_CAPTURE_TOKEN'} & run.call_args.kwargs['env'].keys())

    def test_vault_failure_and_bad_value_never_use_fallback(self):
        for code, value in ((1, TOKEN), (0, ''), (0, 'bad\nvalue')):
            with self.subTest(code=code, value=value), patch.object(credentials.subprocess, 'run') as run, patch.object(
                credentials, '_environment', side_effect=AssertionError('fallback forbidden')
            ), patch.object(credentials, '_secret_service_lookup', side_effect=AssertionError('fallback forbidden')):
                run.return_value.returncode, run.return_value.stdout = code, value
                with self.assertRaises(credentials.CredentialError):
                    credentials.load_capture_token()


if __name__ == '__main__':
    unittest.main()
