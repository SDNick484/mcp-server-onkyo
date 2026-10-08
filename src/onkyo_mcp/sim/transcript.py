"""Turn a ``--debug`` log of one tool call into a contract-test fixture.

On real hardware, run one tool call with debug logging::

    mcp-server-onkyo call --debug set_volume level=30 zone=zone2 receiver=Theater 2> call.log

then::

    python -m onkyo_mcp.sim.transcript call.log > tests/fixtures/eiscp/hw_theater_zone2_volume.json

and fill in "description", "source" and "assumptions" (the ids the
exchange confirms). tests/test_contract.py then replays it against the
server forever after, so a change that alters the traffic a real receiver
accepted fails a test instead of failing in your living room.

How the log maps onto exchanges: each ``eISCP -> <host> <command>`` starts
one, and every ``eISCP <- <host> <message>`` until the next command is its
replies, pushes included. The server logs a reply when it *reads* it, so a
push that arrived just after a matched reply shows up under the next
command; ReplayReceiver then sends it a little later than the receiver did,
which the server must (and does) tolerate either way.

Only the first ``tools/call`` in the log is used, and only TCP traffic
(discovery's UDP lines are skipped). Its ``receiver`` argument is dropped:
in a contract test the replay is the only receiver.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_SEND = re.compile(r"eISCP -> (\S+) (?!\(UDP)(.+)$")
_RECV = re.compile(r"eISCP <- (\S+) (?!\(UDP)(.+?)(?: \(unsolicited, skipped\))?$")
_CALL = re.compile(r"MCP <- \[[^\]]*\] tools/call (\{.*\})$")


def from_log(text: str) -> dict[str, Any]:
    tool: str | None = None
    args: dict[str, Any] = {}
    exchanges: list[dict[str, Any]] = []
    for line in text.splitlines():
        if tool is None and (m := _CALL.search(line)):
            params = json.loads(m.group(1))
            tool, args = params.get("name"), dict(params.get("arguments") or {})
            # The replay is the only receiver in a contract test, so a name
            # picked from your config would match nothing there
            args.pop("receiver", None)
        elif m := _SEND.search(line):
            exchanges.append({"send": m.group(2), "replies": []})
        elif (m := _RECV.search(line)) and exchanges:
            exchanges[-1]["replies"].append(m.group(2))
    if tool is None:
        raise ValueError("no tools/call in the log: was it run with --debug?")
    if not exchanges:
        raise ValueError("no eISCP traffic in the log")
    return {
        "description": "TODO: what this call does",
        "source": "TODO: model and firmware it was captured from, and the date",
        "assumptions": [],
        "tool": tool,
        "args": args,
        "exchanges": exchanges,
    }


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m onkyo_mcp.sim.transcript DEBUG.LOG > fixture.json")
    with open(sys.argv[1], encoding="utf-8") as f:
        fixture = from_log(f.read())
    print(json.dumps(fixture, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
