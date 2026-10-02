"""
Simulated Onkyo receiver for developing without hardware.

    python fake_receiver.py            # listens on 127.0.0.1:60128
    ONKYO_HOST=127.0.0.1 python onkyo_mcp.py

It speaks enough eISCP to exercise the server: it remembers PWR/MVL/AMT/SLI
state, answers QSTN queries, and sends an unsolicited status message before
every reply, the way real receivers do, so the server's filtering gets tested.

Tests import it and call `start(port=0)` to get a receiver on a free port.
"""

import asyncio
import struct
import sys

# MVL 0x50 = 80 raw -> displays 40.0 on a 0.5-step model (TX-NR6050/7100)
DEFAULT_STATE = {"PWR": "00", "MVL": "50", "AMT": "00", "SLI": "10"}
state = dict(DEFAULT_STATE)


def reset_state() -> None:
    state.clear()
    state.update(DEFAULT_STATE)


def packet(msg: str) -> bytes:
    data = f"!1{msg}\x1a\r\n".encode("ascii")
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            header = await reader.readexactly(16)
            _, header_size, data_size, _ = struct.unpack(">4sIIB3x", header)
            await reader.readexactly(header_size - 16)
            cmd = (await reader.readexactly(data_size)).decode("ascii")[2:].strip()
            code, param = cmd[:3], cmd[3:]
            print(f"<- {cmd}", file=sys.stderr)

            writer.write(packet("NLSU0-Now Playing"))  # unsolicited noise
            if code not in state:
                writer.write(packet(f"{code}N/A"))
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
            self.transport.sendto(packet("ECNTX-NR7100/60128/DX/0009B0623D93"), addr)


async def start(host: str = "127.0.0.1", port: int = 60128):
    """Start TCP control and UDP discovery on the same port number, like a real
    receiver. Pass port=0 to pick a free one. Returns (tcp_server, udp_transport, port)."""
    server = await asyncio.start_server(handle, host, port)
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
