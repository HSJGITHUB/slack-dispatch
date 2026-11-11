import contextlib
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch, mock_open

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('probe', ROOT / 'tools' / 'probe' / 'probe_socket_events.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class RedactTests(unittest.TestCase):
    def assert_clean(self, value):
        forbidden = {'text', 'blocks', 'files', 'attachments', 'message', 'previous_message'}
        if isinstance(value, dict):
            self.assertFalse(forbidden.intersection(value))
            for child in value.values():
                self.assert_clean(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_clean(child)

    def test_five_event_fixtures(self):
        fixtures = [
            {'type': 'message', 'channel': 'CFAKE', 'user': 'UFAKE', 'ts': '100.001', 'text': 'PRIVATE_BODY', 'blocks': [{'text': 'PRIVATE_BODY'}], 'files': [{'name': 'PRIVATE_FILENAME'}], 'attachments': [{'text': 'PRIVATE_BODY'}], 'bot_id': 'BFAKE'},
            {'type': 'message', 'channel': 'CFAKE', 'user': 'UFAKE', 'ts': '100.002', 'thread_ts': '100.001', 'text': 'PRIVATE_BODY'},
            {'type': 'message', 'subtype': 'message_changed', 'channel': 'CFAKE', 'message': {'user': 'UFAKE', 'ts': '100.002', 'thread_ts': '100.001', 'text': 'PRIVATE_BODY'}, 'previous_message': {'text': 'PRIVATE_BODY'}},
            {'type': 'reaction_added', 'user': 'UFAKE', 'reaction': 'PRIVATE_BODY', 'item': {'channel': 'CFAKE', 'ts': '100.001', 'type': 'message'}},
            {'type': 'file_shared', 'channel_id': 'CFAKE', 'user_id': 'UFAKE', 'file_id': 'FPRIVATE', 'files': [{'name': 'PRIVATE_FILENAME'}]},
        ]
        for index, event in enumerate(fixtures):
            with self.subTest(index=index):
                payload = {'event': event, 'event_id': 'EvFAKE', 'event_time': 100, 'authorizations': [{'user_id': 'UFAKE', 'is_bot': False}]}
                record = probe.redact_event(payload)
                self.assert_clean(record)
                self.assertNotIn('PRIVATE', json.dumps(record))
                self.assertEqual(record['event_type'], event['type'])
                self.assertEqual(record['event_id'], 'EvFAKE')
                self.assertEqual(record['event_time'], 100)
                self.assertEqual(record['authorizations_user_ids'], ['UFAKE'])
                self.assertEqual(record['authorizations_is_bot'], [False])
                self.assertEqual(record['is_thread_reply'], index in (1, 2))
                self.assertEqual(record['has_files'], index in (0, 4))
                self.assertIn('received_at', record)
                if index < 4:
                    self.assertEqual(record['channel'], 'CFAKE')
                    self.assertIsNotNone(record['ts'])
                if index == 0:
                    self.assertIs(record['bot_id'], True)
                if index == 2:
                    self.assertEqual(record['subtype'], 'message_changed')
                if index == 4:
                    self.assertEqual(record['channel'], 'CFAKE')
                    self.assertEqual(record['user'], 'UFAKE')

    def test_malformed_metadata_is_not_copied(self):
        result = probe.redact_event({'event': {'type': {'text': 'PRIVATE'}, 'channel': ['PRIVATE'], 'user': 'xapp-FAKE', 'ts': {'text': 'PRIVATE'}}, 'authorizations': [{'user_id': {'text': 'PRIVATE'}, 'is_bot': 'PRIVATE'}]})
        self.assert_clean(result)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('xapp-', json.dumps(result))

    def test_parent_thread_is_not_reply(self):
        self.assertFalse(probe.redact_event({'event': {'ts': '100.001', 'thread_ts': '100.001'}})['is_thread_reply'])


class EnvTests(unittest.TestCase):
    def test_crlf_comments(self):
        self.assertEqual(probe.parse_env('# comment\r\nOTHER=value\r\n SLACK_APP_TOKEN = xapp-FAKE-A # comment\r\n'), 'xapp-FAKE-A')

    def test_first_key_wins_both_orders(self):
        for first, second in [('SLACK_APP_TOKEN', 'SLACK_DISPATCH_APP_TOKEN'), ('SLACK_DISPATCH_APP_TOKEN', 'SLACK_APP_TOKEN')]:
            self.assertEqual(probe.parse_env(f'{first}=xapp-FAKE-FIRST\n{second}=xapp-FAKE-SECOND'), 'xapp-FAKE-FIRST')

    def test_invalid_first_value_does_not_fall_back(self):
        for value in ('xoxp-FAKE', '', 'invalid'):
            with self.assertRaises(probe.TokenError):
                probe.parse_env(f'SLACK_DISPATCH_APP_TOKEN={value}\nSLACK_APP_TOKEN=xapp-FAKE')

    def test_missing_key(self):
        with self.assertRaises(probe.TokenError):
            probe.parse_env('# absent\nOTHER=value')


class RuntimeTests(unittest.TestCase):
    def test_ack_before_redaction_and_retry_fields(self):
        order = []
        client = types.SimpleNamespace(send_socket_mode_response=lambda response: order.append('ack'))
        def response(**kwargs):
            return kwargs
        request = types.SimpleNamespace(envelope_id='FAKE', type='events_api', payload={'event': {'type': 'message', 'text': 'PRIVATE_BODY'}}, retry_attempt=2, retry_reason='timeout')
        original = probe.redact_event
        def redact(payload):
            self.assertEqual(order, ['ack'])
            return original(payload)
        with patch.object(Path, 'mkdir'), patch.object(Path, 'open', mock_open()) as opened, patch.object(probe, 'redact_event', side_effect=redact), contextlib.redirect_stdout(io.StringIO()) as output:
            recorder = probe.Recorder(ROOT, response)
            recorder.receive(client, request)
            record = json.loads(opened().write.call_args.args[0])
            self.assertEqual(record['retry_attempt'], 2)
            self.assertEqual(record['retry_reason'], 'timeout')
            self.assertNotIn('PRIVATE_BODY', output.getvalue())
            self.assertEqual(recorder.types['message'], 1)
            request.type = 'hello'
            recorder.receive(client, request)
            self.assertEqual(order, ['ack', 'ack'])
            self.assertEqual(opened().write.call_count, 1)

    def test_exception_does_not_print_payload(self):
        def fail(response):
            raise RuntimeError('PRIVATE_BODY xapp-FAKE')
        recorder = probe.Recorder(ROOT, lambda **kwargs: kwargs)
        request = types.SimpleNamespace(envelope_id='FAKE', type='events_api')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            recorder.receive(types.SimpleNamespace(send_socket_mode_response=fail), request)
        self.assertTrue(recorder.failed.is_set())
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn('xapp-', output.getvalue())

    def test_connection_counts(self):
        counts = probe.ConnectionCounts()
        counts.connected()
        counts.observe(False)
        counts.observe(False)
        counts.connected()
        counts.connected()
        self.assertEqual((counts.disconnects, counts.reconnects), (2, 2))

    def test_token_exit_code_without_file_access(self):
        with patch.object(probe, 'read_env_file', return_value='SLACK_DISPATCH_APP_TOKEN=xoxp-FAKE'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main([]), 2)

    def test_runtime_with_fake_sdk(self):
        for mode, expected in [('normal', 0), ('connect_fail', 3), ('interrupt', 0)]:
            with self.subTest(mode=mode):
                class FakeClient:
                    def __init__(self, **kwargs):
                        self.socket_mode_request_listeners = []
                        self.closed = False
                    def connect(self):
                        if mode == 'connect_fail':
                            raise RuntimeError('PRIVATE_BODY')
                    def is_connected(self):
                        if mode == 'interrupt':
                            raise KeyboardInterrupt()
                        return True
                    def close(self):
                        self.closed = True
                fake_socket = types.ModuleType('slack_sdk.socket_mode')
                fake_socket.SocketModeClient = FakeClient
                fake_response = types.ModuleType('slack_sdk.socket_mode.response')
                fake_response.SocketModeResponse = lambda **kwargs: kwargs
                modules = {'slack_sdk': types.ModuleType('slack_sdk'), 'slack_sdk.socket_mode': fake_socket, 'slack_sdk.socket_mode.response': fake_response}
                with patch.dict('sys.modules', modules), patch.object(probe, 'read_env_file', return_value='SLACK_DISPATCH_APP_TOKEN=xapp-FAKE'), patch.object(probe.logging, 'disable'), patch.object(probe.time, 'sleep'), patch.object(probe.time, 'monotonic', side_effect=[0, 0, 0, 2]), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(probe.main(['--minutes', '0.01']), expected)
                    self.assertNotIn('PRIVATE_BODY', output.getvalue())
                    self.assertNotIn('xapp-FAKE', output.getvalue())


if __name__ == '__main__':
    unittest.main()
