"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list schemas, argument
validation, and tool results, not just the Python functions underneath.
"""
import json

import pytest
from mcp import Client

import fake_receiver
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
                          "set_volume", "set_mute", "set_input", "set_listening_mode"}
    # Literal["bd-dvd", ...] becomes a JSON Schema enum the model must pick from
    source = tools["set_input"].input_schema["properties"]["source"]
    assert set(source["enum"]) == set(onkyo_mcp.SOURCE_CODES)
    mode = tools["set_listening_mode"].input_schema["properties"]["mode"]
    assert set(mode["enum"]) == set(onkyo_mcp.MODE_CODES)


async def test_get_status(client):
    result = await client.call_tool("get_status", {})
    assert json.loads(text(result)) == {"receiver": "127.0.0.1", "power": "standby",
                                        "volume": 40.0, "muted": False,
                                        "input": "bd-dvd", "listening_mode": "stereo"}


@pytest.fixture
async def second_receiver(receiver):
    """A second fake receiver at ::1 (IPv6 loopback, same port, its own state),
    like two receivers on one LAN. 127.0.0.2 would be neater but isn't routed
    on macOS or WSL2. Yields its state dict."""
    state = dict(fake_receiver.DEFAULT_STATE, PWR="01", SLI="02")
    try:
        server, udp, _ = await fake_receiver.start("::1", onkyo_mcp.PORT, state)
    except OSError:
        pytest.skip("no IPv6 loopback on this machine")
    yield state
    udp.close()
    server.close()
    await server.wait_closed()


async def test_every_receiver_tool_takes_receiver_argument(client):
    for tool in (await client.list_tools()).tools:
        if tool.name != "discover_receivers":
            assert "receiver" in tool.input_schema["properties"], tool.name
            assert "receiver" not in tool.input_schema.get("required", []), tool.name


async def test_get_status_picks_receiver(client, second_receiver):
    other = json.loads(text(await client.call_tool("get_status", {"receiver": "::1"})))
    default = json.loads(text(await client.call_tool("get_status", {})))
    assert (other["receiver"], other["power"], other["input"]) == ("::1", "on", "game")
    assert (default["receiver"], default["power"], default["input"]) == ("127.0.0.1", "standby", "bd-dvd")


@pytest.mark.parametrize("tool, args, code, value", [
    ("set_power", {"on": False}, "PWR", "00"),  # second receiver starts on
    ("set_volume", {"level": 20}, "MVL", "28"),
    ("set_mute", {"muted": True}, "AMT", "01"),
    ("set_input", {"source": "tv"}, "SLI", "12"),
    ("set_listening_mode", {"mode": "direct"}, "LMD", "01"),
])
async def test_set_tools_change_only_chosen_receiver(client, receiver, second_receiver,
                                                     tool, args, code, value):
    default_before = receiver[code]
    result = await client.call_tool(tool, {**args, "receiver": "::1"})
    assert not result.is_error
    assert second_receiver[code] == value
    assert receiver[code] == default_before  # the default receiver is untouched


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


async def test_set_listening_mode(client, receiver):
    result = await client.call_tool("set_listening_mode", {"mode": "dolby-surround"})
    assert text(result) == "Listening mode is now dolby-surround"
    assert receiver["LMD"] == "80"


async def test_set_listening_mode_rejected_by_receiver(client, receiver):
    del receiver["LMD"]  # e.g. DTS Neural:X requested on a signal that can't use it
    result = await client.call_tool("set_listening_mode", {"mode": "dts-neural-x"})
    assert "rejected" in text(result)


async def test_set_listening_mode_invalid_name(client, receiver):
    result = await client.call_tool("set_listening_mode", {"mode": "thx"})
    assert result.is_error
    assert receiver["LMD"] == "00"  # unchanged
