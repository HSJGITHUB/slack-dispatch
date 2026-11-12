"""Local daily logs with token redaction and payload-attribute removal."""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import re

_TOKEN = re.compile(r"xox[abp]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+")


def _clean(value):
    if isinstance(value, str):
        return _TOKEN.sub("***", value)
    if isinstance(value, dict):
        return {_clean(key): _clean(item) for key, item in value.items()
                if key not in ("text", "body")}
    if isinstance(value, (tuple, list)):
        return tuple(_clean(item) for item in value)
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record):
        for key in list(record.__dict__):
            if key in ("text", "body"):
                del record.__dict__[key]
            elif key != "exc_info":
                record.__dict__[key] = _clean(record.__dict__[key])
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return _TOKEN.sub("***", super().format(record))


def configure_logging(log_dir, level=logging.INFO):
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("slack_dispatch")
    logger.setLevel(level)
    logger.propagate = False
    for old in list(logger.handlers):
        if getattr(old, "_dispatch_runtime", False):
            logger.removeHandler(old)
            old.close()
    handler = TimedRotatingFileHandler(directory / "service.log", when="midnight",
                                       backupCount=0, encoding="utf-8", utc=False)
    handler._dispatch_runtime = True
    handler.addFilter(RedactionFilter())
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger