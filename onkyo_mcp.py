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
"""

import asyncio
import os
import struct
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

HOST = os.environ.get("ONKYO_HOST", "192.168.1.50")
PORT = int(os.environ.get("ONKYO_PORT", "60128"))
# Volume as shown on the receiver's display (0-100). Server-side guardrail.
MAX_VOLUME = float(os.environ.get("ONKYO_MAX_VOLUME", "50"))
# Raw MVL steps per display unit. 2021+ models (TX-NR6050, TX-NR7100) use
# 0.5 steps, so raw 0x00-0xC8 maps to 0.0-100.0 -> 2. Older models: 1.
VOLUME_STEPS = int(os.environ.get("ONKYO_VOLUME_STEPS", "2"))


def raw_to_volume(raw: str) -> float:
    return int(raw, 16) / VOLUME_STEPS


def volume_to_raw(volume: float) -> str:
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
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


def decode_datagram(packet: bytes) -> str:
    """Decode one whole eISCP packet (as received over UDP)."""
    magic, header_size, data_size, _version = struct.unpack(">4sIIB3x", packet[:16])
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    data = packet[header_size:header_size + data_size]
    return data.decode("ascii", "replace")[2:].rstrip("\x19\x1a\r\n")


DISCOVERY_ADDR = os.environ.get("ONKYO_DISCOVERY_ADDR", "255.255.255.255")


async def discover(timeout: float = 3.0) -> list[dict]:
    """Broadcast "!xECNQSTN" on UDP 60128. Each receiver replies with
    "!1ECN<model>/<port>/<region>/<mac>", and the reply's source address is
    its IP."""
    found: dict[str, dict] = {}

    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple) -> None:
            try:
                msg = decode_datagram(data)
            except (ValueError, struct.error):
                return
            if not msg.startswith("ECN"):
                return
            model, port, region, mac = (msg[3:].split("/") + ["", "", "", ""])[:4]
            mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if len(mac) >= 12 else mac
            found[addr[0]] = {"host": addr[0], "model": model, "port": int(port or 60128),
                              "region": region, "mac": mac}

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        Listener, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    try:
        transport.sendto(build_packet("ECNQSTN", unit="x"), (DISCOVERY_ADDR, PORT))
        await asyncio.sleep(timeout)  # collect every reply that arrives in the window
    finally:
        transport.close()
    return sorted(found.values(), key=lambda r: r["model"])


async def read_packet(reader: asyncio.StreamReader) -> str:
    header = await reader.readexactly(16)
    magic, header_size, data_size, _version = struct.unpack(">4sIIB3x", header)
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    await reader.readexactly(header_size - 16)  # normally 0 bytes
    data = await reader.readexactly(data_size)
    # Strip "!1" prefix and the \x1a / \r / \n terminators
    return data.decode("ascii", "replace")[2:].rstrip("\x1a\r\n")


async def send(command: str, expect: str | None = None, timeout: float = 2.0,
               host: str | None = None) -> str | None:
    """Send one command to `host` (default: ONKYO_HOST). If `expect` is a
    3-char prefix (e.g. "MVL"), wait for the matching reply. The receiver also
    pushes unsolicited status messages, so we skip anything that doesn't match."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host or HOST, PORT), timeout
    )
    try:
        writer.write(build_packet(command))
        await writer.drain()
        if expect is None:
            return None

        async def wait_for_match() -> str:
            while True:
                msg = await read_packet(reader)
                if msg.startswith(expect):
                    return msg[len(expect):]

        return await asyncio.wait_for(wait_for_match(), timeout)
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# MCP layer: each @mcp.tool() becomes an entry in tools/list.
# The docstring becomes the tool description the model reads, and the type
# hints become the JSON Schema for the arguments. Write both carefully:
# they are the model's only documentation.
# ---------------------------------------------------------------------------

@mcp.tool()
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


@mcp.tool()
async def get_status(receiver: Receiver = None) -> dict:
    """Get a receiver's current power state, master volume (0-100), mute
    state, selected input and listening mode. If there are several receivers
    on the network, call discover_receivers first and pass the one you want."""
    host = receiver or HOST
    power = await send("PWRQSTN", expect="PWR", host=host)
    volume = await send("MVLQSTN", expect="MVL", host=host)
    mute = await send("AMTQSTN", expect="AMT", host=host)
    source = await send("SLIQSTN", expect="SLI", host=host)
    mode = await send("LMDQSTN", expect="LMD", host=host)
    return {
        "receiver": host,  # so the model can tell answers from different receivers apart
        "power": "on" if power == "01" else "standby",
        "volume": raw_to_volume(volume) if volume and volume != "N/A" else None,
        "muted": mute == "01",
        # Unknown codes (not in our tables) are shown raw, e.g. "SLI2C"
        "input": CODE_SOURCES.get(source, f"SLI{source}") if source and source != "N/A" else None,
        "listening_mode": CODE_MODES.get(mode, f"LMD{mode}") if mode and mode != "N/A" else None,
    }


@mcp.tool()
async def set_power(on: bool, receiver: Receiver = None) -> str:
    """Turn the main zone on, or put it into standby."""
    reply = await send("PWR01" if on else "PWR00", expect="PWR", host=receiver)
    return f"Power is now {'on' if reply == '01' else 'standby'}"


@mcp.tool()
async def set_volume(level: float, receiver: Receiver = None) -> str:
    """Set master volume on the receiver's 0-100 display scale (0.5 steps on
    newer models). Values above the configured safety cap are clamped."""
    clamped = max(0.0, min(level, MAX_VOLUME))
    reply = await send(f"MVL{volume_to_raw(clamped)}", expect="MVL", host=receiver)
    if reply == "N/A":
        return "Receiver rejected the volume change (is it powered on?)"
    note = f" (requested {level}, capped at {MAX_VOLUME})" if clamped != level else ""
    return f"Volume is now {raw_to_volume(reply)}{note}"


@mcp.tool()
async def set_mute(muted: bool, receiver: Receiver = None) -> str:
    """Mute or unmute the main zone."""
    reply = await send("AMT01" if muted else "AMT00", expect="AMT", host=receiver)
    return "Muted" if reply == "01" else "Unmuted"


@mcp.tool()
async def set_input(source: Source, receiver: Receiver = None) -> str:
    """Select the main zone's input source. Names match the receiver's
    front-panel labels (e.g. "bd-dvd" for the BD/DVD input, "net" for
    network streaming). The receiver must be on."""
    reply = await send(f"SLI{SOURCE_CODES[source]}", expect="SLI", host=receiver)
    if reply == "N/A":
        return f"Receiver rejected input {source!r} (is it powered on?)"
    return f"Input is now {CODE_SOURCES.get(reply, f'SLI{reply}')}"


@mcp.tool()
async def set_listening_mode(mode: ListeningMode, receiver: Receiver = None) -> str:
    """Set the main zone's listening mode (surround processing). "direct" and
    "pure-audio" play the source unprocessed; "dolby-surround" and
    "dts-neural-x" upmix to all speakers and play Dolby Atmos / DTS:X content
    natively. The receiver must be on, and may reject modes that don't suit
    the current input signal."""
    reply = await send(f"LMD{MODE_CODES[mode]}", expect="LMD", host=receiver)
    if reply == "N/A":
        return f"Receiver rejected listening mode {mode!r} (powered off, or not available for this signal?)"
    return f"Listening mode is now {CODE_MODES.get(reply, f'LMD{reply}')}"


def main() -> None:
    import sys

    if "--discover" in sys.argv:  # quick CLI check, no MCP involved
        receivers = asyncio.run(discover())
        for r in receivers:
            print(f"{r['host']:<16} {r['model']:<12} {r['mac']}  port {r['port']}")
        if not receivers:
            print("No receivers answered. See README: Troubleshooting.", file=sys.stderr)
    else:
        mcp.run()  # defaults to stdio transport


if __name__ == "__main__":
    main()
