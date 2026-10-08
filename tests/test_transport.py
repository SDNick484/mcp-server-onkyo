"""eISCP transport against the fake receiver. No MCP here."""

from __future__ import annotations

import asyncio

import pytest

from onkyo_mcp.eiscp import Connection, Found, discover, send
from onkyo_mcp.sim.fake_receiver import FakeReceiver, free_port, packet

pytestmark = pytest.mark.anyio


async def test_send_skips_unsolicited_messages(fake: FakeReceiver):
    # The fake pushes "NLSU0-Now Playing" before every reply; send() must skip it
    assert await send(fake.host, fake.port, "MVLQSTN", expect="MVL") == "50"


async def test_send_changes_state(fake: FakeReceiver):
    assert await send(fake.host, fake.port, "PWR00", expect="PWR") == "00"
    assert fake.state["PWR"] == "00"


async def test_send_without_expect_returns_none(fake: FakeReceiver):
    assert await send(fake.host, fake.port, "AMT01") is None


async def test_setter_in_standby_times_out(fake: FakeReceiver):
    fake.state["PWR"] = "00"
    with pytest.raises(TimeoutError):
        await send(fake.host, fake.port, "MVL20", expect="MVL", timeout=0.3)


async def test_unreachable_raises_connection_error():
    with pytest.raises(ConnectionError):
        await send("127.0.0.1", free_port(), "PWRQSTN", expect="PWR")


async def test_unknown_command_returns_na(fake: FakeReceiver):
    assert await send(fake.host, fake.port, "TUNQSTN", expect="TUN") == "N/A"


async def test_one_connection_carries_several_commands(fake: FakeReceiver):
    async with await Connection.open(fake.host, fake.port, 1.0) as conn:
        assert await conn.request("PWRQSTN", "PWR", 1.0) == "01"
        assert await conn.request("MVL3C", "MVL", 1.0) == "3C"
        assert await conn.request("MVLQSTN", "MVL", 1.0) == "3C"
    assert fake.peak_connections == 1


async def test_discover_parses_ecn_reply(fake: FakeReceiver):
    found = await discover("127.0.0.1", fake.port, timeout=0.3)
    assert found == [Found("127.0.0.1", "TX-NR6050", fake.port, "DX", "00:09:B0:F7:6C:FD")]


@pytest.fixture
async def booting_receiver() -> object:
    """Answers like a TX-NR7100 just after power-on: a status push with the same
    prefix ("AMT00") arrives before the real reply ("AMT01")."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(100)
        writer.write(packet("AMT00") + packet("AMT01"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]


async def test_setter_skips_status_push_with_same_prefix(booting_receiver: int):
    assert await send("127.0.0.1", booting_receiver, "AMT01", expect="AMT") == "01"


async def test_query_takes_first_matching_message(booting_receiver: int):
    # A query has no value to compare against: any AMT message is the answer
    assert await send("127.0.0.1", booting_receiver, "AMTQSTN", expect="AMT") == "00"
