"""Never retain bodies from text, blocks, attachments, or files."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time


class TokenError(ValueError):
    pass


def parse_env(source):
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() not in ('SLACK_DISPATCH_APP_TOKEN', 'SLACK_APP_TOKEN'):
            continue
        value = value.split('#', 1)[0].strip()
        if not value.startswith('xapp-'):
            raise TokenError('Invalid app token format')
        return value
    raise TokenError('Missing app token key')


def safe_string(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def redact_event(payload: dict) -> dict:
    # Reject arbitrary objects and bodies in metadata fields.
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get('event', {})
    event = event if isinstance(event, dict) else {}
    nested = event.get('message', {})
    nested = nested if isinstance(nested, dict) else {}
    item = event.get('item', {})
    item = item if isinstance(item, dict) else {}
    metadata = nested if event.get('subtype') == 'message_changed' else event
    identifier = r'[A-Z][A-Z0-9]{1,63}'
    timestamp = r'[0-9]{1,20}(?:\.[0-9]{1,10})?'
    label = r'[a-z][a-z0-9_]{0,63}'
    result = {'received_at': datetime.now().astimezone().isoformat()}
    result['event_id'] = safe_string(payload.get('event_id'), r'Ev[A-Za-z0-9]{1,64}')
    for key in ('event_time', 'retry_attempt'):
        value = payload.get(key)
        result[key] = value if type(value) is int and value >= 0 else None
    result['retry_reason'] = safe_string(payload.get('retry_reason'), label)
    result['event_type'] = safe_string(event.get('type'), label)
    result['subtype'] = safe_string(event.get('subtype'), label)
    result['channel'] = safe_string(event.get('channel') or event.get('channel_id', item.get('channel')), identifier)
    result['channel_type'] = safe_string(event.get('channel_type'), label)
    result['user'] = safe_string(event.get('user') or event.get('user_id', metadata.get('user')), identifier)
    if 'bot_id' in metadata:
        result['bot_id'] = bool(metadata['bot_id'])
    result['ts'] = safe_string(metadata.get('ts', item.get('ts')), timestamp)
    thread_ts = safe_string(metadata.get('thread_ts'), timestamp)
    if thread_ts is not None:
        result['thread_ts'] = thread_ts
    result['is_thread_reply'] = thread_ts is not None and thread_ts != result['ts']
    result['has_files'] = bool(metadata.get('files')) or event.get('type') == 'file_shared'
    auth = payload.get('authorizations', [])
    auth = auth if isinstance(auth, list) else []
    result['authorizations_user_ids'] = []
    result['authorizations_is_bot'] = []
    for entry in auth:
        if not isinstance(entry, dict):
            continue
        user_id = safe_string(entry.get('user_id'), identifier)
        if user_id is not None:
            result['authorizations_user_ids'].append(user_id)
        if type(entry.get('is_bot')) is bool:
            result['authorizations_is_bot'].append(entry['is_bot'])
    return result


class ConnectionCounts:
    def __init__(self):
        self.lock = threading.Lock()
        self.opened = False
        self.down = False
        self.disconnects = 0
        self.reconnects = 0

    def connected(self):
        with self.lock:
            if self.opened:
                self.reconnects += 1
                if not self.down:
                    self.disconnects += 1
            self.opened, self.down = True, False

    def observe(self, connected):
        with self.lock:
            if self.opened and not connected and not self.down:
                self.disconnects += 1
                self.down = True


class Recorder:
    def __init__(self, directory, response_class):
        self.directory = Path(directory)
        self.response_class = response_class
        self.lock = threading.Lock()
        self.types = Counter()
        self.channels = Counter()
        self.failed = threading.Event()

    def receive(self, client, request):
        try:
            client.send_socket_mode_response(self.response_class(envelope_id=request.envelope_id))
            if request.type != 'events_api':
                return
            payload = dict(request.payload)
            for key in ('retry_attempt', 'retry_reason'):
                value = getattr(request, key, None)
                if value is not None:
                    payload[key] = value
            record = redact_event(payload)
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                day = datetime.now().astimezone().strftime('%Y%m%d')
                with (self.directory / f'events_{day}.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                self.types[record['event_type'] or '-'] += 1
                self.channels[record['channel'] or '-'] += 1
                print(f"{record['event_type']}/{record['subtype'] or '-'} "
                      f"{record['channel'] or '-'} {record['ts'] or '-'} "
                      f"thread?={record['is_thread_reply']}", flush=True)
        except Exception:
            # Use fixed errors because exceptions may contain raw data.
            print('ACK or event recording failed', flush=True)
            self.failed.set()

    def summary(self, final=False):
        with self.lock:
            print('Cumulative totals by type', flush=True)
            for key, count in sorted(self.types.items()):
                print(f'{key}\t{count}', flush=True)
            if final:
                print('Totals by channel', flush=True)
                for key, count in sorted(self.channels.items()):
                    print(f'{key}\t{count}', flush=True)


def positive_minutes(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Minutes must be finite and positive')
    return number


def read_env_file(path):
    with Path(path).expanduser().open(encoding='utf-8-sig') as stream:
        return stream.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Slack intake probe without body storage')
    parser.add_argument('--env-file', default=os.environ.get('SLACK_DISPATCH_ENV_FILE', '~/.local/slack-dispatch.env'))
    parser.add_argument('--out-dir', default='out')
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--minutes', type=positive_minutes, default=10)
    duration.add_argument('--forever', action='store_true')
    args = parser.parse_args(argv)
    try:
        token = parse_env(read_env_file(args.env_file))
    except (OSError, UnicodeError, TokenError):
        print('App token unreadable or invalid')
        return 2
    print(f'App token: {token[:5]} length={len(token)} sha256={hashlib.sha256(token.encode()).hexdigest()[:8]}', flush=True)
    # SDK diagnostics may contain raw envelopes and authentication data.
    logging.disable(logging.CRITICAL)
    counts = ConnectionCounts()
    client = None
    recorder = None
    code = 0
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        class MeasuredClient(SocketModeClient):
            def connect(self):
                super().connect()
                counts.connected()

        recorder = Recorder(args.out_dir, SocketModeResponse)
        client = MeasuredClient(app_token=token)
        client.socket_mode_request_listeners.append(recorder.receive)
        client.connect()
        started = time.monotonic()
        next_report = started + 60
        while args.forever or time.monotonic() - started < args.minutes * 60:
            if recorder.failed.is_set():
                code = 3
                break
            counts.observe(client.is_connected())
            if time.monotonic() >= next_report:
                recorder.summary()
                next_report += 60
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    except Exception:
        print('SDK setup or connection failed', flush=True)
        code = 3
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                print('Connection close failed', flush=True)
                code = 3
        if recorder is not None:
            recorder.summary(final=True)
            if recorder.failed.is_set():
                code = 3
        print(f'disconnects/session_changes={counts.disconnects} reconnects={counts.reconnects}', flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
