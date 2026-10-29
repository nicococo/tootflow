"""tootflow — argument parsing and dispatch."""
import argparse

from app.cli import accounts, campaigns, ops, posts
from app.cli.common import load_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tootflow",
        description="Single-user TootFlow CLI for agent harnesses: accounts, campaigns, posts, publishing.",
        epilog="Content is expected to come from the calling agent. "
               "Add --json to any command for machine-readable output. "
               "Exit codes: 0 ok, 1 error, 2 usage.",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    accounts.register(sub)
    campaigns.register(sub)
    posts.register(sub)
    ops.register(sub)

    from app.cli.schema import cmd_schema
    schema_parser = sub.add_parser(
        "schema", help="Print this CLI as a JSON agent-tool manifest"
    )
    schema_parser.set_defaults(func=cmd_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
