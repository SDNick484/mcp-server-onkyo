"""
Minimal MCP server for Onkyo receivers (eISCP over TCP).

Run (stdio transport, which is what Claude Code uses for local servers):
    ONKYO_HOST=192.168.1.50 mcp-server-onkyo        # or: python -m onkyo_mcp

Register with Claude Code:
    claude mcp add onkyo -e ONKYO_HOST=192.168.1.50 -- mcp-server-onkyo

Find receivers on your network (prints IP, model, MAC):
    mcp-server-onkyo --discover

Inspect interactively (shows tools/list, lets you call tools by hand):
    npx @modelcontextprotocol/inspector mcp-server-onkyo

Serve over HTTP instead (an always-on box behind Cloudflare Access; see README):
    mcp-server-onkyo --http --port 8711

Log MCP and eISCP traffic to stderr (either works; also with --discover):
    ONKYO_DEBUG=1 mcp-server-onkyo
    mcp-server-onkyo --debug
"""

import asyncio
import dataclasses
import json
import logging
import os
from collections.abc import Callable
from typing import Annotated, Literal
from xml.etree import ElementTree

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import eiscp
from .codes import (
    CODE_MODES,
    CODE_NET_SERVICES,
    CODE_SOURCES,
    MODE_CODES,
    NET_SERVICE_CODES,
    PLAY_STATES,
    SOURCE_CODES,
    ZONE_CODES,
    ZONE_LABELS,
    ListeningMode,
    NetService,
    Source,
    Zone,
)


def setting(name: str, default: str) -> str:
    """An ONKYO_* environment variable, or `default` if it's unset *or empty*.
    Empty happens in practice: WSL turns a variable listed in WSLENV but not
    set on the Windows side into "", which would crash float("")."""
    return os.environ.get(name) or default


# No default address: a made-up one would send commands to whatever device
# happens to have it. Unset means "use the one receiver discovery finds".
HOST = setting("ONKYO_HOST", "")
PORT = int(setting("ONKYO_PORT", "60128"))
# Volume as shown on the receiver's display (0-100). Server-side guardrail.
MAX_VOLUME = float(setting("ONKYO_MAX_VOLUME", "75"))
# Raw MVL steps per display unit. 2021+ models (TX-NR6050, TX-NR7100) use
# 0.5 steps, so raw 0x00-0xC8 maps to 0.0-100.0 -> 2. Older models: 1.
VOLUME_STEPS = int(setting("ONKYO_VOLUME_STEPS", "2"))
# Seconds to wait to connect, and then for a reply. Receivers vary a lot: a
# TX-NR6050 answers in ~0.1s, a TX-NR7100 takes ~1.5s even for a query.
# Power commands get 3x this (set_power), since the 7100 only confirms power-on
# after ~4s and standby after ~10s.
TIMEOUT = float(setting("ONKYO_TIMEOUT", "5"))
# Traffic logging (see enable_debug). Also switched on by --debug.
DEBUG = setting("ONKYO_DEBUG", "").lower() in ("1", "true", "yes", "on")

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


# The name is what the client sees in the initialize handshake (serverInfo.name).
mcp = MCPServer("onkyo")


# ---------------------------------------------------------------------------
# eISCP transport: eiscp.py (plain protocol code, no MCP). These wrappers
# supply this server's settings (address, port, timeout).
# ---------------------------------------------------------------------------

DISCOVERY_ADDR = setting("ONKYO_DISCOVERY_ADDR", "255.255.255.255")


async def discover(timeout: float = 3.0) -> list[dict[str, str | int]]:
    """Receivers that answer a discovery broadcast, as dicts (see eiscp.discover)."""
    return [dataclasses.asdict(r) for r in await eiscp.discover(DISCOVERY_ADDR, PORT, timeout)]


async def send(
    command: str,
    expect: str | None = None,
    timeout: float | None = None,
    host: str | None = None,
    until: Callable[[str], bool] | None = None,
) -> str | None:
    """eiscp.send to `host` (default: ONKYO_HOST, required if unset)."""
    host = host or HOST
    if not host:
        raise ValueError("no receiver address: pass host, or set ONKYO_HOST")
    return await eiscp.send(host, PORT, command, expect, timeout or TIMEOUT, until)


# Each receiver describes itself in XML (NRIQSTN): model, inputs, network
# services and zones, e.g. for a TX-NR6050:
#   <zone id="2" value="1" name="Zone2" volmax="100" .../>   present
#   <zone id="3" value="0" name="Zone3" volmax="0" .../>     absent
# volmax="0" on a present zone means it has no volume control (fixed-level
# output). Fetched once per receiver, as it only changes with the setup.
_layouts: dict[str, dict | None] = {}


async def zone_layout(host: str) -> dict | None:
    """{"model": "TX-NR6050", "zones": {"zone2": {"present": True, "volume": True},
    "zone3": {...}}}, or None if the receiver doesn't describe itself."""
    key = f"{host}:{PORT}"
    if key not in _layouts:
        xml = await send("NRIQSTN", expect="NRI", host=host, timeout=3 * TIMEOUT)
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:  # "N/A": an older model without NRI
            _layouts[key] = None
            return None
        zones = {}
        for zone in root.iter("zone"):
            name = {"2": "zone2", "3": "zone3"}.get(zone.get("id", ""))
            if name:
                zones[name] = {"present": zone.get("value") == "1", "volume": zone.get("volmax", "0") != "0"}
        _layouts[key] = {"model": root.findtext(".//model") or "receiver", "zones": zones}
    return _layouts[key]


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
SETTER = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

# Playback controls change state but aren't idempotent: "next" twice skips
# two tracks.
PLAYBACK = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


# Discovery reads nothing but replies, so it is read-only. It is open-world,
# though: it broadcasts to the whole LAN and lists whatever answers.
@mcp.tool(title="Discover receivers", annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def discover_receivers() -> list[dict]:
    """Find Onkyo/Integra/Pioneer receivers on the local network. Returns each
    receiver's IP address, model, eISCP port and MAC address."""
    return await discover()


# Optional, so single-receiver setups (ONKYO_HOST, or one receiver found by
# discovery) keep working unchanged.
# The Field description lands in the tool's JSON Schema next to the type.
Receiver = Annotated[
    str | None,
    Field(
        description="IP address of the receiver, as returned by discover_receivers. Omit to use the default receiver."
    ),
]


_discovered_host: str | None = None


async def resolve_host(receiver: str | None) -> str:
    """The receiver to talk to: the one the model named, else ONKYO_HOST, else
    the only receiver that answers discovery (remembered for later calls)."""
    global _discovered_host
    if receiver or HOST:
        return receiver or HOST
    if _discovered_host is None:
        found = await discover(timeout=2.0)
        if len(found) > 1:
            listing = ", ".join(f"{r['model']} at {r['host']}" for r in found)
            raise ToolError(f"Several receivers found ({listing}): pass the one you want as receiver.")
        if not found:
            raise ToolError(
                "No receiver is configured and none answered discovery. "
                "Set ONKYO_HOST to the receiver's IP address (see the "
                "README: When discovery finds nothing)."
            )
        _discovered_host = found[0]["host"]
        log.info("Using %s at %s (found by discovery)", found[0]["model"], _discovered_host)
    return _discovered_host


async def call_receiver(
    command: str,
    expect: str | None,
    receiver: str | None,
    timeout: float | None = None,
    zone: Zone = "main",
    no_reply: str | None = None,
    until: Callable[[str], bool] | None = None,
) -> str | None:
    """send() for tools: turns network failures into a ToolError whose message
    tells the model what went wrong.

    Why ToolError: when a tool raises any other exception, the SDK treats it as
    a crash and keeps the details on the server. The model only sees
    "Error executing tool set_volume", so it can't tell the user anything
    useful. A ToolError is a failure we raised on purpose, and its message is
    sent to the model as an isError result.

    `no_reply` replaces the guesswork below with a specific message, for
    commands where silence has an obvious meaning."""
    host = await resolve_host(receiver)
    power_code = ZONE_CODES[zone]["power"]
    # Starts every error message: "The receiver at ..." / "Zone 2 of the receiver at ..."
    who = f"The receiver at {host}" if zone == "main" else f"{ZONE_LABELS[zone]} of the receiver at {host}"
    try:
        return await send(command, expect=expect, host=host, timeout=timeout, until=until)
    except TimeoutError as exc:
        # Connected, but no reply. Work out the likeliest reason.
        if no_reply:
            raise ToolError(no_reply) from exc
        if expect == power_code and zone != "main":
            # A working zone answers its power command even in standby. So
            # silence means it doesn't exist (TX-NR6050 zone 3), or isn't set
            # up (TX-NR7100 zone 3 answers queries but ignores power-on).
            label = ZONE_LABELS[zone]
            raise ToolError(
                f"The receiver at {host} didn't answer for {label}: it doesn't "
                f"have {label}, or {label} isn't set up in its speaker "
                "configuration."
            ) from exc
        if expect != power_code and not command.endswith("QSTN"):
            # A setter got no reply. A zone in standby still answers queries,
            # but some receivers (TX-NR7100) silently ignore setters, so ask
            # the zone's power state.
            try:
                power = await send(f"{power_code}QSTN", expect=power_code, host=host)
            except OSError:  # includes TimeoutError
                power = None
            if power == "00":
                how = "set_power" if zone == "main" else f"set_power with zone={zone!r}"
                raise ToolError(f"{who} is in standby. Turn it on with {how} first.") from exc
        raise ToolError(
            f"{who} didn't reply in time. If it was "
            "just turned on, it may still be starting up (some models take "
            "about 15 seconds): wait a few seconds and try again."
        ) from exc
    except OSError as exc:
        raise ToolError(
            f"Can't connect to a receiver at {host} ({exc}). Check the "
            "IP address, and that the receiver is on the network."
        ) from exc


# Optional too, defaulting to the main zone (the room the receiver is in)
ZoneArg = Annotated[
    Zone,
    Field(
        description='Which zone: "main" is the room the receiver is in; "zone2" and '
        '"zone3" are speakers in other rooms. Not every receiver has zone3.'
    ),
]


@mcp.tool(title="Get receiver status", annotations=READ_ONLY)
async def get_status(receiver: Receiver = None, zone: ZoneArg = "main") -> dict:
    """Get the status of one zone: pass zone="zone2" (or "zone3") for another
    room; the default is the main zone. Returns power state, volume (0-100),
    mute state and selected input, plus, for the main zone, the listening
    mode and "other_zones": the power state of each other zone the receiver
    has, so you know which to ask about. If there are several receivers on
    the network, call discover_receivers first and pass the one you want."""
    host = await resolve_host(receiver)
    await check_zone(host, zone)
    codes = ZONE_CODES[zone]
    power = await call_receiver(f"{codes['power']}QSTN", codes["power"], host, zone=zone)
    if power == "N/A":
        raise ToolError(f"The receiver at {host} doesn't have {ZONE_LABELS[zone]}.")
    volume = await call_receiver(f"{codes['volume']}QSTN", codes["volume"], host, zone=zone)
    mute = await call_receiver(f"{codes['mute']}QSTN", codes["mute"], host, zone=zone)
    source = await call_receiver(f"{codes['input']}QSTN", codes["input"], host, zone=zone)
    status = {
        "receiver": host,  # so the model can tell answers from different receivers apart
        "zone": zone,
        "power": "on" if power == "01" else "standby",
        "volume": raw_to_volume(volume) if volume and volume != "N/A" else None,
        "muted": mute == "01",
        # Unknown codes (not in our tables) are shown raw, e.g. "SLI2C"
        "input": CODE_SOURCES.get(source, f"{codes['input']}{source}") if source and source != "N/A" else None,
    }
    if zone == "main":  # zones 2/3 have no surround processing
        mode = await call_receiver("LMDQSTN", "LMD", host)
        status["listening_mode"] = CODE_MODES.get(mode, f"LMD{mode}") if mode and mode != "N/A" else None
        status["other_zones"] = await other_zones(host)
    return status


async def other_zones(host: str) -> dict[str, str]:
    """Power state of each zone besides main that the receiver says it has,
    e.g. {"zone2": "on"}. Best effort: a zone that doesn't answer is left out."""
    try:
        layout = await zone_layout(host)
    except OSError:  # includes TimeoutError
        return {}
    zones = {}
    for zone, info in (layout or {"zones": {}})["zones"].items():
        if info["present"]:
            code = ZONE_CODES[zone]["power"]
            try:
                power = await send(f"{code}QSTN", expect=code, host=host)
            except OSError:
                continue
            zones[zone] = "on" if power == "01" else "standby"
    return zones


async def check_zone(receiver: str | None, zone: Zone, volume: bool = False) -> None:
    """Fail fast, with a clear reason, for a zone the receiver says it doesn't
    have (or, with volume=True, can't change the volume of). Without this, the
    receiver just stays silent and the model gets a timeout after seconds."""
    if zone == "main":
        return
    host = await resolve_host(receiver)
    try:
        layout = await zone_layout(host)
    except OSError:  # includes TimeoutError
        return  # can't tell: let the command itself find out
    info = layout and layout["zones"].get(zone)
    if not info:
        return
    label = ZONE_LABELS[zone]
    if not info["present"]:
        raise ToolError(f"The {layout['model']} at {host} has no {label}.")
    if volume and not info["volume"]:
        raise ToolError(
            f"{label} of the {layout['model']} at {host} has no volume control "
            "(fixed-level output, or its outputs are used for other speakers)."
        )


def zone_prefix(zone: Zone) -> str:
    # Replies for the main zone read as before ("Volume is now 30.0"); other
    # zones say which one they're about ("Zone 2: Volume is now 30.0").
    return "" if zone == "main" else f"{ZONE_LABELS[zone]}: "


@mcp.tool(title="Set power", annotations=SETTER)
async def set_power(on: bool, receiver: Receiver = None, zone: ZoneArg = "main") -> str:
    """Turn any zone on or into standby: the main zone by default, or another
    room with zone="zone2" / "zone3". Zones are independent: zone2 can play
    while the main zone is in standby. After power-on, some receivers need
    about 15 seconds before they accept other commands."""
    await check_zone(receiver, zone)
    code = ZONE_CODES[zone]["power"]
    # Power changes are slow to confirm (a TX-NR7100 takes ~10s to reach standby)
    reply = await call_receiver(f"{code}01" if on else f"{code}00", code, receiver, timeout=3 * TIMEOUT, zone=zone)
    if reply == "N/A":
        raise ToolError(f"The receiver rejected the command: it may not have {ZONE_LABELS[zone]}.")
    if reply == "01":
        # The TX-NR7100 confirms power-on, then ignores commands for ~15s
        return (
            f"{zone_prefix(zone)}Power is now on. Some receivers need about 15 "
            "seconds to start up before they accept other commands."
        )
    return f"{zone_prefix(zone)}Power is now standby"


@mcp.tool(title="Set volume", annotations=SETTER)
async def set_volume(level: float, receiver: Receiver = None, zone: ZoneArg = "main") -> str:
    """Set the volume of any zone: the main zone by default, or another room
    with zone="zone2" / "zone3". Uses the receiver's 0-100 display scale (0.5
    steps on newer models), the same for every zone. Values above the
    configured safety cap are clamped; the cap applies to every zone."""
    await check_zone(receiver, zone, volume=True)
    clamped = max(0.0, min(level, MAX_VOLUME))
    code = ZONE_CODES[zone]["volume"]
    reply = await call_receiver(f"{code}{volume_to_raw(clamped)}", code, receiver, zone=zone)
    if reply == "N/A":
        return (
            f"{zone_prefix(zone)}Receiver rejected the volume change. The zone may be "
            "off, or its volume may be fixed in the receiver's setup (zones that "
            "feed another amplifier often are)."
        )
    note = f" (requested {level}, capped at {MAX_VOLUME})" if clamped != level else ""
    return f"{zone_prefix(zone)}Volume is now {raw_to_volume(reply)}{note}"


@mcp.tool(title="Set mute", annotations=SETTER)
async def set_mute(muted: bool, receiver: Receiver = None, zone: ZoneArg = "main") -> str:
    """Mute or unmute any zone: the main zone by default, or another room
    with zone="zone2" / "zone3"."""
    await check_zone(receiver, zone)
    code = ZONE_CODES[zone]["mute"]
    reply = await call_receiver(f"{code}01" if muted else f"{code}00", code, receiver, zone=zone)
    if reply == "N/A":
        return f"{zone_prefix(zone)}Receiver rejected the mute change (is the zone on?)"
    return zone_prefix(zone) + ("Muted" if reply == "01" else "Unmuted")


@mcp.tool(title="Select input", annotations=SETTER)
async def set_input(source: Source, receiver: Receiver = None, zone: ZoneArg = "main") -> str:
    """Select the input of any zone: the main zone by default, or another
    room with zone="zone2" / "zone3". Names match the receiver's front-panel
    labels (e.g. "bd-dvd" for the BD/DVD input, "net" for network streaming).
    "same-as-main" (zone2/zone3 only) plays whatever the main zone is playing.
    The zone must be on."""
    if source == "same-as-main" and zone == "main":
        raise ToolError('"same-as-main" only applies to zone2 and zone3.')
    await check_zone(receiver, zone)
    code = ZONE_CODES[zone]["input"]
    reply = await call_receiver(f"{code}{SOURCE_CODES[source]}", code, receiver, zone=zone)
    if reply == "N/A":
        return f"{zone_prefix(zone)}Receiver rejected input {source!r} (is it powered on?)"
    return f"{zone_prefix(zone)}Input is now {CODE_SOURCES.get(reply, f'{code}{reply}')}"


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


@mcp.tool(title="Select network service", annotations=SETTER)
async def select_net_service(service: NetService, receiver: Receiver = None) -> str:
    """Switch the receiver's network audio to a streaming service, e.g.
    Pandora. There is one network player per receiver, shared by every zone
    whose input is "net": set a zone's input to "net" (set_input) to hear it.
    The service must be offered by this receiver and signed in (usually in the
    Onkyo Controller app). Switching stops whatever another service was
    playing. "airplay" and "spotify" are normally started from a
    phone (AirPlay, Spotify Connect); selecting them here may only make the
    receiver wait for one. Call get_now_playing afterwards to see what plays."""
    code = NET_SERVICE_CODES[service]
    # NSV gets no reply of its own. The receiver confirms by pushing the title
    # of its new menu: "NLT" + the service code + 20 status characters + the
    # service's name, e.g. "NLT0401000000480100FF0400Pandora".
    reply = await call_receiver(
        f"NSV{code}0",
        f"NLT{code}",
        receiver,  # "0": no account details included
        no_reply=f"The receiver didn't switch to {service}. It may not offer {service}, "
        "or it isn't signed in: check in the Onkyo Controller app.",
    )
    if reply[0] in "34":  # the screen is a popup or keyboard, not the service's menu
        raise ToolError(not_ready(service, reply[20:]))
    return f"Network service is now {reply[20:] or service}"


def not_ready(service: str, screen: str) -> str:
    # A signed-out service opens a popup instead of its menu: "TIDAL Login",
    # "Amazon Music Sign In", "Try Deezer Premium+" (no account)
    return (
        f'{service} isn\'t ready: the receiver shows "{screen}". It needs '
        "signing in (or a subscription), which you can do in the Onkyo "
        "Controller app."
    )


@mcp.tool(title="Get now playing", annotations=READ_ONLY)
async def get_now_playing(receiver: Receiver = None) -> dict:
    """What the receiver's network player is playing: the service, play state,
    title, artist, album and position, plus the menu its screen is showing
    (which can differ: browsing doesn't stop playback). Every zone whose input
    is "net" plays this; check get_status to see which zones are on "net"."""
    host = await resolve_host(receiver)
    # NMS (menu status) ends with the playing service's icon code, e.g.
    # "MxxxxS104" = Pandora. NLT is the menu on screen, e.g. "...NET" when
    # someone has gone back to the top menu while Pandora keeps playing.
    menu_status = await call_receiver("NMSQSTN", "NMS", host)
    menu = await call_receiver("NLTQSTN", "NLT", host)
    state = await call_receiver("NSTQSTN", "NST", host)
    title = await call_receiver("NTIQSTN", "NTI", host)
    artist = await call_receiver("NATQSTN", "NAT", host)
    album = await call_receiver("NALQSTN", "NAL", host)
    position = await call_receiver("NTMQSTN", "NTM", host)
    station = await call_receiver("NDNQSTN", "NDN", host)  # e.g. "Pearl Jam Radio"
    return {
        "receiver": host,
        "service": CODE_NET_SERVICES.get(menu_status[-2:]),  # None at the top menu ("F3")
        "station": None if station in ("", "N/A") else station.strip() or None,
        # NLT: service code, 20 status characters, then the menu's title
        "menu": menu[22:] or None,
        "state": PLAY_STATES.get(state[:1], state or None),
        "title": title.strip() or None,
        "artist": artist.strip() or None,
        "album": album.strip() or None,
        "position": None if position.startswith("--") else position,  # "00:04:31/00:05:38"
    }


# ---------------------------------------------------------------------------
# Stations and playback. A service's top menu (for Pandora: your stations)
# comes from two commands:
#   NSV0401 -> pushes "NLT0401000000480100FF0400Pandora": service 04, list UI
#              (0), service top layer (1), 0x48 = 72 items, layer number 01
#   NLAL0001 01 0000 0048 -> "NLAX0001S000<?xml ...><item icontype="M"
#              title="Pearl Jam Radio" .../>..." (every item, as XML)
# Items with icontype "M" are music (stations, "Shuffle"), and "0" marks the
# one playing now; the rest are things like "Create new station" (G) and
# "Account Info"/"Sign Out" (-), which must never be selected. "NLSI00003" plays item 3 (counting from 1).
# ---------------------------------------------------------------------------

# A list item: (position from 1, icontype, title)
Item = tuple[int, str, str]
LIST_PAGE = 100  # items per NLA request


async def read_list(host: str, nlt: str) -> list[Item]:
    """Every item of the menu the receiver is showing. `nlt` is its NLT title
    info: service (2), UI type, layer type, cursor (4 hex), item count (4 hex),
    layer number (2 hex), ..."""
    count, layer = min(int(nlt[8:12], 16), 0xFFF), nlt[12:14]
    items: list[Item] = []
    # In pages: a TX-NR6050 takes 5.3s to send 700 albums in one reply (over
    # the timeout), but 0.3s per 100.
    for start in range(0, count, LIST_PAGE):
        # Expect "NLAX", not "NLA": send() would take "NLAL..." for a setter
        # and wait for it to be echoed back, which never happens.
        reply = await call_receiver(f"NLAL0001{layer}{start:04X}{min(LIST_PAGE, count - start):04X}", "NLAX", host)
        if reply[4:5] != "S":  # "0001S000<?xml..." = success
            raise ToolError("The receiver couldn't list this menu.")
        page = ElementTree.fromstring(reply[8:]).iter("item")
        items += [
            (position, item.get("icontype", ""), item.get("title", ""))
            for position, item in enumerate(page, start=start + 1)
        ]
    return items


def pick(wanted: str, items: list[Item], what: str) -> Item:
    """The item named `wanted`: an exact match (ignoring case), else the only
    one containing it. Duplicates (same title twice) count as one."""
    key = wanted.casefold().strip()
    matches = [i for i in items if i[2].casefold() == key] or [i for i in items if key in i[2].casefold()]
    names = list(dict.fromkeys(title for _, _, title in matches))
    if not names:
        available = ", ".join(dict.fromkeys(t for _, _, t in items[:30])) or "nothing"
        raise ToolError(f"No {what} matching {wanted!r}. Here: {available}" + (" ..." if len(items) > 30 else "") + ".")
    if len(names) > 1:
        raise ToolError(f"{wanted!r} matches several {what}s: {', '.join(names[:10])}. Which one?")
    return matches[0]


async def open_menu(service: NetService, host: str, folder: tuple[str, ...]) -> tuple[str, list[Item]]:
    """Open a service's top menu, then each folder in `folder` in turn.
    Returns the NLT title info and items of the menu reached."""
    code = NET_SERVICE_CODES[service]
    # "NLT<code>01": a list (0) at the service's top layer (1). The playback
    # screen pushes "NLT<code>22..." while music plays, which isn't the menu.
    try:
        rest = await call_receiver(
            f"NSV{code}0",
            f"NLT{code}01",
            host,
            no_reply=f"The receiver didn't open {service}. It may not offer {service}, "
            "or it isn't signed in: check in the Onkyo Controller app.",
        )
    except ToolError:
        # Maybe it opened a popup instead ("NLT1B31...TIDAL Login")
        shown = await call_receiver("NLTQSTN", "NLT", host)
        if shown.startswith(code) and shown[2:3] in ("3", "4"):
            raise ToolError(not_ready(service, shown[22:])) from None
        raise
    nlt = f"{code}01{rest}"
    items = await read_list(host, nlt)
    for name in folder:
        position, _, title = pick(name, [i for i in items if i[1] == "F"], "folder")
        # The receiver announces the folder it opened with its title info,
        # but the previous menu's info keeps arriving too, with the same
        # prefix and screen type. So wait for the layer number one deeper.
        # Opening can take a few seconds (a music server answering), hence
        # the longer timeout.
        layer = f"{int(nlt[12:14], 16) + 1:02X}"
        rest = await call_receiver(
            f"NLSI{position:05d}",
            f"NLT{code}",
            host,
            timeout=3 * TIMEOUT,
            until=lambda value, layer=layer: value[10:12] == layer,  # value: after "NLT" + code
            no_reply=f"The receiver didn't open the folder {title!r}.",
        )
        nlt = code + rest
        items = await read_list(host, nlt)
    return nlt, items


FolderPath = Annotated[
    tuple[str, ...],
    Field(
        description="Folders to open from the service's top menu, in order, e.g. "
        '["My Presets"] or ["MiniDLNA Server", "Music", "Album", "21"]. '
        "A distinctive part of each name is enough. Empty for the top menu."
    ),
]
LIST_LIMIT = 300  # names per kind; a music server's Album folder can hold thousands


@mcp.tool(title="List stations", annotations=SETTER)
async def list_stations(service: NetService = "pandora", folder: FolderPath = (), receiver: Receiver = None) -> dict:
    """Browse a network service: list what can be played (stations, tracks)
    and the folders at one level of its menu. Starts at the top (for Pandora:
    your stations); to look inside a folder, call again with its name added to
    `folder` (e.g. TuneIn: ["My Presets"]). Pass a playable name, with the
    same `folder`, to play_station. Browsing the service that's playing doesn't
    interrupt it, but opening a different service stops the current music:
    check get_now_playing first, and ask before browsing another service
    while something plays."""
    _, items = await open_menu(service, await resolve_host(receiver), folder)
    # "M" = music, "0" = playing now, "F" = folder; anything else is a
    # message ("No Favorites available") or an account item ("Sign Out")
    playable = list(dict.fromkeys(t for _, kind, t in items if kind in ("M", "0")))
    folders = list(dict.fromkeys(t for _, kind, t in items if kind == "F"))
    result: dict = {
        "service": service,
        "folder": folder,
        "playable": playable[:LIST_LIMIT],
        "folders": folders[:LIST_LIMIT],
    }
    if len(playable) > LIST_LIMIT or len(folders) > LIST_LIMIT:
        result["truncated"] = (
            f"There are {len(playable)} playable items and "
            f"{len(folders)} folders; only the first {LIST_LIMIT} "
            "of each are listed. Names further down still work."
        )
    if not playable and not folders:
        result["message"] = ", ".join(t for _, _, t in items) or "This menu is empty."
    return result


@mcp.tool(title="Play station", annotations=SETTER)
async def play_station(
    station: str, service: NetService = "pandora", folder: FolderPath = (), receiver: Receiver = None
) -> str:
    """Start playing a station or track from a network service, by name (e.g.
    "Pearl Jam Radio" on Pandora). Names come from list_stations; a
    distinctive part of a name is enough ("pearl jam"). For items inside
    folders (TuneIn presets, a music server's albums), pass the same `folder`
    list_stations used. It plays in every zone whose input is "net": set a
    zone's input to "net" first to hear it."""
    host = await resolve_host(receiver)
    _, items = await open_menu(service, host, folder)
    playable = [i for i in items if i[1] in ("M", "0")]  # never "Sign Out" and the like
    if not playable:
        folders = [t for _, kind, t in items if kind == "F"]
        hint = f" It has folders: {', '.join(folders[:30])}; add one to `folder` to look inside." if folders else ""
        raise ToolError(f"Nothing here can be played.{hint}")
    position, _, title = pick(station, playable, "station")
    # Confirmed when the player reports "playing" (NST "P..."), after ~3s
    await call_receiver(
        f"NLSI{position:05d}",
        "NSTP",
        host,
        timeout=3 * TIMEOUT,
        no_reply=f"{title} was selected but didn't start playing.",
    )
    return f"Playing {title} on {service}"


PlaybackAction = Literal["play", "pause", "stop", "next", "previous"]
# The NTC command for each action, and the NST play state that confirms it
PLAYBACK_CODES = {
    "play": ("PLAY", "P"),
    "pause": ("PAUSE", "p"),
    "stop": ("STOP", "S"),
    "next": ("TRUP", None),
    "previous": ("TRDN", None),
}


@mcp.tool(title="Control playback", annotations=PLAYBACK)
async def control_playback(action: PlaybackAction, receiver: Receiver = None) -> str:
    """Play, pause, stop, or skip to the next/previous track on the network
    player (shared by every zone on "net"). "play" resumes what was paused;
    to start a station, use play_station. Services limit skipping: Pandora
    allows a few skips per hour and can't go back."""
    host = await resolve_host(receiver)
    code, state = PLAYBACK_CODES[action]
    if state:
        await call_receiver(
            f"NTC{code}",
            f"NST{state}",
            host,
            no_reply=f"The player didn't {action}. Is something selected? Start a station with play_station.",
        )
        return {"play": "Playing", "pause": "Paused", "stop": "Stopped"}[action]
    # A skip has no state to wait for: watch for the title to change
    before = await call_receiver("NTIQSTN", "NTI", host)
    await call_receiver(f"NTC{code}", None, host)
    for _ in range(int(TIMEOUT / 0.5)):
        await asyncio.sleep(0.5)
        title = await call_receiver("NTIQSTN", "NTI", host)
        if title != before:
            return f"Now playing {title.strip()}"
    raise ToolError(
        "The track didn't change. The service may not allow that "
        "right now (Pandora limits skips per hour and can't go back)."
    )


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    from . import remote as onkyo_remote

    parser = argparse.ArgumentParser(prog="mcp-server-onkyo", description="MCP server for Onkyo receivers")
    parser.add_argument("--debug", action="store_true", help="log MCP and eISCP traffic to stderr")
    parser.add_argument("--discover", action="store_true", help="list receivers on the LAN and exit")
    onkyo_remote.add_http_arguments(parser, default_port=8711, default_path="/onkyo/mcp")
    args = parser.parse_args(argv)

    if DEBUG or args.debug:
        enable_debug()

    if args.discover:  # quick CLI check, no MCP involved
        receivers = asyncio.run(discover())
        for r in receivers:
            print(f"{r['host']:<16} {r['model']:<12} {r['mac']}  port {r['port']}")
        if not receivers:
            print("No receivers answered. See README: Troubleshooting.", file=sys.stderr)
    elif args.http:
        # A long-lived HTTP service, e.g. in an LXC behind Cloudflare Access
        # (see remote.py). Its logs go to stderr like everything else.
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
        try:
            onkyo_remote.serve_http(mcp, onkyo_remote.http_config(args))
        except onkyo_remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-onkyo: {exc}\n")
    else:
        # Defaults to stdio transport: JSON-RPC over stdin/stdout, which is why
        # nothing in the server may print() to stdout.
        mcp.run()


if __name__ == "__main__":
    main()
