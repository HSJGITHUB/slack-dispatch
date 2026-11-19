# Routing and human review

The router consumes a normalized message and a validated Config. It does not
mutate the message, call a classifier, or write to the ledger.

| Precedence | Condition | Result |
| --- | --- | --- |
| 1 | Excluded channel | Excluded, no attention or classifier |
| 2 | Bot with exclude_all | Excluded with excluded:bot |
| 2 | Bot with route_or_exclude | Channel/DM default only, otherwise excluded:bot (always excluded:bot in a mention-only channel, P26) |
| 3 | Protected author, configured exact mention or configured operator name (P36) | Set attention, continue routing |
| 4 | Mention-only channel (P26) without attention, or in owner mode (P30) without `owner_mention`, not a reply in a bound thread | Excluded with excluded:no_mention |
| 5 | Existing reply thread binding | Assign inherited lane |
| 6 | Confirmed scope claim in the ledger (P13 ScopeClaim) | Assign claimed lane |
| 7 | First recognized explicit tag | Assign tagged lane |
| 8 | Non-null channel or DM default | Assign default lane |
| 9 | First person-channel rule | Assign rule lane |
| 10 | No lane with attention | Human pending, no classifier |
| 11 | No lane without attention | Human pending, optional classifier suggestion |

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

The base keys the loader requires (such as `lanes`, `timers`, `wake_policy` and
`notify_adapters`) stay required. The routing keys are optional, including when
loading older configuration files:

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
The optional `mention_texts` list (P36, below) adds plain-text operator names.

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

DM defaults use conversation IDs, as in the sample configuration. `im` and `mpim`
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
the ledger's transaction policy; applying one decision is not a single atomic
transaction across those calls.

Construct AnthropicSuggester only for enabled configuration, using model,
api_key_env, lane_descriptions as taxonomy, and taxonomy_version. The SDK import
and environment lookup happen only on suggest, and missing SDK support raises
a clear RuntimeError. The SDK is optional at runtime and is not a declared
package dependency; install `anthropic` to enable the classifier. Tests inject a zero-argument client_factory, which
bypasses SDK and credential access entirely.

The prompt lists lane descriptions and requests one JSON object with exactly
string `lane` and `reason` fields, with a one-sentence reason. Unknown lanes,
`none`, malformed JSON, duplicate keys, empty reasons, multiline reasons, extra
fields, and non-text or multiple content blocks produce no suggestion. The
parser does not attempt linguistic sentence counting. Stored version is
`taxonomy_version+p1`. SDK errors propagate, leaving the item in the human queue.
The adapter uses only `text` supplied by the store, with a body-unavailable
placeholder when absent. The default ledger read may omit text; the adapter
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

Since P24 that applies to thread roots only. A `reroute` of a reply moves the reply and
leaves the binding, so later replies keep following the thread's lane (a reroute of one
reply had moved a whole thread owned by another lane, 2026-11-16). `reroute --thread`
(CLI) or `"thread": true` in the console lane action body moves every open item of the
thread in the old lane and the binding. `assign` of a reply binds only a thread without a binding.
Edits never change a binding: an edit of an item that already has a lane is not routed
again (SERVICE.md, P24), so thread inherit cannot pull a rerouted item back.

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
and rewrites the queue export. Assigned and acked items of other lanes are not touched
(the lane inbox shows `owner replied at`); items of the direct lane are closed too, see
SERVICE.md P27. Heartbeat counter `owner_reply_closed` when the resolver
is enabled.

## P26 mention-only channels

Some channels belong to other teams: the operator wants to see a post there only when it
mentions them. A `channel_defaults` entry takes an optional `mention_only` value next to
`default`: `true`, `false` (the default) or, since P30, the string `owner` (owner mode,
see below):

```yaml
channel_defaults:
  C0000000004: {default: owner, mention_only: true}
  C0000000006: {default: owner, mention_only: owner}
```

The loader rejects any other value (`must be true, false or owner`) and rejects the key in
`dm_defaults`. `Config.channel_defaults` keeps its `{channel: lane}` shape; the ids of every
mention-only channel (both modes) are `Config.mention_only_channels` and the ids of the
owner mode channels (a subset) are `Config.mention_only_owner_channels` (tuples, empty by
default).

Owner mode (P30) is for reference rooms where protected authors post routinely to other
people: only attention `owner_mention` opens the channel. A message whose attention is not
`owner_mention` (an `always_human.users` author without an owner mention included) is
treated by the gate exactly like a message without attention.

In `route()`, after the excluded channel check, the bot policy and the attention check
(precedence 4 above), a message in a mention-only channel:

- with attention (an exact mention of `always_human.owner_user` or a
  `always_human.mention_users` id, or a configured operator name (P36): `owner_mention`;
  an `always_human.users` author: `always_human`) continues through the normal
  precedence unchanged: thread binding, scope claim, explicit tag, channel default,
  person rule. With `{default: <lane>}` it is
  assigned to that lane with the attention flag (and the usual immediate human notice);
  with `{default: null}` and no other match it is a human pending `always_human` item.
  This holds for `mention_only: true`. In owner mode only `owner_mention` continues this
  way; `always_human` alone falls under the two bullets below, so outside a bound thread
  it is excluded.
- without attention (in owner mode: without `owner_mention`), as a reply in a thread that
  already has a binding, inherits the bound lane (`thread_inherit`) exactly as today and
  keeps its attention value (in owner mode a protected author's bound reply is assigned
  with `always_human`). A mention post assigned to a lane binds its thread (roots by the
  pipeline, replies by `apply_decision`), so the later replies of that thread reach the
  lane.
- without attention (in owner mode: without `owner_mention`) otherwise (a root, or a
  reply in an unbound thread) gets
  `Decision(None, "excluded:no_mention", "excluded")`. Explicit tags, scope claims, the
  channel default and person rules do not apply. The pipeline records it as `excluded`
  with note `excluded:no_mention`; it is never assigned, queued or counted as
  `human_pending`, and it gets no human notice, no inbox record and no wake. The
  heartbeat counter `excluded_no_mention` (present once nonzero) counts these. Like the
  other router exclusions it still counts in `routed`.

Operator posts (P30): a post by `always_human.owner_user` recorded as an owner post
(`owner_signals.posts: record`, live or reconcile origin) in a mention-only channel of
either mode binds its thread (the post itself when it is a root, else its thread root) to
the channel's `channel_defaults` lane when that thread has no binding. An existing binding
is never changed, a null default binds nothing, and nothing is bound in an excluded channel
(config `excluded_channels` or a ledger channel exclusion). The owner post itself stays
`done` with `owner_post` (no inbox record, wake or notice). Later replies in that thread
pass the gate by inheritance and reach that lane like any bound-thread reply. Outside
mention-only channels nothing changes. The heartbeat counter `owner_threads_bound`
(present once nonzero) counts these bindings.

Bots: under `bot_messages.policy: route_or_exclude` a bot post in a mention-only channel
is `excluded:bot` (bots carry no attention under that policy, so neither the channel
default nor a channel scope claim routes it). Under `exclude_all` nothing changes; under
`route_all` bot posts take the human path above.

The console search view shows the note `excluded:no_mention` as text in its note column.

## Operator name mentions (P36)

People often address the operator by typing a name instead of an `@` mention. The optional
`always_human.mention_texts` list makes such a post count as an exact owner mention:

```yaml
always_human:
  owner_user: U0000000093
  mention_texts: [Operator Kim, 운영자님]
```

The loader accepts a list of at most 20 strings, each at least 2 characters after trimming
(entries are stored trimmed); anything else raises `ConfigError`. The default is an empty
list, which keeps the exact-only behaviour.

`attention_for` returns `owner_mention` when the message text contains a configured name:

- Both sides are compared in one form: Unicode NFKC (full-width letters and spaces become
  their ASCII forms), case folded, every run of whitespace collapsed to one space.
- Text inside a Slack entity (`<...>`: a link and its label, a channel reference, a user
  mention with its label, a broadcast or user group) never matches, and a name does not
  match across an entity. The Slack escapes `&lt;`, `&gt;` and `&amp;` are decoded in the
  text outside entities.
- A name made only of ASCII characters must not touch an ASCII letter or digit on a side
  where the name itself starts or ends with one: `Sam` does not match `sample` or `Asam`,
  but matches `Sam,`, `(Sam)` and `Sam` followed by a Korean honorific. A name containing
  a non-ASCII character (for example a Korean name with an honorific) matches as a plain
  substring.
- Matching is plain substring search (`str.find`) over the text, linear in its length;
  no regex is built from the configured strings.

The result is the same `owner_mention` an exact `<@owner_user>` gives: it opens
mention-only channels of both modes (P26, P30), routes by the normal precedence (the
channel default lane in a mention-only channel), binds the thread, follows
`human_queue.immediate_flags` and `assigned_notice_flags`, and shows the attention badge
in the inbox and the console. Posts by `always_human.owner_user` are still recorded as
owner posts before routing, and bot posts carry no attention under `route_or_exclude` and
`exclude_all`. The matched text is the redacted text (P35).

Edits keep the P22 and P24 edit semantics, the same as for an edit that adds an `@`
mention. Since P37 an edit that adds the name (or an exact mention) to an
`excluded:no_mention` post is routed as a first assignment under every
`wake_policy.followup` value (see "Edit handling (P37)"). Before P37 it was recorded only
under `changed_after_ack` and `never` and routed again as an edit wake under `always`.

The heartbeat counter `owner_name_mentions` (present once nonzero) counts routing
decisions whose `owner_mention` came from a configured name and not from an exact mention
(an edit routed again counts again).

Wake debounce (P36 fix1): the pipeline hands the wake engine the attention of the routing
decision it just made, and a message with attention `owner_mention` (an exact mention of
`always_human.owner_user` or a `always_human.mention_users` id, or a configured operator
name) bypasses the same-author debounce (`wake_policy.debounce_seconds`) exactly like the
existing triggers (`wake_policy.immediate_users`, an exact `<@wake_policy.owner_user>`
mention or a `mentions` id). Attention `always_human` and no attention are debounced as
before; the storm guard and the global wake switch still apply to every message. A wake
request served by the service (console wake, P21) offers the stored ledger row, so its
stored `owner_mention` flag bypasses the debounce there too.

## Edit handling (P37)

Three additions to the P22 and P24 edit rules (SERVICE.md). Nothing changes for an edit
that does not meet them.

### Cosmetic edits (`wake_policy.followup_ignore_cosmetic`)

Optional boolean, default `false` (today's behaviour). With `true`, an edit of an item
that holds a lane (state `assigned`, `acked` or `done` with a lane) that would otherwise be
re-delivered as a followup is recorded only (like an ignored edit: ledger body and
`edited_at` updated, no inbox record, wake or notice; counted in `edits_ignored` and in the
heartbeat counter `edits_cosmetic`) when the edit changed decoration only. The rule is
token based and conservative (fix1): when in doubt, an edit is not cosmetic.

- The set of Slack entities is unchanged: user and channel references (`<@U...>`,
  `<#C...>`), broadcasts (`<!here>`, `<!subteam^...>`) and links, by target; labels are
  ignored.
- The rest of the text (NFKC, case folded) is split on whitespace and each token is
  classified:
  - sentence punctuation token: only `. , ! ? ~ ; …` characters (decoration);
  - emoticon token: no letter and no digit, and at least one of `( ) [ ] { } ^`, for
    example `=)`, `:)`, `:-(`, `^^`, `(^_^)` (decoration);
  - emoji token: only emoji characters (with joiners, variation selectors and skin tone
    modifiers) and/or shortcode groups (`:smile:`, `:+1:`, `:wave::skin-tone-2:`);
  - every other token is a content token: anything with a letter or digit, mixed tokens
    and operators or symbols standing alone (`>`, `<`, `>=`, `=`, `+`, `-`, `->`, `/`,
    `%`, `&`, `|`, `#`, `$`).
- A content token is compared in its canonical form: `. , ! ? ~ ; … : ' " ( ) [ ]` are
  stripped from its ends only while the character next to them inside the token is not a
  digit; nothing inside the token is removed. `Done.` equals `done!!!`; `1.5` and `15`,
  `5.` and `5`, `don't` and `dont` differ.
- The canonical content tokens of both texts must be the same in the same order, and every
  emoji token of the old text must still be in the new text. Sentence punctuation and
  emoticon tokens may be added, removed or changed freely.

Limits, stated plainly: any wording change re-delivers (a changed, added, removed, joined or
split word, a changed number, an added negation, an added or removed operator such as `-`,
`->` or `/`); emoji may be added but never removed or swapped (`:+1:` to `:-1:`, `:yes:`
to `:no:` and a removed emoji re-deliver); punctuation next to a digit counts as content
(`5.` to `5` re-delivers). Emoji or emoticons glued to a word (`done:)`, `ok:smile:`) make
a different content token and re-deliver. Both sides are first reduced to the P24
comparable form, so a body rendered from rich text blocks and the raw mrkdwn of the same
message compare equal (a link is its URL either way); that form also drops a line-leading
`>` quote marker. A missing stored body (`ledger.store_body` off) is never cosmetic. The
check is one linear pass over each text.

The option applies under every `wake_policy.followup` value, but only where an edit would
re-deliver an item that holds a lane: an acked or done item under `changed_after_ack`,
any such item under `always`. Edits that collapse whitespace only, or add or remove only a
broadcast, were never text changes under the P24 comparison and stay ignored without the
option too.

### An edit that adds an owner mention to an excluded post

When a post was excluded with note `excluded:no_mention` (a mention-only channel, P26 and
P30) and an edit gives it attention `owner_mention` (an exact mention of
`always_human.owner_user` or a `mention_users` id, or a configured
`always_human.mention_texts` name, P36) that the stored text before the edit did not give,
the edit is routed as a first assignment (the P22 `EDIT_FIRST` path), under every
`wake_policy.followup` value: the normal routing order (the channel default lane in a
mention-only channel), attention, an inbox record, a wake offer (the owner mention bypasses
the debounce), the thread binding and the human notice per
`human_queue.assigned_notice_flags`, exactly as if the post had arrived with the mention.
It counts in the heartbeat counter `edits_mention_added`, not in `edits_woken`. Like every
`EDIT_FIRST` record the inbox record has kind `followup` (the message was edited) and the
inbox view shows it as kind `new`, edited `yes`; it carries no `edit_delta`. The ledger
note is not cleared by this first-assignment path: the assigned row keeps note
`excluded:no_mention` (`apply_decision` and `set_route` set state, lane and route reason
only; a test pins this). `edits_mention_added` counts only edits whose routing ended in an
assignment (fix1); an edit that ends elsewhere (an operator's own edit closed as an owner
post, or a human queue item in a channel without a default lane) is not counted.

Without a stored body (`ledger.store_body` off) the `excluded:no_mention` note stands for
the old text. The edit must not be a bot post and the channel must not be excluded now.
Once assigned, the item holds a lane, so later edits follow the P22 and P24 rules for such
items (a second identical edit does nothing under `changed_after_ack` and `never`; under
`always` every edit of an item that holds a lane re-delivers a followup, as before).

Other exclusions are never routed again by an edit, under any policy: an excluded channel
(`excluded`), a bot (`excluded:bot`), a channel the operator is not a member of
(`excluded:not_member`) and an operator exclusion (`human:console` and the like). Before
P37 `always` routed such an edit again. An `excluded:no_mention` post edited without
gaining an owner mention keeps the P22 behaviour (recorded only, or routed again and still
excluded under `always`). An edit that removes a mention never excludes an item or moves
it out of its lane.

### Edit size hint

A followup inbox record written for a re-delivered edit (an `EDIT_WAKE`: the P24
re-delivery of an item that holds a lane, or the routing path under `always`) carries
`edit_delta: {"added": n, "removed": m}`, the whitespace-separated tokens present in the
new text but not the old and in the old but not the new (multiset difference). Each Slack
entity counts as one token by its target (labels ignored); both sides use the P24
comparable form. No token text is stored. Without both texts (no stored body) the field is
left out. `inbox <lane>` shows it as `edit +n -m` (see INBOX.md).
