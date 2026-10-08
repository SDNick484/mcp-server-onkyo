"""MCP tools for Onkyo receivers.

How a decorated function becomes a tool (same as the sibling projects):
  - ``@mcp.tool()`` registers it; it then appears in the client's ``tools/list``.
  - The *docstring* becomes the tool's description. It is the model's only
    documentation, so it is written for the model, not for us.
  - The *signature* becomes the ``inputSchema`` (JSON Schema): ``Literal``
    turns into an enum, ``Field(ge=..., le=...)`` into minimum/maximum, and
    defaults make arguments optional. The SDK validates every call against it.

The layers underneath: eiscp.py speaks the protocol, receivers.py decides
which receiver a call means and gives one call at a time exclusive use of it,
config.py says which receivers exist and what the limits are.

Run (stdio transport, which is what Claude Code uses for local servers):
    ONKYO_HOST=192.168.1.50 mcp-server-onkyo        # or: python -m onkyo_mcp

Inspect interactively (shows tools/list, lets you call tools by hand):
    npx @modelcontextprotocol/inspector mcp-server-onkyo

Log MCP and eISCP traffic to stderr (either works; also with --discover):
    ONKYO_DEBUG=1 mcp-server-onkyo
    mcp-server-onkyo --debug
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from xml.etree import ElementTree

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

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
    raw_to_volume,
    volume_to_raw,
)
from .config import Settings, load_settings
from .receivers import Receiver, ReceiverError, Registry, Session, Unreachable

# Everything goes through this logger (and its children), which writes to
# stderr: on the stdio transport, stdout belongs to JSON-RPC. Silent
# (WARNING) unless debugging.
log = logging.getLogger("onkyo_mcp")


def enable_debug() -> None:
    """Log every MCP message and eISCP packet to stderr, at DEBUG level."""
    handler = logging.StreamHandler()  # defaults to sys.stderr
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False  # the SDK configures the root logger too; don't log twice


# --- the registry, built once per server run -------------------------------------------
# Tests replace settings_factory to point the server at fake receivers.
settings_factory: Callable[[], Settings] = load_settings
_registry: Registry | None = None


def registry() -> Registry:
    assert _registry is not None, "server lifespan has not started"
    return _registry


# The lifespan runs once per server run (once per process over HTTP, shared by
# every session): the code before `yield` at startup, after it at shutdown.
@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global _registry
    settings = settings_factory()
    for problem in settings.problems:
        log.warning("Config: %s", problem)
    _registry = Registry(settings)
    try:
        yield
    finally:
        _registry = None


# The name is what the client sees in the initialize handshake (serverInfo.name);
# `instructions` is typically added to the model's context.
mcp = MCPServer(
    "onkyo",
    instructions=(
        "Controls Onkyo AV receivers on the local network. Call get_status first: it shows a receiver's "
        "zones (main, zone2, zone3) with power, volume, mute and input. Pass `receiver` (a name or address) "
        "when there are several, and `zone` for rooms other than the main one. Each receiver has one network "
        'player, shared by every zone whose input is "net".'
    ),
    lifespan=lifespan,
)


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


def to_json(value: object) -> str:
    if isinstance(value, BaseModel):
        # by_alias: inputSchema rather than input_schema, as on the wire
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(value, default=str)


async def log_traffic(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
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
# Talking to a receiver from a tool.
#
# Every tool runs its exchange inside `async with call(receiver) as c:`. That
# picks the receiver (receivers.py's never-guess rule), holds its lock, and
# keeps one TCP connection for the whole call. c.ask() turns network failures
# into a ReceiverError (a ToolError) whose message says what went wrong.
#
# Why ToolError: when a tool raises any other exception, the SDK treats it as
# a crash and keeps the details on the server. The model only sees
# "Error executing tool set_volume", so it can't tell the user anything
# useful. A ToolError is a failure we raised on purpose, and its message is
# sent to the model as an isError result.
# ---------------------------------------------------------------------------

_READ_ERRORS = (OSError, ValueError, asyncio.IncompleteReadError)  # OSError includes TimeoutError


class Call:
    """One tool call's exchange with one receiver."""

    def __init__(self, receiver: Receiver, session: Session, settings: Settings) -> None:
        self.receiver = receiver
        self.session = session
        self.settings = settings
        self.steps = settings.steps_for(receiver.settings)

    @property
    def host(self) -> str:
        return self.receiver.host

    def who(self, zone: Zone = "main") -> str:
        """Starts error messages: "The receiver at 10.0.0.2", "Zone 2 of Den (10.0.0.2)"."""
        name = self.receiver.settings.name
        subject = f"{name} ({self.host})" if name else f"the receiver at {self.host}"
        text = subject if zone == "main" else f"{ZONE_LABELS[zone]} of {subject}"
        return text[0].upper() + text[1:]

    async def ask(
        self,
        command: str,
        expect: str,
        *,
        zone: Zone = "main",
        timeout: float | None = None,
        no_reply: str | None = None,
        until: Callable[[str], bool] | None = None,
    ) -> str:
        """Send `command` and return the reply's value, or raise a ReceiverError.

        `no_reply` replaces the guesswork below with a specific message, for
        commands where silence has an obvious meaning."""
        power_code = ZONE_CODES[zone]["power"]
        try:
            return await self.session.request(command, expect, timeout, until)
        except TimeoutError as exc:
            # Connected, but no reply. Work out the likeliest reason.
            if no_reply:
                raise ReceiverError(no_reply) from exc
            if expect == power_code and zone != "main":
                # A working zone answers its power command even in standby. So
                # silence means it doesn't exist (TX-NR6050 zone 3), or isn't set
                # up (TX-NR7100 zone 3 answers queries but ignores power-on).
                label = ZONE_LABELS[zone]
                raise ReceiverError(
                    f"{self.who()} didn't answer for {label}: it doesn't have {label}, or {label} isn't set up in "
                    "its speaker configuration."
                ) from exc
            if expect != power_code and not command.endswith("QSTN"):
                # A setter got no reply. A zone in standby still answers queries,
                # but some receivers (TX-NR7100) silently ignore setters, so ask
                # the zone's power state.
                try:
                    power: str | None = await self.session.request(f"{power_code}QSTN", power_code)
                except _READ_ERRORS:
                    power = None
                if power == "00":
                    how = "set_power" if zone == "main" else f"set_power with zone={zone!r}"
                    raise ReceiverError(f"{self.who(zone)} is in standby. Turn it on with {how} first.") from exc
            raise Unreachable(
                f"{self.who(zone)} didn't reply in time. If it was just turned on, it may still be starting up "
                "(some models take about 15 seconds): wait a few seconds and try again."
            ) from exc
        except (ValueError, asyncio.IncompleteReadError) as exc:
            raise ReceiverError(
                f"{self.who()} sent a reply that couldn't be read ({exc}). Try again; if it keeps happening, run "
                "with --debug and look at the eISCP traffic."
            ) from exc
        except OSError as exc:
            raise Unreachable(
                f"Can't connect to {self.who()[0].lower()}{self.who()[1:]} ({exc}). Check the address, and that the "
                "receiver is on the network."
            ) from exc

    async def tell(self, command: str) -> None:
        """Send without waiting for a reply."""
        try:
            await self.session.write(command)
        except OSError as exc:
            raise ReceiverError(f"Can't send to {self.host} ({exc}).") from exc


@asynccontextmanager
async def call(receiver: str | None) -> AsyncIterator[Call]:
    reg = registry()
    r = await reg.pick(receiver)
    async with r.session(reg.settings.timeout) as session:
        yield Call(r, session, reg.settings)


# ---------------------------------------------------------------------------
# MCP layer: each @mcp.tool() becomes an entry in tools/list.
#
# Tool annotations are hints about a tool's *behavior*, sent in tools/list:
#   {"name": "set_volume", "title": "Set volume",
#    "annotations": {"readOnlyHint": false, "destructiveHint": false,
#                    "idempotentHint": true, "openWorldHint": false}, ...}
# Clients use them to decide how careful to be, e.g. auto-approving read-only
# tools but asking the user before destructive ones. They are only hints: a
# client should not trust them from a server it doesn't trust, and they are
# no substitute for real server-side limits like the volume cap.
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
async def discover_receivers() -> list[dict[str, Any]]:
    """Find Onkyo/Integra/Pioneer receivers on the local network. Returns each
    receiver's IP address, model, eISCP port and MAC address."""
    s = registry().settings
    return [dataclasses.asdict(r) for r in await eiscp.discover(s.discovery_addr, s.discovery_port)]


# Optional: with one receiver configured (or found), it's the default.
# The Field description lands in the tool's JSON Schema next to the type.
ReceiverArg = Annotated[
    str | None,
    Field(
        description="Which receiver: its name or IP address. Omit when there is only one; with several, a "
        "call without it is refused rather than guessed."
    ),
]

# Constraints in the type become JSON Schema the model sees ("minimum": 0,
# "maximum": 100), and the SDK rejects anything outside before our code runs.
VolumeLevel = Annotated[float, Field(ge=0, le=100, description="Volume on the front panel's 0-100 scale.")]

# Optional too, defaulting to the main zone (the room the receiver is in)
ZoneArg = Annotated[
    Zone,
    Field(
        description='Which zone: "main" is the room the receiver is in; "zone2" and '
        '"zone3" are speakers in other rooms. Not every receiver has zone3.'
    ),
]


# --- status -----------------------------------------------------------------------------
# get_status returns TypedDicts rather than dict[str, Any]: the SDK then
# publishes an outputSchema, so clients know the fields without guessing, and
# sends the value as structured content. They come from typing_extensions: on
# Python 3.11, Pydantic rejects typing.TypedDict and the SDK silently drops
# the schema (seen in mcp-server-shieldtv).
class ZoneStatus(TypedDict):
    zone: Zone
    power: Literal["on", "standby"] | None  # None: the zone didn't answer
    volume: float | None  # front-panel scale; None in standby or without volume control
    volume_cap: float  # the most set_volume will set in this zone
    volume_control: bool  # False: fixed-level output, or its amplifier drives other speakers
    muted: bool | None
    input: str | None  # a set_input name, or the raw code ("SLI2C") for inputs not in the table
    listening_mode: str | None  # main zone only


class ReceiverStatus(TypedDict):
    receiver: str  # its name, or its address: what to pass as `receiver`
    host: str
    model: str | None
    reachable: bool
    error: str | None  # why it's unreachable, when it is
    zones: list[ZoneStatus]
    # Zones that are on with input "net". They all play the receiver's one
    # network player, so a station started for one plays in all of them.
    net_zones: list[Zone]


class Status(TypedDict):
    dry_run: bool  # true: setters report what they would send, and send nothing
    receivers: list[ReceiverStatus]


ZoneFilter = Annotated[
    Zone | None,
    Field(description="Only this zone. Omit for every zone the receiver has (main, and zone2/zone3 if present)."),
]


def _value(reply: str) -> str | None:
    return None if reply in ("", "N/A") else reply


async def zone_status(c: Call, zone: Zone, volume_control: bool) -> ZoneStatus:
    codes = ZONE_CODES[zone]
    power = await c.ask(f"{codes['power']}QSTN", codes["power"], zone=zone)
    if power == "N/A":
        raise ReceiverError(f"{c.who()} doesn't have {ZONE_LABELS[zone]}.")
    volume = _value(await c.ask(f"{codes['volume']}QSTN", codes["volume"], zone=zone)) if volume_control else None
    mute = _value(await c.ask(f"{codes['mute']}QSTN", codes["mute"], zone=zone))
    source = _value(await c.ask(f"{codes['input']}QSTN", codes["input"], zone=zone))
    mode = _value(await c.ask("LMDQSTN", "LMD")) if zone == "main" else None  # zones 2/3 have no surround
    return {
        "zone": zone,
        "power": "on" if power == "01" else "standby",
        "volume": raw_to_volume(volume, c.steps) if volume else None,
        "volume_cap": c.settings.cap(zone),
        "volume_control": volume_control,
        "muted": None if mute is None else mute == "01",
        # Unknown codes (not in our tables) are shown raw, e.g. "SLI2C"
        "input": CODE_SOURCES.get(source, f"{codes['input']}{source}") if source else None,
        "listening_mode": CODE_MODES.get(mode, f"LMD{mode}") if mode else None,
    }


async def zones_of(c: Call) -> dict[Zone, bool]:
    """The zones this receiver has, each with whether it has volume control,
    from its self-description. Without one (older models), just the main zone."""
    try:
        layout = await c.session.layout()
    except _READ_ERRORS:
        layout = None
    zones: dict[Zone, bool] = {"main": True}
    if layout is not None:
        zones.update({z: info.volume for z, info in layout.zones.items() if info.present and z != "main"})
    return zones


async def receiver_status(c: Call, only: Zone | None) -> ReceiverStatus:
    zones = await zones_of(c)
    if only is not None:
        await check_zone(c, only)
        zones = {only: zones.get(only, True)}
    statuses = [await zone_status(c, z, control) for z, control in zones.items()]
    return {
        "receiver": c.receiver.label,  # so the model can tell answers from different receivers apart
        "host": c.host,
        "model": c.receiver.model,
        "reachable": True,
        "error": None,
        "zones": statuses,
        "net_zones": [z["zone"] for z in statuses if z["power"] == "on" and z["input"] == "net"],
    }


# --- the shared network player -----------------------------------------------------------
# A receiver has ONE network player. Every zone whose input is "net" plays
# it, so a service or station chosen "for Zone 2" also changes what the main
# zone hears if the main zone is on "net" too. The eISCP commands (NSV, NLS,
# NTC) don't name a zone, so there is no way to give zones different network
# audio; the tools say so instead of letting it surprise anyone.
async def zones_on_net(c: Call) -> list[Zone]:
    """The zones that are on with input "net". Best effort: a zone that
    doesn't answer is left out (this only feeds warnings)."""
    on_net: list[Zone] = []
    for zone in await zones_of(c):
        codes = ZONE_CODES[zone]
        try:
            power = await c.session.request(f"{codes['power']}QSTN", codes["power"])
            source = await c.session.request(f"{codes['input']}QSTN", codes["input"])
        except _READ_ERRORS:
            continue
        if power == "01" and source == SOURCE_CODES["net"]:
            on_net.append(zone)
    return on_net


def with_note(message: str, note: str) -> str:
    return f"{message}. {note}" if note else message


def shared_player_note(on_net: list[Zone]) -> str:
    """One sentence about who will hear the network player, or ""."""
    names = [ZONE_LABELS[z] for z in on_net]
    if not names:
        return (
            'No zone is on input "net" yet, so nothing will be heard: use set_input with source="net" for the '
            "zone that should play it."
        )
    if len(names) > 1:
        return (
            f'Note: {" and ".join(names)} are both on "net", and a receiver has one network player, so both '
            "hear this. To keep one zone out, give it another input."
        )
    return ""


@mcp.tool(title="Get receiver status", annotations=READ_ONLY)
async def get_status(receiver: ReceiverArg = None, zone: ZoneFilter = None) -> Status:
    """Start here. For each receiver, each zone's power, volume (0-100), the
    volume cap, mute and input, and the main zone's listening mode. Without
    `receiver`, covers every configured receiver; a receiver that can't be
    reached is listed with reachable=false and the reason. net_zones lists
    the zones playing the receiver's one, shared network player."""
    reg = registry()
    targets = [await reg.pick(receiver)] if receiver is not None or len(reg.receivers) <= 1 else reg.all()
    results: list[ReceiverStatus] = []
    for r in targets:
        try:
            async with r.session(reg.settings.timeout) as session:
                results.append(await receiver_status(Call(r, session, reg.settings), zone))
        except ReceiverError as exc:
            # An unreachable receiver is a status, reported alongside the rest.
            # Anything else about an explicitly requested zone (e.g. a zone the
            # receiver doesn't have) is the caller's mistake: say so.
            if zone is not None and not isinstance(exc, Unreachable):
                raise
            results.append(
                {
                    "receiver": r.label,
                    "host": r.host,
                    "model": r.model,
                    "reachable": False,
                    "error": str(exc),
                    "zones": [],
                    "net_zones": [],
                }
            )
    return {"dry_run": reg.settings.dry_run, "receivers": results}


async def check_zone(c: Call, zone: Zone, volume: bool = False) -> None:
    """Fail fast, with a clear reason, for a zone the receiver says it doesn't
    have (or, with volume=True, can't change the volume of). Without this, the
    receiver just stays silent and the model gets a timeout after seconds."""
    if zone == "main":
        return
    try:
        layout = await c.session.layout()
    except _READ_ERRORS:
        return  # can't tell: let the command itself find out
    info = layout.zones.get(zone) if layout else None
    if layout is None or info is None:
        return
    label = ZONE_LABELS[zone]
    if not info.present:
        raise ReceiverError(f"The {layout.model} at {c.host} has no {label}.")
    if volume and not info.volume:
        raise ReceiverError(
            f"{label} of the {layout.model} at {c.host} has no volume control "
            "(fixed-level output, or its outputs are used for other speakers)."
        )


def zone_prefix(zone: Zone) -> str:
    # Replies for the main zone read as before ("Volume is now 30.0"); other
    # zones say which one they're about ("Zone 2: Volume is now 30.0").
    return "" if zone == "main" else f"{ZONE_LABELS[zone]}: "


@mcp.tool(title="Set power", annotations=SETTER)
async def set_power(on: bool, receiver: ReceiverArg = None, zone: ZoneArg = "main") -> str:
    """Turn any zone on or into standby: the main zone by default, or another
    room with zone="zone2" / "zone3". Zones are independent: zone2 can play
    while the main zone is in standby. After power-on, some receivers need
    about 15 seconds before they accept other commands."""
    async with call(receiver) as c:
        await check_zone(c, zone)
        code = ZONE_CODES[zone]["power"]
        # Power changes are slow to confirm (a TX-NR7100 takes ~10s to reach standby)
        timeout = 3 * c.settings.timeout
        reply = await c.ask(f"{code}01" if on else f"{code}00", code, timeout=timeout, zone=zone)
    if reply == "N/A":
        raise ReceiverError(f"The receiver rejected the command: it may not have {ZONE_LABELS[zone]}.")
    if reply == "01":
        # The TX-NR7100 confirms power-on, then ignores commands for ~15s
        return (
            f"{zone_prefix(zone)}Power is now on. Some receivers need about 15 "
            "seconds to start up before they accept other commands."
        )
    return f"{zone_prefix(zone)}Power is now standby"


@mcp.tool(title="Set volume", annotations=SETTER)
async def set_volume(level: VolumeLevel, receiver: ReceiverArg = None, zone: ZoneArg = "main") -> str:
    """Set the volume of any zone: the main zone by default, or another room
    with zone="zone2" / "zone3". Uses the receiver's 0-100 display scale (0.5
    steps on newer models), the same for every zone. Each zone has a safety
    cap set by the owner (get_status shows it); a higher level is lowered to
    the cap, and the reply says so."""
    async with call(receiver) as c:
        await check_zone(c, zone, volume=True)
        cap = c.settings.cap(zone)
        code = ZONE_CODES[zone]["volume"]
        reply = await c.ask(f"{code}{volume_to_raw(level, c.steps, cap)}", code, zone=zone)
        steps = c.steps
    if reply == "N/A":
        raise ReceiverError(
            f"{zone_prefix(zone)}The receiver rejected the volume change. The zone may be off, or its volume "
            "may be fixed in the receiver's setup (zones that feed another amplifier often are)."
        )
    now = raw_to_volume(reply, steps)
    note = f" (requested {level:g}, capped at {cap:g})" if level > cap else ""
    return f"{zone_prefix(zone)}Volume is now {now}{note}"


@mcp.tool(title="Set mute", annotations=SETTER)
async def set_mute(muted: bool, receiver: ReceiverArg = None, zone: ZoneArg = "main") -> str:
    """Mute or unmute any zone: the main zone by default, or another room
    with zone="zone2" / "zone3"."""
    async with call(receiver) as c:
        await check_zone(c, zone)
        code = ZONE_CODES[zone]["mute"]
        reply = await c.ask(f"{code}01" if muted else f"{code}00", code, zone=zone)
    if reply == "N/A":
        raise ReceiverError(f"{zone_prefix(zone)}The receiver rejected the mute change. Is the zone on?")
    return zone_prefix(zone) + ("Muted" if reply == "01" else "Unmuted")


@mcp.tool(title="Select input", annotations=SETTER)
async def set_input(source: Source, receiver: ReceiverArg = None, zone: ZoneArg = "main") -> str:
    """Select the input of any zone: the main zone by default, or another
    room with zone="zone2" / "zone3". Names match the receiver's front-panel
    labels (e.g. "bd-dvd" for the BD/DVD input, "net" for network streaming).
    "same-as-main" (zone2/zone3 only) plays whatever the main zone is playing.
    The zone must be on."""
    if source == "same-as-main" and zone == "main":
        raise ReceiverError('"same-as-main" only applies to zone2 and zone3.')
    async with call(receiver) as c:
        await check_zone(c, zone)
        code = ZONE_CODES[zone]["input"]
        reply = await c.ask(f"{code}{SOURCE_CODES[source]}", code, zone=zone)
        note = ""
        if reply != "N/A" and source == "net":
            others = [z for z in await zones_on_net(c) if z != zone]
            if others:
                listing = " and ".join(ZONE_LABELS[z] for z in others)
                note = (
                    f"It now plays the same network audio as {listing}: a receiver has one network player, so "
                    'changing the service or station changes it in every zone on "net".'
                )
    if reply == "N/A":
        raise ReceiverError(
            f"{zone_prefix(zone)}The receiver rejected input {source!r}. Is the zone on, and does this model "
            "have that input?"
        )
    return with_note(f"{zone_prefix(zone)}Input is now {CODE_SOURCES.get(reply, f'{code}{reply}')}", note)


@mcp.tool(title="Set listening mode", annotations=SETTER)
async def set_listening_mode(mode: ListeningMode, receiver: ReceiverArg = None) -> str:
    """Set the main zone's listening mode (surround processing). "direct" and
    "pure-audio" play the source unprocessed; "dolby-surround" and
    "dts-neural-x" upmix to all speakers and play Dolby Atmos / DTS:X content
    natively. The receiver must be on, and may reject modes that don't suit
    the current input signal."""
    async with call(receiver) as c:
        reply = await c.ask(f"LMD{MODE_CODES[mode]}", "LMD")
    if reply == "N/A":
        raise ReceiverError(
            f"The receiver rejected listening mode {mode!r}: it may be off, or the mode may not suit the "
            "current input signal (e.g. dts-neural-x on a PCM stereo source)."
        )
    return f"Listening mode is now {CODE_MODES.get(reply, f'LMD{reply}')}"


@mcp.tool(title="Select network service", annotations=SETTER)
async def select_net_service(service: NetService, receiver: ReceiverArg = None) -> str:
    """Switch the receiver's network audio to a streaming service, e.g.
    Pandora. There is one network player per receiver, shared by every zone
    whose input is "net": set a zone's input to "net" (set_input) to hear it.
    The service must be offered by this receiver and signed in (usually in the
    Onkyo Controller app). Switching stops whatever another service was
    playing. "airplay" and "spotify" are normally started from a
    phone (AirPlay, Spotify Connect); selecting them here may only make the
    receiver wait for one. Call get_now_playing afterwards to see what plays."""
    code = NET_SERVICE_CODES[service]
    async with call(receiver) as c:
        # NSV gets no reply of its own. The receiver confirms by pushing the title
        # of its new menu: "NLT" + the service code + 20 status characters + the
        # service's name, e.g. "NLT0401000000480100FF0400Pandora".
        reply = await c.ask(
            f"NSV{code}0",  # "0": no account details included
            f"NLT{code}",
            no_reply=f"The receiver didn't switch to {service}. It may not offer {service}, "
            "or it isn't signed in: check in the Onkyo Controller app.",
        )
        note = shared_player_note(await zones_on_net(c)) if reply[:1] not in ("3", "4") else ""
    if reply[:1] in ("3", "4"):  # the screen is a popup or keyboard, not the service's menu
        raise ReceiverError(not_ready(service, reply[20:]))
    return with_note(f"Network service is now {reply[20:] or service}", note)


class NetServiceInfo(TypedDict):
    code: str  # the receiver's NSV code
    receiver_name: str  # what the receiver calls it
    name: str | None  # what select_net_service calls it; None if this server has no name for it
    selectable: bool


class NetServices(TypedDict):
    receiver: str
    services: list[NetServiceInfo]


@mcp.tool(title="List network services", annotations=READ_ONLY)
async def list_net_services(receiver: ReceiverArg = None) -> NetServices:
    """The streaming services this receiver offers, as it lists them itself.
    selectable=true ones can be passed to select_net_service (by `name`);
    others are listed so you can tell the user they exist but this server
    can't switch to them yet. A service still has to be signed in to play."""
    async with call(receiver) as c:
        try:
            layout = await c.session.layout()
        except _READ_ERRORS:
            layout = None
        label = c.receiver.label
    if layout is None or not layout.net_services:
        raise ReceiverError(
            f"{label} doesn't list its network services (older models don't describe themselves). "
            f"select_net_service still accepts: {', '.join(NET_SERVICE_CODES)}."
        )
    names = {code: name for name, code in NET_SERVICE_CODES.items()}
    return {
        "receiver": label,
        "services": [
            {"code": code, "receiver_name": shown, "name": names.get(code), "selectable": code in names}
            for code, shown in sorted(layout.net_services.items())
        ],
    }


def not_ready(service: str, screen: str) -> str:
    # A signed-out service opens a popup instead of its menu: "TIDAL Login",
    # "Amazon Music Sign In", "Try Deezer Premium+" (no account)
    return (
        f'{service} isn\'t ready: the receiver shows "{screen}". It needs '
        "signing in (or a subscription), which you can do in the Onkyo "
        "Controller app."
    )


@mcp.tool(title="Get now playing", annotations=READ_ONLY)
async def get_now_playing(receiver: ReceiverArg = None) -> dict[str, Any]:
    """What the receiver's network player is playing: the service, play state,
    title, artist, album and position, plus the menu its screen is showing
    (which can differ: browsing doesn't stop playback). Every zone whose input
    is "net" plays this; check get_status to see which zones are on "net"."""
    async with call(receiver) as c:
        # NMS (menu status) ends with the playing service's icon code, e.g.
        # "MxxxxS104" = Pandora. NLT is the menu on screen, e.g. "...NET" when
        # someone has gone back to the top menu while Pandora keeps playing.
        menu_status = await c.ask("NMSQSTN", "NMS")
        menu = await c.ask("NLTQSTN", "NLT")
        state = await c.ask("NSTQSTN", "NST")
        title = await c.ask("NTIQSTN", "NTI")
        artist = await c.ask("NATQSTN", "NAT")
        album = await c.ask("NALQSTN", "NAL")
        position = await c.ask("NTMQSTN", "NTM")
        station = await c.ask("NDNQSTN", "NDN")  # e.g. "Pearl Jam Radio"
        label = c.receiver.label
    return {
        "receiver": label,
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
# "Account Info"/"Sign Out" (-), which must never be selected. "NLSI00003"
# plays item 3 (counting from 1).
# ---------------------------------------------------------------------------

# A list item: (position from 1, icontype, title)
Item = tuple[int, str, str]
LIST_PAGE = 100  # items per NLA request


async def read_list(c: Call, nlt: str) -> list[Item]:
    """Every item of the menu the receiver is showing. `nlt` is its NLT title
    info: service (2), UI type, layer type, cursor (4 hex), item count (4 hex),
    layer number (2 hex), ..."""
    try:
        count, layer = min(int(nlt[8:12], 16), 0xFFF), nlt[12:14]
    except ValueError:
        raise ReceiverError(f"The receiver described its menu in a way this server can't read ({nlt!r}).") from None
    items: list[Item] = []
    # In pages: a TX-NR6050 takes 5.3s to send 700 albums in one reply (over
    # the timeout), but 0.3s per 100.
    for start in range(0, count, LIST_PAGE):
        # Expect "NLAX", not "NLA": the setter rule would take "NLAL..." as
        # the command echoed back, which never happens.
        reply = await c.ask(f"NLAL0001{layer}{start:04X}{min(LIST_PAGE, count - start):04X}", "NLAX")
        if reply[4:5] != "S":  # "0001S000<?xml..." = success
            raise ReceiverError("The receiver couldn't list this menu.")
        try:
            page = ElementTree.fromstring(reply[8:]).iter("item")
        except ElementTree.ParseError as exc:
            raise ReceiverError(f"The receiver sent a menu list that isn't valid XML ({exc}).") from None
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
        more = " ..." if len(items) > 30 else ""
        raise ReceiverError(f"No {what} matching {wanted!r}. Here: {available}{more}.")
    if len(names) > 1:
        raise ReceiverError(f"{wanted!r} matches several {what}s: {', '.join(names[:10])}. Which one?")
    return matches[0]


async def open_menu(c: Call, service: NetService, folder: tuple[str, ...]) -> tuple[str, list[Item]]:
    """Open a service's top menu, then each folder in `folder` in turn.
    Returns the NLT title info and items of the menu reached."""
    code = NET_SERVICE_CODES[service]
    # "NLT<code>01": a list (0) at the service's top layer (1). The playback
    # screen pushes "NLT<code>22..." while music plays, which isn't the menu.
    try:
        rest = await c.ask(
            f"NSV{code}0",
            f"NLT{code}01",
            no_reply=f"The receiver didn't open {service}. It may not offer {service}, "
            "or it isn't signed in: check in the Onkyo Controller app.",
        )
    except ReceiverError:
        # Maybe it opened a popup instead ("NLT1B31...TIDAL Login")
        shown = await c.ask("NLTQSTN", "NLT")
        if shown.startswith(code) and shown[2:3] in ("3", "4"):
            raise ReceiverError(not_ready(service, shown[22:])) from None
        raise
    nlt = f"{code}01{rest}"
    items = await read_list(c, nlt)
    for name in folder:
        position, _, title = pick(name, [i for i in items if i[1] == "F"], "folder")
        # The receiver announces the folder it opened with its title info,
        # but the previous menu's info keeps arriving too, with the same
        # prefix and screen type. So wait for the layer number one deeper.
        # Opening can take a few seconds (a music server answering), hence
        # the longer timeout.
        layer = f"{int(nlt[12:14], 16) + 1:02X}"

        def one_deeper(value: str, layer: str = layer) -> bool:
            return value[10:12] == layer  # value: what follows "NLT" + code

        rest = await c.ask(
            f"NLSI{position:05d}",
            f"NLT{code}",
            timeout=3 * c.settings.timeout,
            until=one_deeper,
            no_reply=f"The receiver didn't open the folder {title!r}.",
        )
        nlt = code + rest
        items = await read_list(c, nlt)
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


Interrupt = Annotated[
    bool,
    Field(
        description="Browse even though it stops what another service is playing. Ask the user first; "
        "without it, this refuses rather than stop the music."
    ),
]


async def playing_service(c: Call) -> str | None:
    """The network service that is playing right now (by code), or None if
    nothing plays. NMS ends with the service's icon code; NST starts with "P"
    while playing."""
    state = await c.ask("NSTQSTN", "NST")
    if not state.startswith("P"):
        return None
    status = await c.ask("NMSQSTN", "NMS")
    return status[-2:] if len(status) >= 2 else None


@mcp.tool(title="List stations", annotations=SETTER)
async def list_stations(
    service: NetService = "pandora",
    folder: FolderPath = (),
    receiver: ReceiverArg = None,
    interrupt: Interrupt = False,
) -> dict[str, Any]:
    """Browse a network service: list what can be played (stations, tracks)
    and the folders at one level of its menu. Starts at the top (for Pandora:
    your stations); to look inside a folder, call again with its name added to
    `folder` (e.g. TuneIn: ["My Presets"]). Pass a playable name, with the
    same `folder`, to play_station. Browsing the service that's playing doesn't
    interrupt it. Opening a *different* service would stop the music, so that
    is refused unless interrupt=true (ask the user first)."""
    async with call(receiver) as c:
        code = NET_SERVICE_CODES[service]
        playing = await playing_service(c)
        if playing is not None and playing != code and not interrupt:
            name = CODE_NET_SERVICES.get(playing, f"service {playing}")
            raise ReceiverError(
                f"{name} is playing. Browsing {service} would stop it. Ask the user, then call again with "
                "interrupt=true, or wait until nothing plays."
            )
        _, items = await open_menu(c, service, folder)
    # "M" = music, "0" = playing now, "F" = folder; anything else is a
    # message ("No Favorites available") or an account item ("Sign Out")
    playable = list(dict.fromkeys(t for _, kind, t in items if kind in ("M", "0")))
    folders = list(dict.fromkeys(t for _, kind, t in items if kind == "F"))
    result: dict[str, Any] = {
        "service": service,
        "folder": list(folder),
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
    station: str, service: NetService = "pandora", folder: FolderPath = (), receiver: ReceiverArg = None
) -> str:
    """Start playing a station or track from a network service, by name (e.g.
    "Pearl Jam Radio" on Pandora). Names come from list_stations; a
    distinctive part of a name is enough ("pearl jam"). For items inside
    folders (TuneIn presets, a music server's albums), pass the same `folder`
    list_stations used. It plays in every zone whose input is "net": set a
    zone's input to "net" first to hear it."""
    async with call(receiver) as c:
        _, items = await open_menu(c, service, folder)
        playable = [i for i in items if i[1] in ("M", "0")]  # never "Sign Out" and the like
        if not playable:
            folders = [t for _, kind, t in items if kind == "F"]
            hint = f" It has folders: {', '.join(folders[:30])}; add one to `folder` to look inside." if folders else ""
            raise ReceiverError(f"Nothing here can be played.{hint}")
        position, _, title = pick(station, playable, "station")
        # Confirmed when the player reports "playing" (NST "P..."), after ~3s
        await c.ask(
            f"NLSI{position:05d}",
            "NSTP",
            timeout=3 * c.settings.timeout,
            no_reply=f"{title} was selected but didn't start playing.",
        )
        note = shared_player_note(await zones_on_net(c))
    return with_note(f"Playing {title} on {service}", note)


PlaybackAction = Literal["play", "pause", "stop", "next", "previous"]
# The NTC command for each action, and the NST play state that confirms it
PLAYBACK_CODES: dict[str, tuple[str, str | None]] = {
    "play": ("PLAY", "P"),
    "pause": ("PAUSE", "p"),
    "stop": ("STOP", "S"),
    "next": ("TRUP", None),
    "previous": ("TRDN", None),
}


@mcp.tool(title="Control playback", annotations=PLAYBACK)
async def control_playback(action: PlaybackAction, receiver: ReceiverArg = None) -> str:
    """Play, pause, stop, or skip to the next/previous track on the network
    player (shared by every zone on "net"). "play" resumes what was paused;
    to start a station, use play_station. Services limit skipping: Pandora
    allows a few skips per hour and can't go back."""
    code, state = PLAYBACK_CODES[action]
    async with call(receiver) as c:
        if state:
            await c.ask(
                f"NTC{code}",
                f"NST{state}",
                no_reply=f"The player didn't {action}. Is something selected? Start a station with play_station.",
            )
            return {"play": "Playing", "pause": "Paused", "stop": "Stopped"}[action]
        # A skip has no state to wait for: watch for the title to change
        before = await c.ask("NTIQSTN", "NTI")
        await c.tell(f"NTC{code}")
        for _ in range(int(c.settings.timeout / 0.5)):
            await asyncio.sleep(0.5)
            title = await c.ask("NTIQSTN", "NTI")
            if title != before:
                return f"Now playing {title.strip()}"
    raise ReceiverError(
        "The track didn't change. The service may not allow that "
        "right now (Pandora limits skips per hour and can't go back)."
    )
