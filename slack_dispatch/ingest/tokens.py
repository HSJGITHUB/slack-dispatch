"""Credential parsing is deferred until startup; diagnostics never contain values."""
import hashlib
import logging
from pathlib import Path


class TokenError(ValueError):
    pass


def parse_env(source, key, prefix):
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.split("#", 1)[0].strip()
        if not value.startswith(prefix):
            raise TokenError("Invalid token format")
        return value
    raise TokenError("Missing token key")


def load_token(path, key, prefix):
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8-sig")
        token = parse_env(content, key, prefix)
    except (OSError, UnicodeError, TokenError):
        raise TokenError("Token file unreadable or invalid") from None
    print(f"Token: {prefix} len={len(token)} sha8={hashlib.sha256(token.encode()).hexdigest()[:8]}")
    return token


def quiet_logger():
    # SDK errors and debug records may include raw envelopes and headers.
    logger = logging.Logger("slack_dispatch.intake.silent")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.disabled = True
    return logger
