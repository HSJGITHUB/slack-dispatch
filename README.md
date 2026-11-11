# slack-dispatch

## What
A pre-alpha scaffold for receiving Slack messages through a single intake,
recording assignment state in a ledger, and dispatching work to stable lanes.
Version 0.0.1 is not a working dispatcher.

## Why
Independent session sweeps lose shared cursors and ownership. A durable lane
inbox survives session replacement and gives unmatched work a human owner.

## How it works
Planned flow: source event -> ledger -> first matching deterministic rule ->
lane inbox -> wake adapter. Unmatched work enters a human queue with an optional
classifier suggestion. The classifier does not assign work in this release.

## Architecture
- `slack_dispatch/config`: working YAML loader and validated dataclasses.
- `ingest`, `ledger`: source interfaces and draft SQLite DDL.
- `router`: first-match iteration, rule stubs and a null suggester.
- `inbox`, `notify`, `service`: command parsing and explicit runtime stubs.
- `tools/probe`: metadata-only Socket Mode probe for human-operated validation.
- `tools/envhelper`: Windows secret-file helper with offline regression tests.

See [design](docs/DESIGN.md) and [decisions](docs/DECISIONS.md).

## Quickstart
Requires Python 3.12+ and Windows for the environment-helper tests.
Install dependencies in a prepared environment, then run offline tests:

```sh
pip install -e .[dev]
pytest -q
ruff check .
python -m slack_dispatch.inbox.cli --help
```

If pytest is unavailable, run `python -m unittest discover -s tests`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/envhelper/tests/run_tests.ps1
```

Installation and CI dependency setup require package downloads unless cached.
Tests themselves use fake inputs and no Slack requests. Do not start the probe
as part of testing. All functional inbox commands and the service currently
raise `NotImplementedError`. The sample YAML is illustrative, not deployment
configuration. Keep real configuration and credentials outside the repository.

## Design principles
Single intake, one state per message, stable lane ownership, deterministic
routing first, human approval for outbound messages, and identifier-only wake
notifications. Treat external message content as untrusted data.

## Status
License: TBD (to be decided before any public release)

Scaffold only. Live event visibility, session wake delivery, deployment,
transactional deduplication and end-to-end recovery remain unverified.
See [SELF_CHECK.md](SELF_CHECK.md) for the checks actually run.

## Roadmap
1. Ingest and transactional ledger, cursor reconciliation and recovery.
2. Concrete routing rules, human queue, timers and shadow suggestions.
3. Durable inbox CLI, notification adapters and wake policy.
4. Human-approved per-channel classifier promotion and optional status UI.

## 한국어 요약
Slack 메시지를 한 곳에서 받아 고정 레인에 배정하는 도구의 초기 뼈대입니다.
설정 로더와 규칙 순서 엔진, 오프라인 테스트가 포함됩니다. 수집·원장·알림·명령의
실제 동작은 후속 구현 대상입니다. 실제 설정과 자격증명은 저장소 밖에서 관리합니다.
