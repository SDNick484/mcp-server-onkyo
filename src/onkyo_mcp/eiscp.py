"""eISCP: the Integra Serial Control Protocol over Ethernet. Plain protocol code, no MCP.

Packet layout (TCP 60128, and the same framing for UDP discovery):

    "ISCP" | header size (u32 BE, always 16) | data size (u32 BE)
    | version (u8, 0x01) | 3 reserved bytes | data

Data is "!1" + a 3-letter command + its parameter + a terminator:

    "!1PWR01\\r"    power on
    "!1MVLQSTN\\r"  query master volume
    "!1MVL28\\x1a\\r\\n"  a reply (volume is hex: 0x28 = 40 raw steps)

Receivers also *push* status messages nobody asked for (another zone's
volume, the network player's progress), so a reply is found by its 3-letter
prefix, skipping everything else. See Connection.request.

ASSUMPTION O-FRAMING (the layout above), O-PUSHES (the pushes).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

log = logging.getLogger("onkyo_mcp.eiscp")

DEFAULT_PORT = 60128
HEADER = struct.Struct(">4sIIB3x")  # magic, header size, data size, version, 3 pad bytes = 16 bytes


def build_packet(command: str, unit: str = "1") -> bytes:
    """One command, framed. unit "1" = receiver; "x" = any device type (discovery)."""
    data = f"!{unit}{command}\r".encode()
    # ">" big-endian, "I" u32 header size, "I" u32 data size, "B" u8 version,
    # "3x" three zero padding bytes: 4 + 4 + 4 + 1 + 3 = 16.
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


def _strip(data: bytes) -> str:
    # Drop "!1" and the \x1a / \r / \n (and, in some UDP replies, \x19)
    # terminators. Text (track titles, station names) is UTF-8: "Beyoncé"
    # must not become "Beyonc��".
    return data.decode("utf-8", "replace")[2:].rstrip("\x19\x1a\r\n")


def decode_datagram(packet: bytes) -> str:
    """Decode one whole eISCP packet (as received over UDP)."""
    magic, header_size, data_size, _version = HEADER.unpack(packet[:16])
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    return _strip(packet[header_size : header_size + data_size])


async def read_packet(reader: asyncio.StreamReader) -> str:
    """Read one eISCP packet from a TCP stream. TCP is a byte stream, not a
    sequence of messages, so read the fixed 16-byte header first to learn how
    many data bytes follow."""
    magic, header_size, data_size, _version = HEADER.unpack(await reader.readexactly(16))
    if magic != b"ISCP":
        raise ValueError(f"Bad magic: {magic!r}")
    if header_size < 16 or data_size > 1 << 20:
        # A real receiver never sends these; refusing them stops a garbled
        # stream from making us wait for (or allocate) a megabyte of nothing.
        raise ValueError(f"Implausible header: header {header_size}, data {data_size}")
    await reader.readexactly(header_size - 16)  # normally 0 bytes
    return _strip(await reader.readexactly(data_size))


class Connection:
    """One TCP connection to a receiver, used for one or more commands.

    Each tool call opens one, sends its commands one after another (waiting
    for each reply), and closes it (ASSUMPTION O-MULTI-COMMAND). Not kept open between calls: simpler, and
    it survives the receiver dropping idle connections.
    """

    def __init__(self, host: str, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.host, self.port = host, port
        self._reader, self._writer = reader, writer

    @classmethod
    async def open(cls, host: str, port: int, timeout: float) -> Connection:
        """Connect, or raise ConnectionError (an OSError) saying why."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except TimeoutError:
            # Re-raised as a different type, so callers can tell "couldn't
            # connect" apart from "connected, but no reply" (TimeoutError).
            raise ConnectionError(f"timed out connecting to {host}:{port}") from None
        return cls(host, port, reader, writer)

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(OSError):  # the receiver already hung up: nothing left to close
            await self._writer.wait_closed()

    async def write(self, command: str) -> None:
        """Send a command without waiting for anything."""
        log.debug("eISCP -> %s %s", self.host, command)
        self._writer.write(build_packet(command))
        await self._writer.drain()

    async def request(
        self, command: str, expect: str, timeout: float, until: Callable[[str], bool] | None = None
    ) -> str:
        """Send `command` and return the value of the reply that starts with
        `expect` (e.g. "MVL"): "MVL50" -> "50". Skips unsolicited messages.

        For a setter ("AMT01", as opposed to a query, "AMTQSTN"), the reply
        must echo the value sent, or be "N/A". A same-prefix message with
        another value is a status push, not our answer: right after power-on a
        TX-NR7100 pushes "AMT00" while still ignoring commands, which would
        otherwise read as "AMT01 failed".

        `until`, if given, must also accept the reply's value: for replies
        that can't be told apart by prefix alone (which menu layer an NLT is for).

        Raises TimeoutError if no matching reply arrives within `timeout`
        (e.g. a receiver in standby ignoring a setter), and ValueError or
        asyncio.IncompleteReadError if the stream is garbled or closed.
        """
        await self.write(command)
        sent_value = command[len(expect) :]  # "AMT01" -> "01", "AMTQSTN" -> "QSTN"
        is_setter = command.startswith(expect) and sent_value != "QSTN"

        async def wait_for_match() -> str:
            while True:
                msg = await read_packet(self._reader)
                value = msg[len(expect) :]  # "MVL50" -> "50"
                if (
                    msg.startswith(expect)
                    and (not is_setter or value in (sent_value, "N/A"))
                    and (until is None or until(value))
                ):
                    log.debug("eISCP <- %s %s", self.host, msg)
                    return value
                log.debug("eISCP <- %s %s (unsolicited, skipped)", self.host, msg)

        # One timeout around the whole loop, so a chatty receiver that never
        # sends the reply we want can't keep us waiting forever.
        return await asyncio.wait_for(wait_for_match(), timeout)


async def send(
    host: str,
    port: int,
    command: str,
    expect: str | None = None,
    timeout: float = 5.0,
    until: Callable[[str], bool] | None = None,
) -> str | None:
    """One command on its own connection: request() if `expect` is given,
    else write() and return None. Raises ConnectionError or TimeoutError."""
    async with await Connection.open(host, port, timeout) as conn:
        if expect is None:
            await conn.write(command)
            return None
        return await conn.request(command, expect, timeout, until)


# --- discovery ---------------------------------------------------------------------
@dataclass(frozen=True)
class Found:
    """A receiver that answered discovery."""

    host: str
    model: str
    port: int
    region: str
    mac: str


def parse_ecn(msg: str, host: str) -> Found | None:
    """ "ECNTX-NR7100/60128/DX/0009B0123456" -> Found, or None if it isn't an ECN reply."""
    if not msg.startswith("ECN"):
        return None
    # Pad with blanks so a reply with missing fields still unpacks
    model, port, region, mac = (msg[3:].split("/") + ["", "", "", ""])[:4]
    # "0009B0123456" -> "00:09:B0:12:34:56"
    if len(mac) >= 12:
        mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))
    try:
        port_number = int(port or DEFAULT_PORT)
    except ValueError:
        port_number = DEFAULT_PORT
    return Found(host, model, port_number, region, mac)


async def discover(address: str, port: int = DEFAULT_PORT, timeout: float = 3.0) -> list[Found]:
    """Send "!xECNQSTN" to `address` (normally the broadcast address) on UDP
    (ASSUMPTION O-DISCOVERY)
    `port`. Each receiver replies with "!1ECN<model>/<port>/<region>/<mac>",
    and the reply's source address is its IP. Collects replies for `timeout`."""
    found: dict[str, Found] = {}  # by IP, so a receiver that answers twice is listed once

    # asyncio calls datagram_received for every UDP packet that arrives on
    # our socket, while discover() is sleeping below.
    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
            host = str(addr[0])
            try:
                msg = decode_datagram(data)
            except (ValueError, struct.error):
                log.debug("eISCP <- %s (UDP) not eISCP, ignored: %r", host, data[:32])
                return  # some other device on the port
            log.debug("eISCP <- %s (UDP) %s", host, msg)
            if (receiver := parse_ecn(msg, host)) is not None:
                found[host] = receiver

    loop = asyncio.get_running_loop()
    # Port 0 = let the OS pick a free local port; replies come back to it.
    transport, _ = await loop.create_datagram_endpoint(Listener, local_addr=("0.0.0.0", 0), allow_broadcast=True)
    try:
        log.debug("eISCP -> %s (UDP broadcast) ECNQSTN", address)
        try:
            transport.sendto(build_packet("ECNQSTN", unit="x"), (address, port))
        except OSError as exc:  # no route for broadcasts here (no network, some containers)
            log.warning("Discovery broadcast to %s failed: %s", address, exc)
            return []
        await asyncio.sleep(timeout)  # collect every reply that arrives in the window
    finally:
        transport.close()
    return sorted(found.values(), key=lambda r: (r.model, r.host))
