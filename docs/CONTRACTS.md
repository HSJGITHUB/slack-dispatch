# slack-dispatch phase 1 contracts (v0.1 target)

These contracts let three packets (ingest+ledger, router+queue, inbox+notify) be built in parallel against the v0.0.1 scaffold. Packets implement or consume exactly these shapes. Additive changes only; record any deviation in `CONTRACT_NOTES.md` inside the packet.

## 1. Normalized event (Source -> service -> ledger)

`Event = dict[str, Any]` with keys:

| key | type | notes |
|---|---|---|
| source | str | "slack" |
| event_id | str | Slack event_id, globally unique; dedup key |
| event_type | str | message, reaction_added, reaction_removed, file_shared, channel_created, member_joined_channel |
| subtype | str or None | e.g. message_changed, message_deleted, thread_broadcast |
| channel | str or None | conversation id |
| channel_type | str or None | channel, group, im, mpim |
| user | str or None | author or actor id |
| ts | str or None | message ts; for message_changed the edited message ts; for reactions the reacted item ts |
| thread_ts | str or None | parent ts when the message is a thread reply |
| is_thread_reply | bool | thread_ts present and thread_ts != ts |
| has_files | bool | |
| reaction | str or None | reaction name for reaction events |
| received_at | str | ISO-8601 local time with offset |
| event_time | int or None | Slack event_time |
| retry_attempt | int or None | Socket Mode retry metadata |
| text | str or None | message body. Present only when `ledger.store_body` is true; never printed or logged by any component |

## 2. Message id

`message_id = f"{source}:{channel}:{ts}"`. Stable and derivable from any event that references a message. A `message_changed` event updates the existing row (sets `edited_at`, `followup=1`) and never creates a new row.

## 3. LedgerStore API (package `slack_dispatch.ledger.store`)

Constructor `LedgerStore(path)`; `open()` enables WAL and foreign keys; `migrate()` applies `schema.sql` through a `schema_version` table; `close()`. All mutations run through this single object using `BEGIN IMMEDIATE`.

```
record_event(event: Event) -> RecordResult        # RecordResult(message_id: str | None, created: bool, duplicate: bool, kind: str)
get_message(message_id: str) -> Message | None    # Message = dict with the messages row (body omitted unless include_body=True)
list_messages(*, state: str | None = None, lane: str | None = None, since: str | None = None, limit: int = 100) -> list[Message]
set_route(message_id: str, lane: str, reason: str, *, state: str = "assigned") -> None
set_state(message_id: str, state: str, *, note: str | None = None) -> None   # states: new, assigned, acked, done, human_pending, excluded
set_suggestion(message_id: str, lane: str, reason: str, classifier_version: str) -> None
mark_followup(message_id: str) -> None
bind_thread(source: str, channel: str, thread_ts: str, lane: str) -> None
lookup_binding(source: str, channel: str, thread_ts: str) -> str | None
upsert_lane(lane_id: str, *, session_pointer: str | None = None, paused: bool | None = None) -> None
get_lane(lane_id: str) -> dict | None
list_lanes() -> list[dict]
get_cursor(source: str, channel: str) -> str | None
set_cursor(source: str, channel: str, cursor: str) -> None
list_reactions(message_id: str) -> list[dict]
```

`record_event` behaviour: message events insert (state `new`) or update; reaction events insert into `reactions` and return the referenced message id (created=False); file_shared with a message ts marks `has_files=1` on that message; channel_created and member_joined_channel are stored in `events_seen` only (kind="channel_meta"). Any event whose event_id was already seen returns duplicate=True with no change.

Schema additions owned by the ingest+ledger packet (additive to the scaffold `schema.sql`): `schema_version(version INTEGER)`, `events_seen(event_id PK, received_at, kind, message_id)`, `reactions(message_id, user, reaction, added_at, removed_at)`, columns `messages.event_id`, `messages.channel_type`, `messages.has_files`, `messages.edited_at`, `messages.note`.

Other packets must not edit `schema.sql` or `store.py`; their tests use an in-memory fake implementing the method names above.

## 4. Router contract (package `slack_dispatch.router`)

```
Decision(lane: str | None, reason: str, rule: str | None)       # rule in thread_inherit, explicit_tag, channel_default, person_channel, always_human, excluded, none
route(message: Message, config: Config, lookup_binding: Callable[[str, str, str], str | None]) -> Decision
```
Order: excluded channel -> always_human (configured user ids or mentions of owner id) -> ThreadInherit -> ExplicitTag -> ChannelDefault (a None default means multi-lane, no match) -> PersonChannel -> none. First match wins; no side effects. `Suggester.suggest` runs only when the decision is none; a suggestion never assigns.

## 5. Inbox record (per-lane JSONL, `inbox/<lane>.jsonl`)

`{"id": message_id, "delivered_at": iso, "channel": str, "ts": str, "thread_ts": str | None, "followup": bool, "kind": "new" | "followup" | "reroute"}`. No body. Writers hold `inbox/<lane>.lock` while appending.

## 6. Notifier contract (package `slack_dispatch.notify`)

`Notifier.notify(lane: str, message_ids: list[str]) -> None`. Payloads carry ids and counts only. Adapters: `FileInboxNotifier(root)`, `SessionMessageNotifier(command_template, target_resolver, dry_run)`, `FileDropNotifier(outbox_dir, template)`. Wake policy (`notify/wake.py`) decides when to call notifiers: immediate by default, 90 s debounce per (lane, author), per-lane storm guard (30 per hour -> 600 s batching), global switch (config flag or presence of file `<state>/wake.off`), immediate triggers for configured user ids and owner mentions.

## 7. Config additions (owned by each packet, additive to `config/schema.py`)

- ingest+ledger: `ledger: {path, store_body: bool=false}`, `sources[].kind == "slack_socket"` with `env_file`, `app_token_key`, `user_token_key`; `sources[].kind == "slack_reconcile"` with `interval_seconds`, `lookback_days`.
- router+queue: `always_human: {users: [..], mention_users: [..]}`, `classifier: {kind: "none" | "anthropic", model, api_key_env, taxonomy_version, enabled: false}`, `explicit_tag_pattern` default `\[(?P<lane>[a-z0-9-]+)\]`.
- inbox+notify: `inbox: {root}`, `notify_adapters[]` kinds `file_inbox`, `session_message`, `file_drop`; `wake_policy.immediate_users`, `wake_policy.owner_user`.

Loader changes must keep the existing sample config loading and add the new keys to `examples/instance-sample/config.yaml` with dummy values. Each packet edits only its own keys and adds its own tests for them.
