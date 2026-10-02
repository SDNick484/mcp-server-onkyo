"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list schemas, argument
validation, and tool results, not just the Python functions underneath.
"""
import json

import pytest
from mcp import Client

import onkyo_mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(receiver):
    async with Client(onkyo_mcp.mcp) as c:
        yield c


def text(result) -> str:
    return result.content[0].text


async def test_tools_list(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"discover_receivers", "get_status", "set_power",
                          "set_volume", "set_mute", "set_input"}
    # Literal["bd-dvd", ...] becomes a JSON Schema enum the model must pick from
    source = tools["set_input"].input_schema["properties"]["source"]
    assert set(source["enum"]) == set(onkyo_mcp.SOURCE_CODES)


async def test_get_status(client):
    result = await client.call_tool("get_status", {})
    assert json.loads(text(result)) == {"power": "standby", "volume": 40.0,
                                        "muted": False, "input": "bd-dvd"}


async def test_get_status_unknown_input_shown_raw(client, receiver):
    receiver["SLI"] = "2C"  # an input not in SOURCE_CODES
    result = await client.call_tool("get_status", {})
    assert json.loads(text(result))["input"] == "SLI2C"


async def test_set_power(client, receiver):
    assert text(await client.call_tool("set_power", {"on": True})) == "Power is now on"
    assert receiver["PWR"] == "01"


async def test_set_mute(client, receiver):
    assert text(await client.call_tool("set_mute", {"muted": True})) == "Muted"
    assert receiver["AMT"] == "01"


async def test_set_volume(client, receiver):
    assert text(await client.call_tool("set_volume", {"level": 30.5})) == "Volume is now 30.5"
    assert receiver["MVL"] == "3D"


async def test_set_volume_capped(client, receiver):
    result = await client.call_tool("set_volume", {"level": 90})
    assert text(result) == "Volume is now 50.0 (requested 90.0, capped at 50.0)"
    assert receiver["MVL"] == "64"  # raw 0x64 = 50.0, never above the cap


async def test_set_volume_rejected_by_receiver(client, receiver):
    # Real receivers answer "MVLN/A" when they can't take the command (e.g. in
    # standby). The fake does the same for any command it doesn't know.
    del receiver["MVL"]
    result = await client.call_tool("set_volume", {"level": 20})
    assert not result.is_error
    assert "rejected" in text(result)


async def test_set_input(client, receiver):
    assert text(await client.call_tool("set_input", {"source": "game"})) == "Input is now game"
    assert receiver["SLI"] == "02"


async def test_set_input_rejected_by_receiver(client, receiver):
    del receiver["SLI"]
    assert "rejected" in text(await client.call_tool("set_input", {"source": "tv"}))


async def test_set_input_invalid_name_never_reaches_receiver(client, receiver):
    result = await client.call_tool("set_input", {"source": "hdmi9"})
    assert result.is_error
    assert receiver["SLI"] == "10"  # unchanged
