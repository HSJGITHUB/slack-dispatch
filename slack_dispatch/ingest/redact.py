"""Credential redaction before message text reaches the ledger (P35).

`redact(text)` is pure (no I/O, no logging) and replaces every high-confidence credential
with `[redacted:<kind>]`. Every pattern runs in time linear in the input: quantifiers are
either bounded or followed by a character class they cannot overlap, every pattern that
could start inside a long run has a lookbehind that admits only the start of the run, and a
PEM block end is found by one forward search from its BEGIN line. Values are never logged
or returned anywhere except in the redacted text itself (which no longer holds them).
"""
from bisect import bisect_right
from collections import Counter
import re

from slack_dispatch.config.schema import REDACTION_KINDS

KINDS = REDACTION_KINDS
MARK = "[redacted:"
_MARKER = re.compile(r"\[redacted:([a-z_]{1,40})\]")


def placeholder(kind):
    return MARK + kind + "]"


def is_marker(value):
    """True when `value` is exactly one placeholder of a known kind (fix1: a placeholder
    followed by more characters is not skipped, so nothing can hide behind one)."""
    match = _MARKER.fullmatch(value)
    return match is not None and match.group(1) in KINDS


# A PEM private key block: BEGIN line to the next END line of a private key (or the end of
# the text when the END line is missing). The bounded type words cover RSA, EC, DSA,
# OPENSSH and ENCRYPTED keys.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----")
_AWS_ID = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AIDA|AROA|AGPA)[A-Z0-9]{16}(?![A-Za-z0-9])")
_SLACK = re.compile(r"(?<![A-Za-z0-9])(?:xox[abprsec]-|xapp-[0-9]-)[A-Za-z0-9-]{10,}")
# A Slack incoming webhook: the path after the fixed prefix is replaced, the host stays.
_SLACK_WEBHOOK = re.compile(r"(?<=hooks\.slack\.com/services/)[A-Za-z0-9_/-]{10,}")
_GITHUB = re.compile(r"(?<![A-Za-z0-9_])(?:gh[pous]_|github_pat_)[A-Za-z0-9_]{20,}")
_GOOGLE = re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")
_STRIPE = re.compile(r"(?<![A-Za-z0-9_])(?:sk_live_|rk_live_|sk_test_)[A-Za-z0-9]{16,}")
_API_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")
# A JSON web token: three base64url segments, the first two starting with `eyJ`.
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}"
                  r"\.[A-Za-z0-9_-]{8,}")
# `Bearer <token>` (any case): the token needs a digit, so prose such as "Bearer
# responsibilities" (16 letters) stays. Only the token is replaced; the word stays.
_BEARER = re.compile(r"(?<![A-Za-z0-9])((?i:bearer)[ \t]+)(?=[A-Za-z._~+/=-]*[0-9])"
                     r"[A-Za-z0-9._~+/=-]{16,}")
# `Authorization: Basic <base64>`: only the credentials are replaced.
_BASIC = re.compile(r"((?i:authorization)[ \t]*:[ \t]*(?i:basic)[ \t]+)"
                    r"[A-Za-z0-9+/]{16,}={0,2}")
# `scheme://user:password@host`: only the password is replaced.
_URL_PASSWORD = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,30}://"
                           r"[^\s:/@<>|]{1,256}:)([^\s/@<>|]{1,256})(?=@)")
# An AWS secret access key: 40 base64-like characters, as a whole token.
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")
# The word `secret`, except inside this module's own placeholders (idempotence).
_SECRET_WORD = re.compile(r"(?i)(?<!redacted:aws_)(?<!redacted:labelled_)secret")
_NEWLINE = re.compile(r"\n")
# fix1: a label is a credential word at the tail of an identifier: after a non-letter
# (`password`, `AWS_SESSION_TOKEN`, `x-api-key`, `*password*`), at a camelCase boundary
# (`sessionToken`, `clientSecret`, `myApiKey`) or after upper-case letters in upper case
# (`GITHUBTOKEN`), or a Korean label.
_LABEL_WORDS = (r"password|passwd|passphrase|secret(?:[ _-]?key)?|api[ _-]?key"
                r"|access[ _-]?key|private[ _-]?key|token")
_LABEL = (r"(?:(?<![A-Za-z])(?i:" + _LABEL_WORDS + r")"
          r"|(?<=[a-z0-9])(?:P(?i:ass(?:word|wd|phrase)|rivate[ _-]?key)|S(?i:ecret(?:[ _-]?key)?)"
          r"|A(?i:pi[ _-]?key|ccess[ _-]?key)|T(?i:oken))"
          r"|(?<=[A-Z0-9])(?:PASSWORD|PASSWD|PASSPHRASE|SECRET(?:[ _-]?KEY)?|API[ _-]?KEY"
          r"|ACCESS[ _-]?KEY|PRIVATE[ _-]?KEY|TOKEN)"
          r"|비밀번호|비번|암호|패스워드)")
# Then optional emphasis or a closing quote around the label (`*password*:`, `_token_ =`,
# `"password":`), `:` or `=`, optional emphasis after it (`*password:* value`), any
# horizontal Unicode space (a non-breaking space too) and the value. A value never starts
# at `<` and stops before a Slack mention, channel, broadcast or link (`<@`, `<#`, `<!`,
# `<http`), so those are never part of a value.
_LABELLED = re.compile(_LABEL + r"[ \t_-]*(?:[*_~`\"']{1,3}[ \t]*)?[:=](?:[*_~`]{1,3})?"
                       r"[^\S\r\n]*(?P<value>[^\s<](?:[^\s<]|<(?![@#!]|http))*)")
_OPEN_QUOTES = "\"'`*~"
_TRAILING = "\"'`,;>*~"
LABELLED_MIN = 8


def _pem(text, counts):
    parts, position = [], 0
    while True:
        begin = _PEM_BEGIN.search(text, position)
        if begin is None:
            break
        end = _PEM_END.search(text, begin.end())
        parts.append(text[position:begin.start()])
        parts.append(placeholder("private_key"))
        counts["private_key"] += 1
        if end is None:
            position = len(text)
            break
        position = end.end()
    if not parts:
        return text
    parts.append(text[position:])
    return "".join(parts)


def _whole(kind, pattern):
    def apply(text, counts):
        def replace(match):
            counts[kind] += 1
            return placeholder(kind)
        return pattern.sub(replace, text)
    return apply


def _keep_prefix(kind, pattern):
    """Replace the match after its first group (the kept prefix, such as `Bearer `)."""
    def apply(text, counts):
        def replace(match):
            counts[kind] += 1
            return match.group(1) + placeholder(kind)
        return pattern.sub(replace, text)
    return apply


def _url_password(text, counts):
    def replace(match):
        if is_marker(match.group(2)):
            return match.group(0)
        counts["url_password"] += 1
        return match.group(1) + placeholder("url_password")
    return _URL_PASSWORD.sub(replace, text)


def _aws_secret(text, counts, paired):
    """Every 40-character candidate when the original text holds an AWS access key id,
    else only candidates with the word `secret` earlier on the same line."""
    if paired:
        return _whole("aws_secret_access_key", _AWS_SECRET)(text, counts)
    candidates = list(_AWS_SECRET.finditer(text))
    if not candidates:
        return text
    label_ends = [match.end() for match in _SECRET_WORD.finditer(text)]
    if not label_ends:
        return text
    breaks = [match.start() for match in _NEWLINE.finditer(text)]
    parts, position = [], 0
    for match in candidates:
        start = match.start()
        label = bisect_right(label_ends, start) - 1
        line = bisect_right(breaks, start) - 1
        if label < 0 or label_ends[label] <= (breaks[line] if line >= 0 else -1):
            continue
        parts.append(text[position:start])
        parts.append(placeholder("aws_secret_access_key"))
        counts["aws_secret_access_key"] += 1
        position = match.end()
    if not parts:
        return text
    parts.append(text[position:])
    return "".join(parts)


def _labelled(text, counts):
    def replace(match):
        value = match.group("value")
        close = value.find(value[0], 1) if value[0] in _OPEN_QUOTES else -1
        if close > LABELLED_MIN:
            start, stop = 1, close  # a wrapped value keeps its wrapper: "<value>"},
        else:
            start, stop = 0, len(value)
            while stop > 0 and value[stop - 1] in _TRAILING:
                stop -= 1
        core = value[start:stop]
        if len(core) < LABELLED_MIN or is_marker(core):
            return match.group(0)
        counts["labelled_secret"] += 1
        head = match.group(0)[:match.start("value") - match.start(0)]
        return head + value[:start] + placeholder("labelled_secret") + value[stop:]
    return _LABELLED.sub(replace, text)


_STEPS = (("private_key", _pem),
          ("aws_access_key_id", _whole("aws_access_key_id", _AWS_ID)),
          ("slack_token", _whole("slack_token", _SLACK)),
          ("slack_webhook", _whole("slack_webhook", _SLACK_WEBHOOK)),
          ("github_token", _whole("github_token", _GITHUB)),
          ("google_api_key", _whole("google_api_key", _GOOGLE)),
          ("stripe_key", _whole("stripe_key", _STRIPE)),
          ("api_key", _whole("api_key", _API_KEY)),
          ("bearer_token", _keep_prefix("bearer_token", _BEARER)),
          ("jwt", _whole("jwt", _JWT)),
          ("basic_auth", _keep_prefix("basic_auth", _BASIC)),
          ("url_password", _url_password))


def redact_counts(text, kinds=None):
    """(text, Counter kind -> replaced matches). `kinds` limits the kinds (default all).
    Non-string values come back unchanged with an empty Counter."""
    counts = Counter()
    if not isinstance(text, str) or not text:
        return text, counts
    enabled = KINDS if kinds is None else tuple(kinds)
    paired = _AWS_ID.search(text) is not None
    for kind, step in _STEPS:
        if kind in enabled:
            text = step(text, counts)
    if "aws_secret_access_key" in enabled:
        text = _aws_secret(text, counts, paired)
    if "labelled_secret" in enabled:
        text = _labelled(text, counts)
    return text, counts


def redact(text, kinds=None):
    """(redacted text, distinct kinds replaced in KINDS order). Pure; idempotent:
    redact(redact(x)[0]) leaves the text unchanged."""
    text, counts = redact_counts(text, kinds)
    return text, tuple(kind for kind in KINDS if counts[kind])


def redact_json(value, kinds=None):
    """(value, Counter) with every string inside a JSON value (dict values, list items)
    redacted. Dict keys stay as they are."""
    counts = Counter()

    def walk(item):
        if isinstance(item, str):
            clean, found = redact_counts(item, kinds)
            counts.update(found)
            return clean
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value), counts
