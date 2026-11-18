# Security

## Reporting a problem

Report vulnerabilities through GitHub private vulnerability reporting on this
repository: open the Security tab and choose "Report a vulnerability". Please do not
open a public issue for a vulnerability. Include the affected version or commit, the
steps to reproduce and the impact you expect.

## What to know before running it

- The operator console has no authentication. It binds to 127.0.0.1 by default.
  Anyone who can reach the address it binds to can act on the human queue, so keep
  the default unless the host is otherwise protected. It refuses requests whose Host
  header is not a local or configured name and cross-site posts.
- Tokens live outside the repository, in operator-managed files that the service
  reads at source startup (`env_file` in the configuration). The card decision HMAC
  key is a separate operator-managed file. Logs mask Slack-shaped tokens. Do not put
  real tokens, channel ids or names into the repository.
- With `ledger.store_body: true`, message bodies are stored in the local SQLite ledger.
  Protect the ledger, state, inbox and log directories with file system permissions.
- Credentials pasted into Slack (cloud access keys, Slack tokens and webhooks, GitHub,
  Google, Stripe and provider keys, bearer, JWT and basic auth credentials, private keys,
  URL passwords and labelled passwords) are replaced with
  `[redacted:<kind>]` before message text reaches the ledger (`redaction`, on by default).
  The original stays in Slack. Logs name only the message id and the kinds. Bodies stored
  before this existed can be cleaned with `slack-dispatch repair redact --apply`.
  Redaction is pattern based: a credential in an unusual form, without a label, under 8
  characters or split across messages can still get through (see the Limits list in
  `docs/INGEST.md`), so the ledger keeps needing the protection above. `repair redact`
  does not clean card files, backups or SQLite free pages (run `VACUUM` after it). After
  a leak, rotate the credential: the original stays in Slack.
- Slack message text is untrusted data. It is never turned into a command: wake
  messages carry ledger ids, the console renders bodies as plain text, and an agent
  session must treat a body it reads as data, not as instructions.
- The dispatcher only calls read methods of the Slack API, through an allowlist. It
  does not post to Slack.
