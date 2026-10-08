"""eISCP transport against fake_receiver.py. No MCP here."""

import asyncio

import pytest

from onkyo_mcp import server as onkyo_mcp
from onkyo_mcp.server import discover, send
from onkyo_mcp.sim import fake_receiver

pytestmark = pytest.mark.anyio


async def test_send_skips_unsolicited_messages(receiver):
    # The fake pushes "NLSU0-Now Playing" before every reply; send() must skip it
    assert await send("MVLQSTN", expect="MVL") == "50"


async def test_send_changes_state(receiver):
    assert await send("PWR00", expect="PWR") == "00"
    assert receiver["PWR"] == "00"


async def test_send_without_expect_returns_none(receiver):
    assert await send("AMT01") is None


async def test_setter_in_standby_times_out(receiver):
    receiver["PWR"] = "00"
    with pytest.raises(TimeoutError):
        await send("MVL20", expect="MVL")


async def test_unreachable_raises_connection_error(receiver, monkeypatch):
    monkeypatch.setattr(onkyo_mcp, "PORT", fake_receiver.free_port())
    with pytest.raises(ConnectionError):
        await send("PWRQSTN", expect="PWR")


async def test_unknown_command_returns_na(receiver):
    assert await send("TUNQSTN", expect="TUN") == "N/A"


async def test_discover_parses_ecn_reply(receiver):
    found = await discover(timeout=0.5)
    assert found == [
        {"host": "127.0.0.1", "model": "TX-NR7100", "port": 60128, "region": "DX", "mac": "00:09:B0:12:34:56"}
    ]


@pytest.fixture
async def booting_receiver(monkeypatch):
    """Answers like a TX-NR7100 just after power-on: a status push with the same
    prefix ("AMT00") arrives before the real reply ("AMT01")."""

    async def handle(reader, writer):
        await reader.read(100)
        writer.write(fake_receiver.packet("AMT00") + fake_receiver.packet("AMT01"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    monkeypatch.setattr(onkyo_mcp, "PORT", server.sockets[0].getsockname()[1])
    async with server:
        yield


async def test_setter_skips_status_push_with_same_prefix(booting_receiver):
    assert await send("AMT01", expect="AMT", host="127.0.0.1") == "01"


async def test_query_takes_first_matching_message(booting_receiver):
    # A query has no value to compare against: any AMT message is the answer
    assert await send("AMTQSTN", expect="AMT", host="127.0.0.1") == "00"
