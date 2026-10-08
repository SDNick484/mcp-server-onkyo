"""Entry point: ``mcp-server-onkyo [serve] | discover | doctor | simulate | call``.

``serve`` is the default, so ``mcp-server-onkyo`` alone still runs the MCP
server over stdio, and ``mcp-server-onkyo --http`` works as well as
``mcp-server-onkyo serve --http`` (the sibling servers spell it the second
way). The old ``--discover`` flag still works too.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys

from . import __version__, eiscp, remote
from .config import load_settings
from .logsafe import setup_logging

COMMANDS = ("serve", "discover", "doctor", "simulate", "call")


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

    doc = sub.add_parser("doctor", parents=[common], help="check config and each receiver, layer by layer (read-only)")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--dump", metavar="DIR", help="also write each receiver's raw replies (redacted) to DIR")
    doc.add_argument("--timeout", type=float, default=5.0, help="seconds per step (default 5)")
    doc.add_argument("--host", help="check this address instead of the configured receivers")

    sim = sub.add_parser("simulate", parents=[common], help="run fake receivers locally, for testing without hardware")
    sim.add_argument("--port", type=int, default=60128, help="port of the first fake (default 60128)")
    sim.add_argument(
        "--model", action="append", help="TX-NR6050 or TX-NR7100; repeat for several (default: one of each)"
    )
    sim.add_argument("--write-config", metavar="DIR", help="write a config.json naming the fakes to DIR")

    cal = sub.add_parser("call", parents=[common], help="call one tool as the model would and print the result")
    cal.add_argument("tool", help="tool name, or 'tools' to list them")
    cal.add_argument("args", nargs="*", metavar="key=value", help="tool arguments (values are JSON if they parse)")
    cal.add_argument("--dry-run", action="store_true", help="send nothing that changes anything")
    return parser


async def _doctor(args: argparse.Namespace, redacted: bool) -> int:
    import dataclasses
    from pathlib import Path

    from .config import ReceiverSettings
    from .doctor import render, run_doctor, to_json

    settings = load_settings()
    if args.host:
        settings = dataclasses.replace(settings, receivers=(ReceiverSettings(args.host),))
    report = await run_doctor(settings, timeout=args.timeout, dump=Path(args.dump) if args.dump else None)
    print(to_json(report, redacted) if args.json else render(report, redacted))
    return 0 if report.ok else 1


SIM_NAMES = {"TX-NR6050": "Family Room", "TX-NR7100": "Theater"}


async def _simulate(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .sim.fake_receiver import make

    models = args.model or ["TX-NR6050", "TX-NR7100"]
    fakes = [make(m) for m in models]
    try:
        for i, f in enumerate(fakes):
            await f.start(args.port if i == 0 else 0)
    except OSError as exc:
        print(f"Couldn't start a fake receiver: {exc}. Is port {args.port} free? Try --port 0.", file=sys.stderr)
        for f in fakes:
            await f.stop()
        return 1
    print("Fake receivers (they implement eISCP as this server expects it; see assumptions.py):")
    for f in fakes:
        print(f"  {SIM_NAMES.get(f.model, f.model):<12} {f.model:<10} {f.host}:{f.port}")
    if args.write_config:
        cfg_dir = Path(args.write_config)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        doc = {"receivers": [{"host": f.host, "port": f.port, "name": SIM_NAMES.get(f.model)} for f in fakes]}
        (cfg_dir / "config.json").write_text(json.dumps(doc, indent=2) + "\n")
        print(f"\nWrote {cfg_dir / 'config.json'}. In another terminal:")
        print(f"  ONKYO_CONFIG_DIR={cfg_dir} mcp-server-onkyo doctor")
        print(f"  ONKYO_CONFIG_DIR={cfg_dir} mcp-server-onkyo call get_status")
        print(f"  ONKYO_CONFIG_DIR={cfg_dir} mcp-server-onkyo serve      # or add it to Claude Code/Desktop")
    else:
        print("(Pass --write-config DIR to have a config.json for these written for you.)")
    print("Ctrl+C to stop.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for f in fakes:
            await f.stop()
    return 0


async def _call(args: argparse.Namespace) -> int:
    import json

    from mcp import Client

    from .server import mcp

    tool_args: dict[str, object] = {}
    for pair in args.args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return 2
        try:
            tool_args[key] = json.loads(raw)  # level=30 -> 30, muted=true -> True
        except json.JSONDecodeError:
            tool_args[key] = raw  # receiver=Family Room -> a string
    async with Client(mcp) as c:
        if args.tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<20} {(t.description or '').splitlines()[0]}")
            return 0
        result = await c.call_tool(args.tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


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
    if getattr(args, "dry_run", False):  # serve --dry-run, call --dry-run
        os.environ["ONKYO_DRY_RUN"] = "1"  # the server's lifespan reads settings from the environment
    settings = load_settings()

    from .server import enable_debug, mcp  # the tools register on import

    redacted = not (args.no_redact or os.environ.get("ONKYO_LOG_UNREDACTED", "") in ("1", "true", "yes", "on"))
    # stderr only: on the stdio transport, stdout carries JSON-RPC
    setup_logging(logging.INFO if getattr(args, "http", False) else logging.WARNING, redacted=redacted)
    if settings.debug or args.debug:
        enable_debug(redacted)

    if args.cmd == "doctor":
        sys.exit(asyncio.run(_doctor(args, redacted)))
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_simulate(args)))
        return
    if args.cmd == "call":
        sys.exit(asyncio.run(_call(args)))

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
