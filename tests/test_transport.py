"""eISCP transport against fake_receiver.py. No MCP here."""
import pytest

from onkyo_mcp import discover, send

pytestmark = pytest.mark.anyio


async def test_send_skips_unsolicited_messages(receiver):
    # The fake pushes "NLSU0-Now Playing" before every reply; send() must skip it
    assert await send("MVLQSTN", expect="MVL") == "50"


async def test_send_changes_state(receiver):
    assert await send("PWR01", expect="PWR") == "01"
    assert receiver["PWR"] == "01"


async def test_send_without_expect_returns_none(receiver):
    assert await send("AMT01") is None


async def test_unknown_command_returns_na(receiver):
    assert await send("LMDQSTN", expect="LMD") == "N/A"


async def test_discover_parses_ecn_reply(receiver):
    found = await discover(timeout=0.5)
    assert found == [{"host": "127.0.0.1", "model": "TX-NR7100", "port": 60128,
                      "region": "DX", "mac": "00:09:B0:62:3D:93"}]
