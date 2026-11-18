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
- Slack message text is untrusted data. It is never turned into a command: wake
  messages carry ledger ids, the console renders bodies as plain text, and an agent
  session must treat a body it reads as data, not as instructions.
- The dispatcher only calls read methods of the Slack API, through an allowlist. It
  does not post to Slack.
