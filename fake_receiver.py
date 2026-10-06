"""
Simulated Onkyo receiver for developing without hardware.

    python fake_receiver.py            # listens on 127.0.0.1:60128
    ONKYO_HOST=127.0.0.1 python onkyo_mcp.py

It speaks enough eISCP to exercise the server: it remembers PWR/MVL/AMT/SLI/LMD
state for the main zone and ZPW/ZVL/ZMT/SLZ for zone 2 (it has no zone 3, like
a TX-NR6050) and a network player (NST/NTI/NAT/NAL/NTM/NLT, and NSV to pick
Pandora or Amazon Music), answers QSTN queries, and sends an unsolicited status message before
every reply, the way real receivers do, so the server's filtering gets tested.
A zone in standby answers queries but ignores every other command for that
zone except power, with no reply at all, like a real TX-NR7100.

Tests import it and call `start(port=0)` to get a receiver on a free port.
"""

import asyncio
import socket
import struct
import sys

# Main zone on; MVL 0x50 = 80 raw -> displays 40.0 on a 0.5-step model.
# Zone 2 in standby, its input following the main zone (SLZ 80).
DEFAULT_STATE = {"PWR": "01", "MVL": "50", "AMT": "00", "SLI": "10", "LMD": "00",
                 "ZPW": "00", "ZVL": "50", "ZMT": "00", "SLZ": "80",
                 # Network player, showing its top menu ("NET"), nothing playing
                 "NLT": "F3000000000E0000FFFF00NET", "NMS": "xxxxxxxF3", "NST": "Sxx1", "NTI": "", "NAT": "",
                 "NAL": "", "NTM": "--:--:--/--:--:--",
                 # Its self-description (trimmed): a TX-NR6050, Zone 2 but no Zone 3
                 "NRI": '<?xml version="1.0" encoding="utf-8"?><response status="ok"><device id="TX-NR6050">'
                        '<model>TX-NR6050</model><zonelist count="4">'
                        '<zone id="1" value="1" name="Main" volmax="100"/>'
                        '<zone id="2" value="1" name="Zone2" volmax="100"/>'
                        '<zone id="3" value="0" name="Zone3" volmax="0"/>'
                        '<zone id="4" value="0" name="Zone4" volmax="0"/>'
                        '</zonelist></device></response>'}
# Services NSV can switch to (the rest get no reply, like a service the
# receiver doesn't offer), and the menu title each one shows
NET_SERVICES = {"04": "Pandora", "1C": "Amazon Music"}
# Which power command each setting belongs to
ZONE_POWER = {"MVL": "PWR", "AMT": "PWR", "SLI": "PWR", "LMD": "PWR",
              "ZVL": "ZPW", "ZMT": "ZPW", "SLZ": "ZPW"}
state = dict(DEFAULT_STATE)


def reset_state() -> None:
    state.clear()
    state.update(DEFAULT_STATE)


def packet(msg: str) -> bytes:
    data = f"!1{msg}\x1a\r\n".encode("utf-8")
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 state: dict = state) -> None:
    try:
        while True:
            header = await reader.readexactly(16)
            _, header_size, data_size, _ = struct.unpack(">4sIIB3x", header)
            await reader.readexactly(header_size - 16)
            cmd = (await reader.readexactly(data_size)).decode("ascii")[2:].strip()
            code, param = cmd[:3], cmd[3:]
            print(f"<- {cmd}", file=sys.stderr)

            writer.write(packet("NLSU0-Now Playing"))  # unsolicited noise
            if code == "NSV":
                # No reply of its own: the receiver pushes its new menu title
                if param[:2] in NET_SERVICES:
                    state["NLT"] = f"{param[:2]}01000000480100FF0400{NET_SERVICES[param[:2]]}"
                    state["NMS"] = f"MxxxxS1{param[:2]}"  # ends with the service icon
                    writer.write(packet("NLT" + state["NLT"]))
            elif code not in state:
                writer.write(packet(f"{code}N/A"))
            elif code in ZONE_POWER and state[ZONE_POWER[code]] == "00" and param != "QSTN":
                print("   (in standby: ignored, no reply)", file=sys.stderr)
            else:
                if param != "QSTN":
                    state[code] = param
                writer.write(packet(code + state[code]))
                print(f"-> {code}{state[code]}", file=sys.stderr)
            await writer.drain()
    except asyncio.IncompleteReadError:
        pass  # client closed the connection
    finally:
        writer.close()


class Discovery(asyncio.DatagramProtocol):
    """Answers the UDP "!xECNQSTN" discovery broadcast like a TX-NR7100."""

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if b"ECNQSTN" in data:
            print(f"<- discovery from {addr[0]}", file=sys.stderr)
            self.transport.sendto(packet("ECNTX-NR7100/60128/DX/0009B0123456"), addr)


def free_port() -> int:
    """A port with nothing listening on it (for testing connection failures)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start(host: str = "127.0.0.1", port: int = 60128, state: dict = state):
    """Start TCP control and UDP discovery on the same port number, like a real
    receiver. Pass port=0 to pick a free one, and your own `state` dict to run
    several independent receivers. Returns (tcp_server, udp_transport, port)."""
    server = await asyncio.start_server(lambda r, w: handle(r, w, state), host, port)
    port = server.sockets[0].getsockname()[1]
    udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        Discovery, local_addr=(host, port)
    )
    return server, udp, port


async def main() -> None:
    server, _udp, port = await start()
    print(f"Fake receiver on 127.0.0.1:{port} (TCP control + UDP discovery)", file=sys.stderr)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
