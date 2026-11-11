# Metadata-only intake probe

Only a human authorized to handle credentials should run the live probe:

```sh
python tools/probe/probe_socket_events.py --minutes 10
```

Default credential path: `~/.local/slack-dispatch.env`. Override with
`SLACK_DISPATCH_ENV_FILE` or `--env-file`. Supported keys are
`SLACK_DISPATCH_APP_TOKEN` and `SLACK_APP_TOKEN`; the first supported key in file
order wins, even if invalid. Parsing accepts KEY=VALUE lines and hash comments.

The default duration is ten minutes. `--forever` runs until interruption and is
mutually exclusive with `--minutes`. Ctrl+C is a normal exit. `--out-dir` changes
the default `out` metadata directory. Records append to `events_YYYYMMDD.jsonl`.
No text, blocks, attachments or file bodies are retained.

Output includes event summaries, per-type totals every minute, final channel
counts and disconnect/reconnect counts. Retry events count individually. The
0.2-second observation interval and session replacement detection can count SDK
session renewal as a disconnect; intentional final shutdown is excluded.

Exit codes: 0 normal, 2 unreadable/invalid token, 3 SDK, connection, ACK or record
failure. No events does not prove that subscriptions or permissions are correct.
Offline regression tests mock token reads and SDK calls:
`python -m pytest -q tests/test_probe_redact.py`.
