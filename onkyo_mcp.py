"""
Minimal MCP server for Onkyo receivers (eISCP over TCP).

Run (stdio transport, which is what Claude Code uses for local servers):
    ONKYO_HOST=192.168.1.50 mcp-server-onkyo        # or: python onkyo_mcp.py

Register with Claude Code:
    claude mcp add onkyo -e ONKYO_HOST=192.168.1.50 -- mcp-server-onkyo

Find receivers on your network (prints IP, model, MAC):
    python onkyo_mcp.py --discover

Inspect interactively (shows tools/list, lets you call tools by hand):
    npx @modelcontextprotocol/inspector python onkyo_mcp.py

Log MCP and eISCP traffic to stderr (either works; also with --discover):
    ONKYO_DEBUG=1 mcp-server-onkyo
    mcp-server-onkyo --debug
"""

import asyncio
import json
import logging
import os
import struct
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

HOST = os.environ.get("ONKYO_HOST", "192.168.1.50")
PORT = int(os.environ.get("ONKYO_PORT", "60128"))
# Volume as shown on the receiver's display (0-100). Server-side guardrail.
MAX_VOLUME = float(os.environ.get("ONKYO_MAX_VOLUME", "50"))
# Raw MVL steps per display unit. 2021+ models (TX-NR6050, TX-NR7100) use
# 0.5 steps, so raw 0x00-0xC8 maps to 0.0-100.0 -> 2. Older models: 1.
VOLUME_STEPS = int(os.environ.get("ONKYO_VOLUME_STEPS", "2"))
# Seconds to wait to connect, and then for a reply. Receivers vary a lot: a
# TX-NR6050 answers in ~0.1s, a TX-NR7100 takes ~1.5s even for a query.
# Power commands get 3x this (set_power), since the 7100 only confirms power-on
# after ~4s and standby after ~10s.
TIMEOUT = float(os.environ.get("ONKYO_TIMEOUT", "5"))
# Traffic logging (see enable_debug). Also switched on by --debug.
DEBUG = os.environ.get("ONKYO_DEBUG", "").lower() in ("1", "true", "yes", "on")

# Everything goes through this logger, which writes to stderr: on the stdio
# transport, stdout belongs to JSON-RPC. Silent (WARNING) unless debugging.
log = logging.getLogger("onkyo_mcp")


def enable_debug() -> None:
    """Log every MCP message and eISCP packet to stderr, at DEBUG level."""
    handler = logging.StreamHandler()  # defaults to sys.stderr
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False  # the SDK configures the root logger too; don't log twice


def raw_to_volume(raw: str) -> float:
    # "50" (hex) -> 80 raw steps -> 40.0 on the display, with VOLUME_STEPS=2
    return int(raw, 16) / VOLUME_STEPS


def volume_to_raw(volume: float) -> str:
    # 40.0 -> 80 raw steps -> "50". round() snaps e.g. 40.3 to the nearest
    # step the receiver supports; :02X is the two-digit uppercase hex it expects.
    return f"{round(volume * VOLUME_STEPS):02X}"


# Input selector (SLI) codes, named after the TX-NR7100/6050 front-panel labels.
# The Literal type becomes a JSON Schema "enum", so the model can only pick
# one of these names. Keep the two in sync.
Source = Literal["bd-dvd", "game", "cbl-sat", "strm-box", "pc", "aux", "tv",
                 "phono", "cd", "fm", "am", "net", "bluetooth"]
SOURCE_CODES: dict[str, str] = {
    "bd-dvd": "10", "game": "02", "cbl-sat": "01", "strm-box": "11", "pc": "05",
    "aux": "03", "tv": "12", "phono": "22", "cd": "23", "fm": "24", "am": "25",
    "net": "2B", "bluetooth": "2E",
}
# Reverse lookup, for turning the receiver's replies back into names
CODE_SOURCES = {code: name for name, code in SOURCE_CODES.items()}

# Listening mode (LMD) codes. Several codes have older and newer meanings in
# onkyo-eiscp's table (80 = PLII Movie / Dolby Surround, 82 = Neo:6 Cinema /
# DTS Neural:X, 03 = Film / Game-RPG); these names are the 2021-model ones.
ListeningMode = Literal["stereo", "direct", "pure-audio", "all-ch-stereo", "full-mono",
                        "theater-dimensional", "dolby-surround", "dts-neural-x",
                        "game-rpg", "game-action", "game-rock", "game-sports"]
MODE_CODES: dict[str, str] = {
    "stereo": "00", "direct": "01", "pure-audio": "11", "all-ch-stereo": "0C",
    "full-mono": "13", "theater-dimensional": "0D", "dolby-surround": "80",
    "dts-neural-x": "82", "game-rpg": "03", "game-action": "05", "game-rock": "06",
    "game-sports": "0E",
}
CODE_MODES = {code: name for name, code in MODE_CODES.items()}


# The name is what the client sees in the initialize handshake (serverInfo.name).
mcp = MCPServer("onkyo")


# ---------------------------------------------------------------------------
# eISCP transport layer (no MCP here; this is plain protocol code)
#
# Packet layout:
#   "ISCP" | header size (u32 BE, always 16) | data size (u32 BE)
#   | version (u8, 0x01) | 3 reserved bytes | data
# Data is "!1" + 3-char command + parameter + terminator.
#   e.g. "!1PWR01\r" = power on, "!1MVLQSTN\r" = query master volume
# Responses look like "!1MVL28\x1a\r\n" (volume is hex: 0x28 = 40).
# ---------------------------------------------------------------------------

def build_packet(command: str, unit: str = "1") -> bytes:
    # unit "1" = receiver; "x" = any device type (used for discovery)
    data = f"!{unit}{command}\r".encode("ascii")
    # struct format: ">" big-endian, "I" u32 header size, "I" u32 data size,
    # "B" u8 version, "3x" three zero padding bytes. 4 + 4 + 4 + 1 + 3 = 16.
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


def decode_datagram(packet: bytes) -> str:
    """Decode one whole eISCP packet (as received over UDP)."""
    magic, header_size, data_size, _version = struct.unpack(">4sIIB3x", packet[:16])
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    data = packet[header_size:header_size + data_size]
    # Same stripping as read_packet, plus \x19, which can also turn up at
    # the end of a UDP reply.
    return data.decode("ascii", "replace")[2:].rstrip("\x19\x1a\r\n")


DISCOVERY_ADDR = os.environ.get("ONKYO_DISCOVERY_ADDR", "255.255.255.255")


async def discover(timeout: float = 3.0) -> list[dict]:
    """Broadcast "!xECNQSTN" on UDP 60128. Each receiver replies with
    "!1ECN<model>/<port>/<region>/<mac>", and the reply's source address is
    its IP."""
    # Keyed by IP, so a receiver that answers twice is only listed once
    found: dict[str, dict] = {}

    # asyncio calls datagram_received for every UDP packet that arrives on
    # our socket, while discover() is sleeping below.
    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple) -> None:
            try:
                msg = decode_datagram(data)
            except (ValueError, struct.error):
                log.debug("eISCP <- %s (UDP) not eISCP, ignored: %r", addr[0], data[:32])
                return  # not eISCP (some other device on the port): ignore it
            log.debug("eISCP <- %s (UDP) %s", addr[0], msg)
            if not msg.startswith("ECN"):
                return
            # Pad with blanks so a reply with missing fields still unpacks
            model, port, region, mac = (msg[3:].split("/") + ["", "", "", ""])[:4]
            # "0009B0123456" -> "00:09:B0:12:34:56"
            mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if len(mac) >= 12 else mac
            found[addr[0]] = {"host": addr[0], "model": model, "port": int(port or 60128),
                              "region": region, "mac": mac}

    loop = asyncio.get_running_loop()
    # Port 0 = let the OS pick a free local port; replies come back to it.
    transport, _ = await loop.create_datagram_endpoint(
        Listener, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    try:
        log.debug("eISCP -> %s (UDP broadcast) ECNQSTN", DISCOVERY_ADDR)
        transport.sendto(build_packet("ECNQSTN", unit="x"), (DISCOVERY_ADDR, PORT))
        await asyncio.sleep(timeout)  # collect every reply that arrives in the window
    finally:
        transport.close()
    return sorted(found.values(), key=lambda r: r["model"])


async def read_packet(reader: asyncio.StreamReader) -> str:
    """Read one eISCP packet from a TCP stream. TCP is a byte stream, not a
    sequence of messages, so we read the fixed 16-byte header first to learn
    how many data bytes follow."""
    header = await reader.readexactly(16)
    magic, header_size, data_size, _version = struct.unpack(">4sIIB3x", header)
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    await reader.readexactly(header_size - 16)  # normally 0 bytes
    data = await reader.readexactly(data_size)
    # Strip "!1" prefix and the \x1a / \r / \n terminators
    return data.decode("ascii", "replace")[2:].rstrip("\x1a\r\n")


async def send(command: str, expect: str | None = None, timeout: float | None = None,
               host: str | None = None) -> str | None:
    """Send one command to `host` (default: ONKYO_HOST). If `expect` is a
    3-char prefix (e.g. "MVL"), wait for the matching reply. The receiver also
    pushes unsolicited status messages, so we skip anything that doesn't match.

    For a setter ("AMT01", as opposed to a query, "AMTQSTN"), the reply must
    echo the value we sent, or be "N/A". A same-prefix message with another
    value is a status push, not our answer: right after power-on a TX-NR7100
    pushes "AMT00" while still ignoring commands, which would otherwise read as
    "AMT01 failed".

    Raises ConnectionError (an OSError) if it can't connect, and TimeoutError
    if it connected but the reply never came (e.g. a receiver in standby)."""
    # One short-lived connection per command: simpler than keeping a socket
    # open, and it survives the receiver dropping idle connections.
    host = host or HOST
    timeout = timeout or TIMEOUT
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, PORT), timeout
        )
    except TimeoutError:
        # Re-raised as a different type, so callers can tell "couldn't
        # connect" apart from "connected, but no reply" (TimeoutError below).
        raise ConnectionError(f"timed out connecting to {host}:{PORT}") from None
    try:
        log.debug("eISCP -> %s %s", host, command)
        writer.write(build_packet(command))
        await writer.drain()
        if expect is None:
            return None
        sent_value = command[len(expect):]  # "AMT01" -> "01", "AMTQSTN" -> "QSTN"
        is_setter = command.startswith(expect) and sent_value != "QSTN"

        async def wait_for_match() -> str:
            while True:
                msg = await read_packet(reader)
                value = msg[len(expect):]  # "MVL50" -> "50"
                if msg.startswith(expect) and (not is_setter or value in (sent_value, "N/A")):
                    log.debug("eISCP <- %s %s", host, msg)
                    return value
                log.debug("eISCP <- %s %s (unsolicited, skipped)", host, msg)

        # One timeout around the whole loop, so a chatty receiver that never
        # sends the reply we want can't keep us waiting forever.
        return await asyncio.wait_for(wait_for_match(), timeout)
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# Debug logging of MCP traffic.
#
# Middleware wraps every request and notification the client sends us, after
# the SDK has parsed the JSON-RPC envelope but before it dispatches to a
# handler. So it sees the same method/params/result the wire carries, e.g.
#   MCP <- [3] tools/call {"name": "set_volume", "arguments": {"level": 30}}
#   MCP -> [3] {"content": [{"type": "text", "text": "Volume is now 30.0"}], ...}
# The [3] is the JSON-RPC id that pairs a response with its request;
# notifications (like notifications/initialized) have none and get no reply.
# Using middleware instead of tapping stdin/stdout means it also works
# unchanged on other transports (e.g. streamable HTTP).
# ---------------------------------------------------------------------------

def to_json(value) -> str:
    if isinstance(value, BaseModel):
        # by_alias: inputSchema rather than input_schema, as on the wire
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(value, default=str)


async def log_traffic(ctx, call_next):
    if not log.isEnabledFor(logging.DEBUG):
        return await call_next(ctx)
    tag = f"[{ctx.request_id}] " if ctx.request_id is not None else ""
    log.debug("MCP <- %s%s %s", tag, ctx.method, to_json(ctx.params or {}))
    try:
        result = await call_next(ctx)
    except Exception as exc:  # protocol errors (unknown method, bad params) arrive as exceptions
        log.debug("MCP -> %serror: %r", tag, exc)
        raise
    if ctx.request_id is not None:
        log.debug("MCP -> %s%s", tag, to_json(result))
    return result


mcp.middleware.append(log_traffic)


# ---------------------------------------------------------------------------
# MCP layer: each @mcp.tool() becomes an entry in tools/list.
# The docstring becomes the tool description the model reads, and the type
# hints become the JSON Schema for the arguments. Write both carefully:
# they are the model's only documentation.
#
# Tool annotations are hints about a tool's *behavior*, sent in tools/list:
#   {"name": "set_volume", "title": "Set volume",
#    "annotations": {"readOnlyHint": false, "destructiveHint": false,
#                    "idempotentHint": true, "openWorldHint": false}, ...}
# Clients use them to decide how careful to be, e.g. auto-approving read-only
# tools but asking the user before destructive ones. They are only hints: a
# client should not trust them from a server it doesn't trust, and they are
# no substitute for real server-side limits like MAX_VOLUME.
#
# The spec's defaults are deliberately pessimistic (a tool with no
# annotations counts as destructive, non-idempotent and open-world), so it
# pays to state them.
# ---------------------------------------------------------------------------

# Queries: they change nothing on the receiver.
# (destructiveHint and idempotentHint only matter when readOnlyHint is false.)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Setters: they change state, but nothing is lost that another call can't put
# back (not destructive), and sending "volume 30" twice leaves the receiver
# exactly as sending it once (idempotent). Closed world: they only talk to a
# receiver we were pointed at, not the wider internet.
SETTER = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                         idempotent_hint=True, open_world_hint=False)


# Discovery reads nothing but replies, so it is read-only. It is open-world,
# though: it broadcasts to the whole LAN and lists whatever answers.
@mcp.tool(title="Discover receivers",
          annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def discover_receivers() -> list[dict]:
    """Find Onkyo/Integra/Pioneer receivers on the local network. Returns each
    receiver's IP address, model, eISCP port and MAC address."""
    return await discover()


# Optional, so single-receiver setups (ONKYO_HOST) keep working unchanged.
# The Field description lands in the tool's JSON Schema next to the type.
Receiver = Annotated[str | None, Field(
    description="IP address of the receiver, as returned by discover_receivers. "
                "Omit to use the default receiver."
)]


async def call_receiver(command: str, expect: str, receiver: str | None,
                        timeout: float | None = None) -> str:
    """send() for tools: turns network failures into a ToolError whose message
    tells the model what went wrong.

    Why ToolError: when a tool raises any other exception, the SDK treats it as
    a crash and keeps the details on the server. The model only sees
    "Error executing tool set_volume", so it can't tell the user anything
    useful. A ToolError is a failure we raised on purpose, and its message is
    sent to the model as an isError result."""
    host = receiver or HOST
    try:
        return await send(command, expect=expect, host=host, timeout=timeout)
    except TimeoutError as exc:
        # Connected, but no reply. Receivers in standby still answer queries,
        # but some (TX-NR7100) silently ignore everything else, so ask which.
        if not command.startswith("PWR"):
            try:
                standby = await send("PWRQSTN", expect="PWR", host=host) == "00"
            except OSError:  # includes TimeoutError
                standby = False
            if standby:
                raise ToolError(f"The receiver at {host} is in standby. "
                                "Turn it on with set_power first.") from exc
        raise ToolError(f"The receiver at {host} didn't reply in time. If it was just "
                        "turned on, it may still be starting up (some models take about "
                        "15 seconds): wait a few seconds and try again.") from exc
    except OSError as exc:
        raise ToolError(f"Can't connect to a receiver at {host} ({exc}). Check the "
                        "IP address, and that the receiver is on the network.") from exc


@mcp.tool(title="Get receiver status", annotations=READ_ONLY)
async def get_status(receiver: Receiver = None) -> dict:
    """Get a receiver's current power state, master volume (0-100), mute
    state, selected input and listening mode. If there are several receivers
    on the network, call discover_receivers first and pass the one you want."""
    host = receiver or HOST
    power = await call_receiver("PWRQSTN", "PWR", host)
    volume = await call_receiver("MVLQSTN", "MVL", host)
    mute = await call_receiver("AMTQSTN", "AMT", host)
    source = await call_receiver("SLIQSTN", "SLI", host)
    mode = await call_receiver("LMDQSTN", "LMD", host)
    return {
        "receiver": host,  # so the model can tell answers from different receivers apart
        "power": "on" if power == "01" else "standby",
        "volume": raw_to_volume(volume) if volume and volume != "N/A" else None,
        "muted": mute == "01",
        # Unknown codes (not in our tables) are shown raw, e.g. "SLI2C"
        "input": CODE_SOURCES.get(source, f"SLI{source}") if source and source != "N/A" else None,
        "listening_mode": CODE_MODES.get(mode, f"LMD{mode}") if mode and mode != "N/A" else None,
    }


@mcp.tool(title="Set power", annotations=SETTER)
async def set_power(on: bool, receiver: Receiver = None) -> str:
    """Turn the main zone on, or put it into standby. After power-on, some
    receivers need about 15 seconds before they accept other commands."""
    # Power changes are slow to confirm (a TX-NR7100 takes ~10s to reach standby)
    reply = await call_receiver("PWR01" if on else "PWR00", "PWR", receiver,
                                timeout=3 * TIMEOUT)
    if reply == "01":
        # The TX-NR7100 confirms power-on, then ignores commands for ~15s
        return ("Power is now on. Some receivers need about 15 seconds to start up "
                "before they accept other commands.")
    return "Power is now standby"


@mcp.tool(title="Set volume", annotations=SETTER)
async def set_volume(level: float, receiver: Receiver = None) -> str:
    """Set master volume on the receiver's 0-100 display scale (0.5 steps on
    newer models). Values above the configured safety cap are clamped."""
    clamped = max(0.0, min(level, MAX_VOLUME))
    reply = await call_receiver(f"MVL{volume_to_raw(clamped)}", "MVL", receiver)
    if reply == "N/A":
        return "Receiver rejected the volume change (is it powered on?)"
    note = f" (requested {level}, capped at {MAX_VOLUME})" if clamped != level else ""
    return f"Volume is now {raw_to_volume(reply)}{note}"


@mcp.tool(title="Set mute", annotations=SETTER)
async def set_mute(muted: bool, receiver: Receiver = None) -> str:
    """Mute or unmute the main zone."""
    reply = await call_receiver("AMT01" if muted else "AMT00", "AMT", receiver)
    return "Muted" if reply == "01" else "Unmuted"


@mcp.tool(title="Select input", annotations=SETTER)
async def set_input(source: Source, receiver: Receiver = None) -> str:
    """Select the main zone's input source. Names match the receiver's
    front-panel labels (e.g. "bd-dvd" for the BD/DVD input, "net" for
    network streaming). The receiver must be on."""
    reply = await call_receiver(f"SLI{SOURCE_CODES[source]}", "SLI", receiver)
    if reply == "N/A":
        return f"Receiver rejected input {source!r} (is it powered on?)"
    return f"Input is now {CODE_SOURCES.get(reply, f'SLI{reply}')}"


@mcp.tool(title="Set listening mode", annotations=SETTER)
async def set_listening_mode(mode: ListeningMode, receiver: Receiver = None) -> str:
    """Set the main zone's listening mode (surround processing). "direct" and
    "pure-audio" play the source unprocessed; "dolby-surround" and
    "dts-neural-x" upmix to all speakers and play Dolby Atmos / DTS:X content
    natively. The receiver must be on, and may reject modes that don't suit
    the current input signal."""
    reply = await call_receiver(f"LMD{MODE_CODES[mode]}", "LMD", receiver)
    if reply == "N/A":
        return f"Receiver rejected listening mode {mode!r} (powered off, or not available for this signal?)"
    return f"Listening mode is now {CODE_MODES.get(reply, f'LMD{reply}')}"


def main() -> None:
    import sys

    if DEBUG or "--debug" in sys.argv:
        enable_debug()

    if "--discover" in sys.argv:  # quick CLI check, no MCP involved
        receivers = asyncio.run(discover())
        for r in receivers:
            print(f"{r['host']:<16} {r['model']:<12} {r['mac']}  port {r['port']}")
        if not receivers:
            print("No receivers answered. See README: Troubleshooting.", file=sys.stderr)
    else:
        # Defaults to stdio transport: JSON-RPC over stdin/stdout, which is why
        # nothing in the server may print() to stdout.
        mcp.run()


if __name__ == "__main__":
    main()
