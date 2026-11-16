# Routing and human review

The router consumes a normalized message and a validated Config. It does not
mutate the message, call a classifier, or write to the ledger.

| Precedence | Condition | Result |
| --- | --- | --- |
| 1 | Excluded channel | Excluded, no attention or classifier |
| 2 | Bot with exclude_all | Excluded with excluded:bot |
| 2 | Bot with route_or_exclude | Channel/DM default only, otherwise excluded:bot |
| 3 | Protected author or configured exact mention | Set attention, continue routing |
| 4 | Existing reply thread binding | Assign inherited lane |
| 5 | Confirmed scope claim in the ledger (P13 ScopeClaim) | Assign claimed lane |
| 6 | First recognized explicit tag | Assign tagged lane |
| 7 | Non-null channel or DM default | Assign default lane |
| 8 | First person-channel rule | Assign rule lane |
| 9 | No lane with attention | Human pending, no classifier |
| 10 | No lane without attention | Human pending, optional classifier suggestion |

The optional bot policy route_all uses the complete normal routing path, including
human review and the optional classifier. The default route_or_exclude never
sends bot traffic to human review, attention, storm notices, or reassignment
proposals. A message is a bot message when it has no author, has subtype
`bot_message`, its author is in bot_messages.known_bot_users, or the user directory
(`users.info` `is_bot`, see P16 below) says its author is a bot. `bot_id`, `app_id`
and `bot_profile` alone do not mark a bot: a human posting through an app is a human.
The pipeline decides the flag once per event and stores it as `is_bot`; `route()`
uses `is_bot` when the message carries it and otherwise applies the rule without a
directory. Channel defaults for bots use the existing DM semantics and override
explicit tags and thread bindings.

Protected authors set attention=always_human; matching mentions set
attention=owner_mention and take priority when both match. Attention does not
prevent assignment. A lane assignment also sends an immediate human notice by
default, with the generated text "FYI, assigned to lane X". With no matching lane,
the item remains human_pending. The ledger stores attention separately from the
selected routing rule. Excluded messages have no attention.

`Decision(lane, reason, rule, attention=None)` identifies the selected rule. Deterministic reasons
are stable rule names, never copies of message text. Author matching accepts the
normalized `user` field, or the ledger `author` field if `user` is absent. Reply
identity is derived from a nonempty `thread_ts` different from `ts`.

## Configuration

All original required scaffold keys remain required. The routing keys are
optional, including when loading older configuration files:

```yaml
always_human:
  users: [U0000000091]
  mention_users: [U0000000092]
  owner_user: U0000000093
classifier:
  kind: none
  enabled: false
  model: placeholder-model
  api_key_env: DUMMY_CLASSIFIER_API_KEY
  taxonomy_version: v1
  lane_descriptions:
    support: Support requests
explicit_tag_pattern: '\[(?P<lane>[a-z0-9-]+)\]'
```

Omitted always-human settings default to empty user lists and a null owner.
Mentions must be exact Slack markup such as `<@U0000000093>`. Bare identifiers
and longer identifiers do not match. If text is absent, only author checks apply.
Both `mention_users` and the optional `owner_user` participate in mention checks.

Omitted classifier settings default to kind `none`, enabled `false`, empty model,
API key environment variable name `ANTHROPIC_API_KEY`, taxonomy version `v1`, and
empty lane descriptions. Enabled classification requires kind `anthropic` and a
nonempty model. Description keys must identify configured lanes. Loading config
never reads environment variables or credentials and never starts a client.

The default tag regex is shown above and must contain a named `lane` group.
Existing full-tag mappings such as `'[support]': support` remain supported.
Custom patterns can also map the captured name, for example `support: support`
with pattern `#(?P<lane>[a-z]+)`. Full-tag entries take precedence over captured
names. Unknown tags are skipped; the first recognized tag wins. Tags require
text. A body stored under `body` is not implicitly read as `text`.

DM defaults use conversation IDs, matching the scaffold sample. `im` and `mpim`
messages use only the DM table. A conversation explicitly in that table also
uses it if channel type is missing. A null default produces no match and permits
the person-channel rule to run. Person-channel rules use the validated
`PersonChannelRule` objects in configured order.

## Applying decisions

Call `apply_decision(store, message_id, decision, suggester)` after `route`.
Assigned decisions call `set_route`; excluded decisions call `set_state`.
Attention is persisted through the additive `set_attention` store method.
All other decisions enter `human_pending` before any classifier call. Only a
`none` decision may invoke the supplied suggester. Pass None or NullSuggester
when disabled. An adapter exposing `enabled=False` is also skipped. Legacy
Suggester implementations without that attribute are enabled when supplied.

A reply assigned by any rule binds its parent if no binding exists. Existing
bindings are preserved. Root messages do not establish a reply binding here.
Missing message IDs raise KeyError before mutations. Each store mutation uses
the ledger's transaction policy; this packet does not add cross-call atomicity.

Construct AnthropicSuggester only for enabled configuration, using model,
api_key_env, lane_descriptions as taxonomy, and taxonomy_version. The SDK import
and environment lookup happen only on suggest, and missing SDK support raises
a clear RuntimeError. The SDK is optional at runtime; this packet does not edit
package dependency metadata. Tests inject a zero-argument client_factory, which
bypasses SDK and credential access entirely.

The prompt lists lane descriptions and requests one JSON object with exactly
string `lane` and `reason` fields, with a one-sentence reason. Unknown lanes,
`none`, malformed JSON, duplicate keys, empty reasons, multiline reasons, extra
fields, and non-text or multiple content blocks produce no suggestion. The
parser does not attempt linguistic sentence counting. Stored version is
`taxonomy_version+p1`. SDK errors propagate, leaving the item in the human queue.
The adapter uses only `text` supplied by the store, with a body-unavailable
placeholder when absent. The default ledger read may omit text; this packet
never requests a privileged body read.

## Human queue

`list_pending(store, limit)` returns metadata, a suggested lane, a reason and
nonnegative age in seconds from the offset-aware received_at timestamp. Reason
uses suggestion_reason when supplied by the ledger, then route_reason, then
note. The contract does not prescribe a dedicated suggestion-reason column, so
its absence is tolerated. Invalid timestamps are not silently replaced.

`assign(store, message_id, lane, *, by: str, note: str | None)` requires
keyword-only `by` and `note` arguments with no defaults. Pass `note=None` when
no note is needed. It records `human:<by>`, preserves suggestions, and binds a reply's parent
to the selected lane, including an explicit human override of a prior binding.
Optional notes use set_state with state assigned. Lane validity is enforced by
the ledger. Reviewer identity must be provided by the caller.

`export_jsonl(store, path)` overwrites an adapter export with one object per
pending item containing only `id` and `suggested_lane`. Body, author, reason and
note are never exported. Because the ledger API has no offset cursor, exports
and statistics grow the list limit until all returned rows fit. This assumes a
stable finite view during the call, not a transactional historical snapshot.

`promotion_stats(store, channel)` reports judged, agreed, agreement and eligible.
Judged items are current rows with a `human:` route reason in that channel,
including acknowledged and completed work. Agreement compares the current
assigned lane to its stored suggestion. Missing suggestions count as disagreement.
At least 30 judged items and at least 90 percent agreement set eligible to true.
These are current-row statistics, not an immutable review history. Eligibility
never changes routing or enables automatic assignment. Promotion remains manual.

## Integration boundary

Tests exercise the consumed LedgerStore API through an in-memory fake and a fake
classifier client. The shadow-fix tests also exercise the real SQLite store, service source wrappers,
pipeline and timers with fake clients and clocks. No live SDK, credential, network
or deployment validation is claimed.

## P7A coverage

The pipeline applies owner signals before the existing router. When
always_human.owner_user is set, owner_signals.posts=record (the default) stores
owner-authored messages as done with owner_post reason/note, preserving body
policy and bindings without classification or notification. posts=route keeps
normal routing. An unset owner makes these policies inert.

For remaining reconcile messages, a configured done reaction by the owner wins
first, followed by a later owner reply in the fetched thread. Both result in
prehandled done rows without routing, classification, delivery or human notices.
Reconcile marks older non-owner replies and the parent using fetched owner-reply
evidence; a later owner reply already present in the ledger also qualifies.
Prehandled increments its own counter, not routed. Socket messages do
not apply the backfill filter. Existing router precedence follows these checks.
A backfill message that the router assigns is written to the lane file inbox
with kind backfill and does not reach the wake engine (see INGEST.md).

An owner reaction_added on a known row can mark new/assigned/acked/human_pending
as done for reactions_done (default white_check_mark, heavy_check_mark), or an
assigned row as acked for reactions_ack (default eyes). The note is
owner_reaction:<emoji>. Removed reactions and other actors do not change state;
reaction records remain available. Done matching takes precedence over ack if
an emoji appears in both lists.

## P13 ScopeClaim

`ScopeClaim` runs after `ThreadInherit` and before `ExplicitTag`. It routes only on
claims in the ledger table `scope_claims` with status `confirmed` and no `removed_at`
(the ledger-backed overrides written by the console `confirm` button or by `scope:auto`).
Proposals (`scope_proposals`) never route.

- The conversation id is looked up as a `dm` and as a `channel` claim (dm first in a
  direct conversation: channel type `im` or `mpim`, or a `D` id).
- A `person` claim matches the author only in a direct conversation; it never overrides
  routing in a shared channel.
- Claims naming a lane that is not configured are ignored.
- The reason is `scope:auto` for a claim applied automatically by `scope add`, else
  `scope_claim`; the decision rule is `scope_claim` in both cases.
- Excluded channels stay excluded. For bots under `route_or_exclude`, confirmed channel
  and dm claims act like channel defaults (checked before the config defaults).
- A confirmed claim wins over the config `channel_defaults` and `dm_defaults`. After
  pasting an exported fragment into the config the claim can stay; `scope remove` drops
  it when the config should be the only source again.

`route(message, config, lookup_binding, lookup_scope=None)`: without `lookup_scope` the
engine uses `lookup_scope_claim` of the store behind a bound `store.lookup_binding`, so
the pipeline call is unchanged. Plain callables and test fakes have no such store, so
ScopeClaim is inactive for them.

`assign` and `reroute` (CLI, console and card decisions) point the message's thread
binding (thread_ts, else ts) at the new lane, overriding an earlier binding, so later
replies follow the item (first-day defect, 2026-11-13).

## P15 membership gate and ledger channel exclusions

Before routing, the pipeline applies the membership gate (see INGEST.md): under
`membership.policy: exclude` with a loaded cache, a message from a channel that is not
a member channel is set to `excluded` with note `excluded:not_member`; `route` is not
called and nothing is queued or delivered.

Ledger channel exclusions (`channel_exclusions`, written by the console `Exclude
channel` action) are read at runtime: the pipeline passes `route` a config whose
`excluded_channels` is the config list plus every active ledger exclusion
(`pipeline.routing_config(config, store)`), so they behave exactly like
`excluded_channels`: precedence 1, rule `excluded`, note `excluded`, no attention, no
root hydration, and reconcile does not scan them. No YAML edit is needed and the
override survives restarts; `undo` ends it. Stores without the lookup add nothing.

## P16 bot flag and user directory

One rule, `slack_dispatch.ingest.user_directory.is_bot_message(event, config,
directory=None)`: a message is a bot message iff (a) it has no `user` (ledger rows:
`author`), (b) `subtype == "bot_message"`, (c) the author is in
`bot_messages.known_bot_users`, or (d) a directory is given and says the author is a
bot. `bot_id`, `app_id` and `bot_profile` by themselves no longer mark a bot.

- The pipeline decides the flag in one place (`Pipeline.is_bot`) for socket and
  reconcile events and stores it in `messages.is_bot`. Sources no longer set it. A
  caller-supplied `is_bot: True` is kept. The directory is consulted only for message
  events that carry `bot_id` or `app_id`, so a human posting by hand never costs a
  lookup.
- `route()` uses `message["is_bot"]` when present (events and ledger rows carry it) and
  otherwise the rule without a directory. The router never calls Slack.
- With `user_directory.enabled: false`, or without a reconcile source, the rule is
  (a)(b)(c) only. `bot_messages.policy` and `known_bot_users` are unchanged.
- Consequence: a human post through an app gets the attention check (owner mentions,
  protected authors) and, in a channel without a default lane, goes to the human queue
  instead of `excluded:bot`. A bot user's post keeps the bot policy.

### Repair command

`python -m slack_dispatch.inbox.cli repair bot-flag [--apply] [--since-days N]
[--assume-human] [--json]` (config required: `--config` or `SLACK_DISPATCH_CONFIG`;
default dry run; `--since-days` default 30). It stays offline: it reads only the
ledger (including the `user_directory` table) and the config.

Candidates are ledger rows with `is_bot = 1`, a non-null author and `received_at`
within the window whose author is not a bot by (c) or (d) as recorded in the
`user_directory` table. Authors without a table row are listed as `skip:unknown` and
skipped unless `--assume-human`. Context rows and owner posts are refused with the
`inbox_refusal` rule that `assign` and `cutover` use (`refused:context`,
`refused:owner_post`), and rows by `always_human.owner_user` are listed as
`refused:owner`; refused rows are not changed. Rule (b) cannot be checked offline
because the ledger does not store the subtype.

The dry run prints one line per candidate: id, author, state, lane, note and the
planned action (`reroute`, `attention`, `fix`, `skip:unknown`, `refused:<reason>`), then
the counts. `--apply` per row:

- `excluded` with note `excluded:bot`: routed again with the current config, ledger
  channel exclusions, bindings and confirmed scope claims as if newly received (no
  classifier). An assigned lane gets a `backfill` record in its inbox (the cutover
  mechanism, no wake) and a new root binds its thread; a human pending item or an
  attention flag queues the usual human notice, and a human pending item rewrites the
  human queue export. Result `rerouted:<lane>` or `rerouted:human`.
- `assigned` or `acked`: attention is recomputed from the author and body mentions;
  when it becomes set the row is updated and the usual immediate human notice is
  queued. Result `attention:<flag>`, else `fixed`.
- other states (`done`, `human_pending`, `new`): the flag only. Result `fixed`.

The flag is cleared after delivery, so a failed inbox append leaves the row unchanged
for a rerun; that row prints `error` and the exit code is 1. Human notices are queued as
`human_notices` rows; the running service's next flush sends them (immediate rows at
once, others with the digest; a notice for an `acked` row is dropped by the flush as
usual). Without a body in the ledger (`ledger.store_body: false`) only author checks
apply to attention. `--json` prints `{"mode", "since_days", "rows", "counts"}`.

## P17 owner resolver and registry sync

`slack_dispatch.resolver` answers "who owns this channel or thread?" from local evidence:
the ledger, the config, the session thread registry JSON (`owner_resolver.registry_path`)
and an optional scope harvest jsonl (`owner_resolver.harvest_path`). It never calls Slack,
a model or the network, and its answers are suggestions only: routing (`route()`) is
unchanged and never reads the registry.

Candidates, in order (`resolve(store, config, registry, channel, ts=None,
thread_ts=None)`; the root is `thread_ts`, else `ts`):

1. `certain`: the ledger binding of (channel, root) (`binding`); a confirmed scope claim
   for the thread key `CHANNEL:ROOT`, the channel or the dm (`scope_claim`); the config
   channel or dm default (`channel_default`, `dm_default`; a null default adds nothing).
2. `declared`: a registry row for (channel, root) whose session ids map to configured
   lanes (`registry`); when no row has that root, rows of the conversation without a
   root (flat channel or DM rows, `registry_channel`); a registry `_meta.sweep_scope*`
   entry that lists the conversation (`sweep_scope`); a registry routing rule whose text
   names the conversation id and a session id (`routing_rule`).
3. `recorded`: a registry row with an owner text but no session id (lane none; the
   evidence carries the owner text, title and status).
4. `observed`: lanes that acked or completed ledger items of the conversation within
   `owner_resolver.activity_days` (`activity`, by count), then the harvest file
   aggregated per lane with strong and weak file counts (`harvest`). Only configured
   lanes count.

A session id maps to the lane whose `lanes[].session` starts with it (8 hex prefix).
Unknown session ids stay candidates with lane none (shown as `session:<8hex>`).
`Resolution.best` is the first candidate with a lane. Evidence is at most 160
characters and never contains a ledger message body.

Registry reading. The registry is parsed once per file version (mtime and size) and the
file is checked at most every `owner_resolver.refresh_seconds`. A parse failure keeps the
previous index and logs one WARNING with the exception class only. Field aliases: channel
under `ch`, `channel_id` or `channel` (the first token that starts with a conversation id;
a name-only value means no channel), root under `root_ts` or `thread_ts` (`-`, missing or
not `digits.digits` means no root). Duplicate (channel, root) rows are all kept; the row
with the newest `last_ts`, then the later one in the file, speaks for status. A row is
closed when its status (lower case) is in `owner_resolver.closed_statuses`; `last_ts` is
the largest Slack ts among `last_out_ts`, `last_in_ts` and `last_event`.

Suggestion hook. When a message stays `human_pending` and `owner_resolver.enabled`, the
resolver runs first; a best lane is stored with `set_suggestion(mid, lane, "<source>:
<evidence>", "owner-resolver/1")` (one line, at most 200 characters). The classifier, if
enabled, is asked only when the resolver found no lane, and only for rule `none` as
before; queued `always_human` items get the resolver suggestion too. A resolver failure is
logged by class and the item is queued as usual.

Owner reply close. With `owner_resolver.enabled` and `close_queue_on_owner_reply: true`,
an owner post (`always_human.owner_user`, recorded as `owner_post`) that is a thread reply
sets every `human_pending` item of that thread with a lower ts to `done` with note
`owner_reply`, drops their unsent human notices (a pending digest no longer lists them)
and rewrites the queue export. Assigned and acked lane items are not touched; the lane
inbox shows `owner replied at`. Heartbeat counter `owner_reply_closed` when the resolver
is enabled.
