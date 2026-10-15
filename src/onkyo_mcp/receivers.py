"""Which receiver a tool call means, and one-at-a-time access to each.

**Several receivers: never guess.** Every tool takes an optional ``receiver``
(a configured name, or an address). Without one, a call is only acted on when
exactly one receiver is configured (or, with none configured, exactly one
answers discovery). Otherwise the answer is an error naming the receivers, so
the model can ask the user. Guessing would mean turning off the wrong room.
Read-only tools that can cover every receiver at once (get_status) do that
instead of asking.

**One command at a time per receiver.** Each Receiver has a lock, and a tool
call holds it for its whole exchange (``session()``), over one TCP
connection. Two MCP clients over HTTP, or a model calling tools in parallel,
then queue instead of opening parallel connections. How many simultaneous
eISCP connections a receiver accepts is unknown (ASSUMPTION O-CONNECTIONS),
and interleaving two exchanges would mix up their replies anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from xml.etree import ElementTree

from mcp.server.mcpserver.exceptions import ToolError

from . import eiscp
from .codes import ZONE_LABELS, Zone
from .config import ReceiverSettings, Settings, is_address, name_key

log = logging.getLogger("onkyo_mcp.receivers")


class ReceiverError(ToolError):
    """A problem the model (and user) can act on.

    Subclassing the SDK's ToolError matters: only ToolError messages reach the
    model. Any other exception is reported as just "Error executing tool
    <name>", which would hide advice like "turn it on first".
    """


class Unreachable(ReceiverError):
    """Couldn't connect, or connected but got no reply: a state of the
    receiver (off the network, rebooting), not a mistake in the call."""


@dataclass(frozen=True)
class ZoneInfo:
    present: bool
    volume: bool  # False: fixed-level output, or its amplifier drives other speakers
    volmax: int | None = None  # the receiver's own maximum, in raw steps, if it says


@dataclass(frozen=True)
class Layout:
    """What a receiver says about itself (NRIQSTN). Fetched once per receiver,
    since it only changes when someone changes the receiver's setup."""

    model: str
    zones: dict[Zone, ZoneInfo]
    # <netservicelist>: service code (hex, upper case) -> the receiver's name for it
    net_services: dict[str, str] = field(default_factory=dict)


def parse_nri(xml: str) -> Layout | None:
    """The receiver's self-description, or None if it has none ("N/A" on older models).

    Each zone appears as e.g. <zone id="2" value="1" name="Zone2" volmax="100"/>:
    value="1" means present, volmax="0" means no volume control (ASSUMPTION
    O-NRI-ZONES). Network services appear as <netservice id="04" value="1"
    name="Pandora"/>; value="1" means offered (ASSUMPTION O-NRI-SERVICES).
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    zones: dict[Zone, ZoneInfo] = {}
    for z in root.iter("zone"):
        name: Zone | None = {"1": "main", "2": "zone2", "3": "zone3"}.get(z.get("id", ""))  # type: ignore[assignment]
        if name is None:
            continue
        raw_max = z.get("volmax", "0")
        volmax = int(raw_max) if raw_max.isdigit() else None
        zones[name] = ZoneInfo(present=z.get("value") == "1", volume=volmax != 0, volmax=volmax or None)
    services = {
        s.get("id", "").upper(): s.get("name", "")
        for s in root.iter("netservice")
        if s.get("value") == "1" and s.get("id")
    }
    return Layout(model=root.findtext(".//model") or "receiver", zones=zones, net_services=services)


class Receiver:
    """One receiver: its settings, its lock, and what it said about itself."""

    def __init__(self, settings: ReceiverSettings, model: str | None = None) -> None:
        self.settings = settings
        self.model = model  # from discovery or NRI, once known
        self.lock = asyncio.Lock()
        self._layout: Layout | None = None
        self._layout_known = False

    @property
    def host(self) -> str:
        return self.settings.host

    @property
    def port(self) -> int:
        return self.settings.port

    @property
    def label(self) -> str:
        return self.settings.label

    def describe(self) -> str:
        """For messages: "Family Room (TX-NR6050 at 192.168.1.147)"."""
        address = self.host if self.port == eiscp.DEFAULT_PORT else f"{self.host}:{self.port}"
        bits = " at ".join(b for b in (self.model, address) if b)
        return f"{self.settings.name} ({bits})" if self.settings.name else bits

    def answers_to(self, ref: str) -> bool:
        ref = ref.strip()
        if ref in (self.host, f"{self.host}:{self.port}"):
            return True
        return bool(self.settings.name) and name_key(ref) == name_key(self.settings.name or "")

    @asynccontextmanager
    async def session(self, timeout: float) -> AsyncIterator[Session]:
        """Exclusive use of this receiver for one tool call, over one connection
        (opened on first use, so a call that only reads the cache opens none)."""
        async with self.lock:
            s = Session(self, timeout)
            try:
                yield s
            finally:
                await s.close()

    def layout_cached(self) -> tuple[bool, Layout | None]:
        return self._layout_known, self._layout

    def remember_layout(self, layout: Layout | None) -> None:
        self._layout, self._layout_known = layout, True
        if layout is not None and not self.model:
            self.model = layout.model


class Session:
    """A tool call's exchange with one receiver."""

    def __init__(self, receiver: Receiver, timeout: float) -> None:
        self.receiver = receiver
        self.timeout = timeout
        self._conn: eiscp.Connection | None = None

    async def _connection(self) -> eiscp.Connection:
        if self._conn is None:
            self._conn = await eiscp.Connection.open(self.receiver.host, self.receiver.port, self.timeout)
        return self._conn

    async def request(
        self, command: str, expect: str, timeout: float | None = None, until: Callable[[str], bool] | None = None
    ) -> str:
        """Connection.request on this session's connection. After a timeout or
        a garbled reply the connection is dropped (a late reply could
        otherwise be read as the answer to the next command) and the next
        request opens a fresh one."""
        for attempt in (1, 2):
            conn = await self._connection()
            try:
                return await conn.request(command, expect, timeout or self.timeout, until)
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                # The receiver dropped the connection. A query is safe to ask
                # again on a fresh one; a setter isn't retried here (it may
                # have been applied before the drop).
                await self.close()
                if attempt == 2 or not command.endswith("QSTN"):
                    raise
                log.info("%s dropped the connection during %s; asking again", self.receiver.host, command)
            except (TimeoutError, ValueError, OSError):
                await self.close()
                raise
        raise AssertionError("unreachable")

    async def write(self, command: str) -> None:
        conn = await self._connection()
        try:
            await conn.write(command)
        except OSError:
            await self.close()
            raise

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()

    async def layout(self) -> Layout | None:
        """The receiver's self-description, fetched on first use and cached."""
        known, layout = self.receiver.layout_cached()
        if not known:
            reply = await self.request("NRIQSTN", "NRI", timeout=3 * self.timeout)
            layout = parse_nri(reply)
            self.receiver.remember_layout(layout)
        return layout


class Registry:
    """Every receiver this server knows: configured ones, then any the model
    names by address, then (with none configured) the one discovery finds."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.receivers = [Receiver(r) for r in settings.receivers]
        self._adhoc: dict[str, Receiver] = {}
        self._discovered: Receiver | None = None

    def names(self) -> str:
        return ", ".join(r.describe() for r in self.receivers) or "(none)"

    def _by_address(self, ref: str) -> Receiver:
        """A receiver the model named by address but isn't configured, e.g. one
        it found with discover_receivers. One object per address, so it gets
        one lock however often it's named."""
        host, _, port = ref.partition(":") if ref.count(":") == 1 else (ref, "", "")
        key = f"{host}:{port or self.settings.discovery_port}"
        if key not in self._adhoc:
            port_number = int(port) if port.isdigit() else self.settings.discovery_port
            self._adhoc[key] = Receiver(ReceiverSettings(host, None, port_number, None))
        return self._adhoc[key]

    async def pick(self, ref: str | None) -> Receiver:
        """The one receiver a call is about, or a ReceiverError saying why not."""
        if ref is not None and ref.strip():
            matches = [r for r in self.receivers if r.answers_to(ref)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:  # two receivers with the same name (config warned about it)
                raise ReceiverError(
                    f"{ref!r} matches several receivers: {', '.join(r.describe() for r in matches)}. "
                    "Pass one by address."
                )
            # Not configured: an address is fine (e.g. one discover_receivers
            # found), but only an IP or a dotted hostname. A bare word is
            # far more likely a mistyped name than a host, and must not turn
            # into a DNS lookup.
            address = ref.strip()
            host = address.split(":")[0] if address.count(":") == 1 else address
            if is_address(host) and ("." in host or ":" in host):
                return self._by_address(address)
            raise ReceiverError(f"No receiver named {ref!r}. Receivers: {self.names()}")
        if len(self.receivers) == 1:
            return self.receivers[0]
        if len(self.receivers) > 1:
            raise ReceiverError(
                f"Several receivers are configured: {self.names()}. Pass the one you mean as `receiver` "
                "(its name or address); call get_status without one to see them all."
            )
        return await self._discover_one()

    def all(self) -> list[Receiver]:
        """Every configured receiver (for read-only tools that cover them all)."""
        return list(self.receivers)

    async def _discover_one(self) -> Receiver:
        """With nothing configured: the only receiver that answers discovery,
        remembered for later calls."""
        if self._discovered is None:
            found = await eiscp.discover(self.settings.discovery_addr, self.settings.discovery_port, timeout=2.0)
            if len(found) > 1:
                listing = ", ".join(f"{r.model} at {r.host}" for r in found)
                raise ReceiverError(f"Several receivers found ({listing}): pass the one you want as receiver.")
            if not found:
                raise ReceiverError(
                    "No receiver is configured and none answered discovery. Set ONKYO_HOST to the receiver's IP "
                    "address, or list receivers in config.json (see the README: When discovery finds nothing)."
                )
            r = found[0]
            self._discovered = Receiver(ReceiverSettings(r.host, None, r.port, None), model=r.model)
            log.info("Using %s at %s (found by discovery)", r.model, r.host)
        return self._discovered


def zone_label(zone: Zone) -> str:
    return ZONE_LABELS[zone]
