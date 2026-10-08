"""Entry point: ``mcp-server-onkyo [serve] | discover``.

``serve`` is the default, so ``mcp-server-onkyo`` alone still runs the MCP
server over stdio, and ``mcp-server-onkyo --http`` works as well as
``mcp-server-onkyo serve --http`` (the sibling servers spell it the second
way). The old ``--discover`` flag still works too.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__, eiscp, remote
from .config import load_settings
from .logsafe import setup_logging

COMMANDS = ("serve", "discover")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-onkyo", description="MCP server for Onkyo AV receivers (eISCP)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="log MCP and eISCP traffic to stderr")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show LAN addresses and MACs in logs (ONKYO_LOG_UNREDACTED=1); secrets stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio by default, or --http)")
    serve.add_argument(
        "--dry-run",
        action="store_true",
        help="read the receivers, but send nothing that changes anything (ONKYO_DRY_RUN=1)",
    )
    remote.add_http_arguments(serve, default_port=8711, default_path="/onkyo/mcp")

    disc = sub.add_parser("discover", parents=[common], help="list receivers that answer a discovery broadcast")
    disc.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for replies (default 3)")
    return parser


def normalize(argv: list[str]) -> list[str]:
    """Accept the old spellings: no subcommand means serve, and --discover means discover."""
    argv = list(argv)
    if "--discover" in argv:
        argv.remove("--discover")
        return ["discover", *argv]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["serve", *argv]
    return argv


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(normalize(sys.argv[1:] if argv is None else argv))
    if getattr(args, "dry_run", False):
        os.environ["ONKYO_DRY_RUN"] = "1"  # the server's lifespan reads settings from the environment
    settings = load_settings()

    from .server import enable_debug, mcp  # the tools register on import

    redacted = not (args.no_redact or os.environ.get("ONKYO_LOG_UNREDACTED", "") in ("1", "true", "yes", "on"))
    # stderr only: on the stdio transport, stdout carries JSON-RPC
    setup_logging(logging.INFO if getattr(args, "http", False) else logging.WARNING, redacted=redacted)
    if settings.debug or args.debug:
        enable_debug(redacted)

    if args.cmd == "discover":  # quick CLI check, no MCP involved
        receivers = asyncio.run(eiscp.discover(settings.discovery_addr, settings.discovery_port, args.timeout))
        for r in receivers:
            print(f"{r.host:<16} {r.model:<12} {r.mac}  port {r.port}")
        if not receivers:
            print("No receivers answered. See README: When discovery finds nothing.", file=sys.stderr)
            sys.exit(1)
        return

    if args.http:
        # A long-lived HTTP service, e.g. in an LXC behind Cloudflare Access
        # (see remote.py). Its logs go to stderr like everything else.
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-onkyo: {exc}\n")
    else:
        # stdio transport: JSON-RPC over stdin/stdout, which is why nothing in
        # the server may print() to stdout.
        mcp.run()
