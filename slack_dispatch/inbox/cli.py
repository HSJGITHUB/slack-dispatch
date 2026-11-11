"""Parse the future inbox commands without mutating state."""
import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pre-alpha lane inbox CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inbox", "claim"):
        commands.add_parser(command).add_argument("lane")
    commands.add_parser("ack").add_argument("id")
    done = commands.add_parser("done")
    done.add_argument("id")
    done.add_argument("--note")
    reroute = commands.add_parser("reroute")
    reroute.add_argument("id")
    reroute.add_argument("--to", required=True, dest="lane")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"Command {args.command} is not implemented")


if __name__ == "__main__":
    raise SystemExit(main())
