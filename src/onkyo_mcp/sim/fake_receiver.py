"""A simulated Onkyo receiver, for developing and testing without hardware.

    python -m onkyo_mcp.sim.fake_receiver             # one receiver on 127.0.0.1:60128
    ONKYO_HOST=127.0.0.1 mcp-server-onkyo
    mcp-server-onkyo simulate                          # two, plus a config.json for them

It speaks enough eISCP to exercise the server:

- state for the main zone (PWR/MVL/AMT/SLI/LMD) and Zone 2 (ZPW/ZVL/ZMT/SLZ);
  no Zone 3, like a TX-NR6050;
- a network player (NST/NTI/NAT/NAL/NTM/NLT/NDN/NMS), NSV to pick a service,
  NLA/NLSI to list and play Pandora stations or browse TuneIn and a music
  server's folders, NTC for playback;
- its own description (NRIQSTN) and discovery (UDP ECNQSTN);
- an unsolicited status message before every reply, the way real receivers
  push status, so the server's reply matching is exercised;
- in standby, a zone answers queries, and its setters get one of the two
  behaviors seen on hardware (``FakeReceiver.standby``): "silent", no reply at
  all, like a TX-NR7100; or "na", input changes accepted and other setters
  answered N/A, like a TX-NR6050. ``make()`` gives each model its own.

Everything here is modelled on traffic seen from a TX-NR6050 and TX-NR7100,
or on the onkyo-eiscp command tables where noted. It is a *simulator*:
passing tests against it shows the server handles these exchanges, not that
a receiver behaves this way. Anything not confirmed on hardware is an
assumption in assumptions.py.

**Failure injection** (``Faults``) makes it misbehave on purpose: never answer
some commands, answer slowly, drop the connection after N commands, send a
garbled packet, refuse connections beyond a limit, or push extra unsolicited
messages. Several ``FakeReceiver`` instances run side by side, each with its
own state, port and identity.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger("onkyo_mcp.sim")


def packet(msg: str) -> bytes:
    """A reply as a receiver frames it: "!1" + message + EOF, CR, LF."""
    data = f"!1{msg}\x1a\r\n".encode()
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


# --- what the receiver offers ------------------------------------------------------
# Services NSV can switch to (the rest get no reply, like a service the
# receiver doesn't offer), and the menu title each one shows
NET_SERVICES = {"04": "Pandora", "1C": "Amazon Music", "0E": "TuneIn Radio", "00": "Music Server"}
# A signed-out service opens a popup (UI type 3) instead of its menu
SIGNED_OUT = {"1B": "TIDAL Login"}
# Pandora's top menu, as (icontype, title): M = music, the rest must never be
# played. "Pearl Jam Radio" appears twice, as it does on a real account.
STATIONS: list[tuple[Any, ...]] = [
    ("G", "Create new station"),
    ("M", "Shuffle"),
    ("M", "Pearl Jam Radio"),
    ("M", "Beyoncé Radio"),
    ("M", "Pearl Jam Radio"),
    ("-", "Sign Out"),
]
# Other services' top menus. TuneIn's are folders (F), each with its own
# items: (icontype, title, contents) for a folder
MENUS: dict[str, list[tuple[Any, ...]]] = {
    "04": STATIONS,
    "1C": [],
    # More albums than one NLA page (100) holds, to exercise paging
    "00": [("F", "Album", [("F", f"Album {n}", [("M", f"Track {n}")]) for n in range(1, 251)])],
    "0E": [
        ("F", "My Presets", [("M", "KQED Public Radio"), ("M", "KCSM Jazz")]),
        ("F", "Local Radio", [("-", "No stations available")]),
    ],
}
TRACKS = ["Black", "Interstate Love Song", "Garden"]  # what "next" steps through
# Which power command each setting belongs to
INPUT_CODES = ("SLI", "SLZ", "SL3")
ZONE_POWER = {"MVL": "PWR", "AMT": "PWR", "SLI": "PWR", "LMD": "PWR", "ZVL": "ZPW", "ZMT": "ZPW", "SLZ": "ZPW"}


def nri_xml(model: str, zone2: bool = True, zone2_volume: bool = True, zone3: bool = False) -> str:
    """The self-description NRIQSTN returns, trimmed to what the server reads.

    The <zonelist> shape is copied from a TX-NR6050. The <netservicelist> entry
    shape (id, value, name) is ASSUMPTION O-NRI-SERVICES: the codes match what
    the receivers list, the attribute layout is from other projects' parsers.
    """
    z2max = "100" if zone2 and zone2_volume else "0"
    services = "".join(
        f'<netservice id="{code.lower()}" value="1" name="{name}"/>'
        for code, name in {**NET_SERVICES, "0A": "Spotify", "12": "Deezer", "1B": "TIDAL", "44": "AirPlay"}.items()
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><response status="ok"><device id="{model}">'
        f"<model>{model}</model>"
        f'<netservicelist count="{services.count("<netservice")}">{services}</netservicelist>'
        '<zonelist count="4">'
        '<zone id="1" value="1" name="Main" volmax="100"/>'
        f'<zone id="2" value="{int(zone2)}" name="Zone2" volmax="{z2max}"/>'
        f'<zone id="3" value="{int(zone3)}" name="Zone3" volmax="{"100" if zone3 else "0"}"/>'
        '<zone id="4" value="0" name="Zone4" volmax="0"/>'
        "</zonelist></device></response>"
    )


def default_state(model: str = "TX-NR6050") -> dict[str, Any]:
    # Main zone on; MVL 0x50 = 80 raw -> displays 40.0 on a 0.5-step model.
    # Zone 2 in standby, its input following the main zone (SLZ 80).
    return {
        "PWR": "01",
        "MVL": "50",
        "AMT": "00",
        "SLI": "10",
        "LMD": "00",
        "ZPW": "00",
        "ZVL": "50",
        "ZMT": "00",
        "SLZ": "80",
        # Network player, showing its top menu ("NET"), nothing playing
        "NLT": "F3000000000E0000FFFF00NET",
        "NMS": "xxxxxxxF3",
        "NST": "Sxx1",
        "NTI": "",
        "NAT": "",
        "NAL": "",
        "NTM": "--:--:--/--:--:--",
        "NDN": "",
        "menu_path": (),  # folders opened (positions), not an eISCP code
        "NRI": nri_xml(model),
    }


@dataclass
class Faults:
    """Ways to make the fake misbehave. All off by default."""

    silent: set[str] = field(default_factory=set)  # 3-letter codes it never answers
    delay: float = 0.0  # seconds before every reply
    drop_after: int | None = None  # close each connection after this many commands
    garble: set[str] = field(default_factory=set)  # codes answered with a corrupt packet
    max_connections: int | None = None  # refuse (close at once) connections beyond this
    extra_pushes: int = 0  # unsolicited messages before each reply, on top of the usual one
    refuse_all: bool = False  # close every connection at once (receiver rebooting)


class FakeReceiver:
    """One simulated receiver: TCP control and UDP discovery on one port."""

    def __init__(
        self,
        model: str = "TX-NR6050",
        mac: str = "0009B0F76CFD",
        region: str = "DX",
        host: str = "127.0.0.1",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.model, self.mac, self.region, self.host = model, mac, region, host
        self.state = default_state(model) if state is None else state
        self.faults = Faults()
        # How a zone in standby answers setters (ASSUMPTION O-STANDBY-SILENT):
        # "silent" (TX-NR7100) or "na" (TX-NR6050: inputs change, the rest is N/A)
        self.standby: Literal["silent", "na"] = "silent"
        self.selected: list[int] = []  # NLSI positions played, for tests to check
        self.received: list[str] = []  # every command, in order
        self.connections = 0  # open right now
        self.peak_connections = 0  # most open at once
        self.port = 0
        self._server: asyncio.Server | None = None
        self._udp: asyncio.BaseTransport | None = None

    # --- lifecycle -------------------------------------------------------------------
    async def start(self, port: int = 0) -> FakeReceiver:
        """Listen on `port` (0: a free one); TCP and UDP use the same number, like a real receiver."""
        self._server = await asyncio.start_server(self._handle, self.host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(lambda: _Discovery(self), local_addr=(self.host, self.port))
        log.info("Fake %s on %s:%s", self.model, self.host, self.port)
        return self

    async def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def __aenter__(self) -> FakeReceiver:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --- protocol --------------------------------------------------------------------
    def current_menu(self) -> list[tuple[Any, ...]]:
        """The items of the menu on screen: the service's top menu, then down
        through each folder opened since (positions in state["menu_path"])."""
        items = MENUS[self.state["NLT"][:2]]
        for position in self.state["menu_path"]:
            items = items[position - 1][2]
        return items

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.faults.refuse_all or (
            self.faults.max_connections is not None and self.connections >= self.faults.max_connections
        ):
            log.info("refusing a connection (%s open)", self.connections)
            writer.close()
            return
        self.connections += 1
        self.peak_connections = max(self.peak_connections, self.connections)
        commands = 0
        try:
            while True:
                header = await reader.readexactly(16)
                _, header_size, data_size, _ = struct.unpack(">4sIIB3x", header)
                await reader.readexactly(header_size - 16)
                cmd = (await reader.readexactly(data_size)).decode("ascii")[2:].strip()
                self.received.append(cmd)
                commands += 1
                log.debug("<- %s", cmd)
                if self.faults.delay:
                    await asyncio.sleep(self.faults.delay)
                writer.write(self.reply(cmd))
                await writer.drain()
                if self.faults.drop_after is not None and commands >= self.faults.drop_after:
                    log.info("dropping the connection after %s commands", commands)
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            # Client closed the connection. ConnectionError covers a reset or
            # broken pipe while we were still answering: a client that writes one
            # command and hangs up at once, on a fast local link.
            pass
        finally:
            self.connections -= 1
            writer.close()

    def reply(self, cmd: str) -> bytes:
        """Everything the receiver sends back for one command (possibly nothing)."""
        state, code, param = self.state, cmd[:3], cmd[3:]
        out = packet("NLSU0-Now Playing")  # unsolicited noise, as real receivers push status
        for n in range(self.faults.extra_pushes):
            out += packet(f"NTM00:00:{n:02d}/00:04:00")
        if code in self.faults.silent:
            return out
        if code in self.faults.garble:
            return out + b"ISCP" + struct.pack(">IIB3x", 16, 0xFFFFFF, 1) + b"!1" + code.encode()
        if code == "NSV":
            # No reply of its own: the receiver pushes its new menu title
            service = param[:2]
            if service in SIGNED_OUT:
                state["NLT"] = f"{service}31000000090100FF{service}00{SIGNED_OUT[service]}"
                out += packet("NLT" + state["NLT"])
            if service in NET_SERVICES:
                count = len(MENUS[service])
                state["NLT"] = f"{service}010000{count:04X}0100FF0400{NET_SERVICES[service]}"
                state["NMS"] = f"MxxxxS1{service}"  # ends with the service icon
                state["menu_path"] = ()  # back at the top menu
                out += packet("NLT" + state["NLT"])
            return out
        if code == "NLA" and param.startswith("L"):
            # "L" + sequence (4) + layer (2) + first item (4 hex, from 0) + count (4 hex).
            # The whole page as XML: "X" + sequence number + "S" (success).
            # The station playing now is marked icontype "0", not "M".
            start, count = int(param[7:11], 16), int(param[11:15], 16)
            page = self.current_menu()[start : start + count]
            items = "".join(
                f'<item icontype="{"0" if i in self.selected[-1:] else t}" title="{title}" selectable="1" />'
                for i, (t, title, *_) in enumerate(page, start=start + 1)
            )
            xml = (
                f'<?xml version="1.0" encoding="utf-8"?><response status="ok">'
                f'<items offset="0" totalitems="{items.count("<item")}" >{items}</items></response>'
            )
            return out + packet(f"NLAX{param[1:5]}S000{xml}")
        if code == "NLS" and param.startswith("I"):
            position = int(param[1:])
            kind, title, *contents = self.current_menu()[position - 1]
            if kind == "F":
                # Open the folder: one layer deeper, no reply but its title info
                service = state["NLT"][:2]
                state["menu_path"] += (position,)
                layer = len(state["menu_path"]) + 1
                state["NLT"] = f"{service}020000{len(contents[0]):04X}{layer:02X}00FF{service}00{title}"
                return out + packet("NLT" + state["NLT"])
            self.selected.append(position)
            state.update(NDN=title, NTI=TRACKS[0], NST="Pxx1")
            return out + packet("NSTSxx1") + packet("NST" + state["NST"])
        if code == "NTC":
            if param == "TRUP":
                state["NTI"] = TRACKS[(TRACKS.index(state["NTI"]) + 1) % len(TRACKS)] if state["NTI"] else TRACKS[0]
                return out + packet("NTI" + state["NTI"])
            if param in ("PLAY", "PAUSE", "STOP") and state["NDN"]:
                state["NST"] = {"PLAY": "P", "PAUSE": "p", "STOP": "S"}[param] + "xx1"
                return out + packet("NST" + state["NST"])
            return out  # nothing selected, or TRDN (Pandora can't go back): no reply
        if code not in state:
            return out + packet(f"{code}N/A")
        if code in ZONE_POWER and state[ZONE_POWER[code]] == "00" and param != "QSTN":
            if self.standby == "silent":
                log.debug("   (in standby: ignored, no reply)")
                return out
            if code not in INPUT_CODES:
                log.debug("   (in standby: N/A)")
                return out + packet(f"{code}N/A")
        if param != "QSTN":
            state[code] = param
        log.debug("-> %s%s", code, state[code])
        return out + packet(code + state[code])


class _Discovery(asyncio.DatagramProtocol):
    """Answers the UDP "!xECNQSTN" discovery query with this receiver's identity."""

    def __init__(self, fake: FakeReceiver) -> None:
        self.fake = fake
        self.transport: Any = None  # asyncio's datagram transport (not a DatagramTransport subclass)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if b"ECNQSTN" in data and self.transport is not None and not self.fake.faults.refuse_all:
            f = self.fake
            log.debug("<- discovery from %s", addr[0])
            # The port field is the TCP control port, which for the fake is
            # wherever it was started.
            self.transport.sendto(packet(f"ECN{f.model}/{f.port}/{f.region}/{f.mac}"), addr)


def free_port() -> int:
    """A port with nothing listening on it (for testing connection failures)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


# Two different receivers, for `simulate` and multi-receiver tests: the
# owner's pair. The owner's TX-NR7100 uses its Zone 2 amplifier for height
# speakers, and on hardware its Zone 2 volume answered "N/A". The fake models
# that as volmax="0" in its NRI; what the real NRI says is unconfirmed
# (ASSUMPTION O-NRI-ZONES).
PROFILES: dict[str, dict[str, Any]] = {
    "TX-NR6050": {"mac": "0009B0F76CFD"},
    "TX-NR7100": {"mac": "0009B0623D93"},
}


def make(model: str, host: str = "127.0.0.1") -> FakeReceiver:
    fake = FakeReceiver(model=model, mac=PROFILES.get(model, {}).get("mac", "0009B0000000"), host=host)
    # Seen on the owner's receivers: the TX-NR6050's zones in standby accept an
    # input change and answer N/A to volume and mute; the TX-NR7100 says nothing.
    fake.standby = "na" if model == "TX-NR6050" else "silent"
    if model == "TX-NR7100":
        fake.state["NRI"] = nri_xml(model, zone2=True, zone2_volume=False)
    return fake


async def run(models: list[str], port: int, host: str = "127.0.0.1") -> None:
    """Run fakes until interrupted: the first on `port`, the rest on free ports."""
    fakes = [make(m, host) for m in models]
    for i, fake in enumerate(fakes):
        await fake.start(port if i == 0 else 0)
        print(f"Fake {fake.model} on {fake.host}:{fake.port} (TCP control + UDP discovery)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for fake in fakes:
            await fake.stop()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("--port", type=int, default=60128)
    p.add_argument("--model", action="append", help="TX-NR6050 (default) or TX-NR7100; repeat for several")
    p.add_argument("-v", "--verbose", action="store_true", help="log every command")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.model or ["TX-NR6050"], args.port))


if __name__ == "__main__":
    main()


class ReplayReceiver:
    """A strict, scripted receiver for contract tests: it expects exactly the
    commands in `exchanges`, in order, and answers each with its recorded
    replies. Anything else is recorded in `mismatches` and the connection is
    closed, so a test sees precisely where the server's traffic diverged from
    the transcript (hand-built, or captured with `doctor --dump`)."""

    def __init__(self, exchanges: list[dict[str, Any]], host: str = "127.0.0.1") -> None:
        self.exchanges = list(exchanges)
        self.host = host
        self.port = 0
        self.position = 0
        self.mismatches: list[str] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> ReplayReceiver:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def finished(self) -> bool:
        return self.position == len(self.exchanges)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(16)
                _, header_size, data_size, _ = struct.unpack(">4sIIB3x", header)
                await reader.readexactly(header_size - 16)
                cmd = (await reader.readexactly(data_size)).decode("utf-8")[2:].strip()
                if self.position >= len(self.exchanges):
                    self.mismatches.append(f"unexpected extra command {cmd!r}")
                    break
                expected = self.exchanges[self.position]
                if cmd != expected["send"]:
                    self.mismatches.append(f"exchange {self.position}: expected {expected['send']!r}, got {cmd!r}")
                    break
                self.position += 1
                for reply in expected["replies"]:
                    writer.write(packet(reply))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
