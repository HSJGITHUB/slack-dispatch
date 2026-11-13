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
proposals. A bot is identified by is_bot, bot_id, a missing author, or membership
in bot_messages.known_bot_users. Channel defaults for bots use the existing DM
semantics and override explicit tags and thread bindings.

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
