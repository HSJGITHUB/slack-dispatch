"""Run the operator console: python -m slack_dispatch.console --config PATH."""
import argparse
from pathlib import Path
import sys

from slack_dispatch.config.loader import ConfigError, load_config

DEFAULT_HOST = "127.0.0.1"


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m slack_dispatch.console",
                                     description="Local operator console for the human queue")
    parser.add_argument("--config", required=True, metavar="PATH")
    parser.add_argument("--host", default=None,
                        help="bind address (default: console.host, which defaults to 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="port (default: console.port, which defaults to 8811)")
    return parser


def bind_address(args, config):
    """--host and --port win over the console section; the default bind is 127.0.0.1."""
    host = args.host or config.console.host or DEFAULT_HOST
    port = args.port if args.port is not None else config.console.port
    return host, port


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as error:
        return error.code
    try:
        config = load_config(args.config)
    except ConfigError:
        print("Invalid console configuration", file=sys.stderr)
        return 2
    host, port = bind_address(args, config)
    if not 1 <= port <= 65535:
        print("Invalid command arguments", file=sys.stderr)
        return 2
    try:
        import uvicorn
        from slack_dispatch.console.app import create_app
    except ImportError:
        print("The console needs its optional dependencies: pip install -e .[console]",
              file=sys.stderr)
        return 2
    from slack_dispatch.console.queries import open_ledger
    if not Path(config.ledger.path).exists():
        print("Ledger not found: start the service first", file=sys.stderr)
        return 2
    try:
        store = open_ledger(config)
    except Exception:
        print("Cannot open the ledger", file=sys.stderr)
        return 1
    try:
        app = create_app(config, store, host=host)
        # No access log (request lines carry search text and ids) and no uvicorn log config.
        uvicorn.run(app, host=host, port=port, access_log=False, log_config=None,
                    log_level="warning")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
