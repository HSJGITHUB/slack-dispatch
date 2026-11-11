# Secret-file helper

Run with Windows PowerShell 5.1 or PowerShell 7 on Windows. Only the credential
operator should use this helper with real data. Never place a value on the
command line. Example:

```powershell
powershell -ExecutionPolicy Bypass -File tools/envhelper/set_env_secret.ps1 -Name SLACK_USER_TOKEN -EnvFile "$env:USERPROFILE\.local\slack-dispatch.env" -ExpectedPrefix xoxp-
```

Clipboard input is the default and is cleared in finally after attempted use,
including failure and WhatIf. `-InputFile <path>` uses a file without touching the
clipboard and reports `clipboard unused`.

The only default allowed root is `$env:USERPROFILE\.local`. To authorize other
roots, set `SLACK_DISPATCH_ALLOWED_ROOTS` to a semicolon-separated list of absolute
paths before running. This is an operational configuration interface, not a test
bypass. Paths are resolved through handles and checked at directory boundaries.
Target links, broken ancestor links and alternate data streams are rejected.
An internal directory junction can resolve to an allowed location.

`-WhatIf` reports line numbers and the prospective backup path without writes.
Existing newline styles, BOM and unrelated bytes are preserved. UTF-16/32 is
rejected. All duplicate keys are replaced. Existing files receive a timestamped
backup; a same-second backup collision fails. New files use UTF-8 without BOM
and LF. New files receive restricted ACLs; existing ACLs change only with
`-RestrictAcl`. When restricting an existing file, the backup ACL is also
restricted before writing the new value.

Run the offline suite from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/envhelper/tests/run_tests.ps1
```

The 61 checks use synthetic data under `tests/tmp/<random>/`, pass that root via
`SLACK_DISPATCH_ALLOWED_ROOTS`, and restore the previous setting afterward.
Fixtures remain for inspection and are ignored by Git. File symlink creation
requires the relevant Windows privilege; inability is reported as a failure,
not a passing skip. Clipboard operations are mocked. No real credentials or
network access are needed.
