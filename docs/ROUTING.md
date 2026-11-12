# Routing and human review

The router consumes a normalized message and a validated Config. It does not
mutate the message, call a classifier, or write to the ledger. First match wins:

1. Excluded channel: excluded state, no suggestion.
2. Always-human author or configured mention: human queue, no suggestion.
3. Existing reply thread binding.
4. First recognized explicit tag in message text.
5. Channel or DM default, unless the configured value is null.
6. First matching person and channel rule.
7. No match: human queue, optionally with a classifier suggestion.

`Decision(lane, reason, rule)` identifies the selected rule. Deterministic reasons
are stable rule names, never copies of message text. Author matching accepts the
normalized `user` field, or the ledger `author` field if `user` is absent. Reply
identity is derived from a nonempty `thread_ts` different from `ts`.

## Configuration

All original required scaffold keys remain required. The three new keys are
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
classifier client. Ledger, ingest, service, inbox and notifier implementation are
owned by other packets and are unchanged. No live SDK, credential, network or
service integration validation is claimed.
