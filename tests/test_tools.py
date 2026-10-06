"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list schemas, argument
validation, and tool results, not just the Python functions underneath.
"""
import json
import logging

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
                          "set_volume", "set_mute", "set_input", "set_listening_mode",
                          "select_net_service", "get_now_playing"}
    # Literal["bd-dvd", ...] becomes a JSON Schema enum the model must pick from
    source = tools["set_input"].input_schema["properties"]["source"]
    assert set(source["enum"]) == set(onkyo_mcp.SOURCE_CODES)
    mode = tools["set_listening_mode"].input_schema["properties"]["mode"]
    assert set(mode["enum"]) == set(onkyo_mcp.MODE_CODES)


async def test_tool_annotations(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    # Every tool states its hints rather than falling back to the spec's
    # pessimistic defaults (destructive, non-idempotent, open-world).
    for tool in tools.values():
        assert tool.title and tool.annotations is not None
    assert tools["get_status"].annotations.read_only_hint is True
    assert tools["discover_receivers"].annotations.open_world_hint is True
    volume = tools["set_volume"].annotations
    assert volume.read_only_hint is False
    assert volume.destructive_hint is False
    assert volume.idempotent_hint is True


async def test_get_status(client):
    result = await client.call_tool("get_status", {})
    assert json.loads(text(result)) == {"receiver": "127.0.0.1", "zone": "main", "power": "on",
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
    assert (default["receiver"], default["power"], default["input"]) == ("127.0.0.1", "on", "bd-dvd")


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
    assert text(await client.call_tool("set_power", {"on": False})) == "Power is now standby"
    assert receiver["PWR"] == "00"
    assert text(await client.call_tool("set_power", {"on": True})).startswith("Power is now on.")
    assert receiver["PWR"] == "01"


async def test_set_mute(client, receiver):
    assert text(await client.call_tool("set_mute", {"muted": True})) == "Muted"
    assert receiver["AMT"] == "01"


async def test_set_volume(client, receiver):
    assert text(await client.call_tool("set_volume", {"level": 30.5})) == "Volume is now 30.5"
    assert receiver["MVL"] == "3D"


async def test_set_volume_capped(client, receiver):
    result = await client.call_tool("set_volume", {"level": 90})
    assert text(result) == "Volume is now 75.0 (requested 90.0, capped at 75.0)"
    assert receiver["MVL"] == "96"  # raw 0x96 = 75.0, never above the cap


async def test_set_volume_rejected_by_receiver(client, receiver):
    # Some receivers answer "MVLN/A" when they can't take a command. The fake
    # does the same for any command it doesn't know.
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


async def test_debug_logs_mcp_and_eiscp_traffic(client, receiver, caplog):
    caplog.set_level(logging.DEBUG, logger="onkyo_mcp")
    await client.call_tool("set_mute", {"muted": True})
    lines = [r.getMessage() for r in caplog.records if r.name == "onkyo_mcp"]
    # In order: the request in, the packets out and back, the result out
    assert any(m.startswith("MCP <- [") and "tools/call" in m and '"muted": true' in m for m in lines)
    assert "eISCP -> 127.0.0.1 AMT01" in lines
    assert "eISCP <- 127.0.0.1 NLSU0-Now Playing (unsolicited, skipped)" in lines
    assert "eISCP <- 127.0.0.1 AMT01" in lines
    assert any(m.startswith("MCP -> [") and "Muted" in m for m in lines)


async def test_no_traffic_logged_by_default(client, receiver, caplog):
    caplog.set_level(logging.INFO)  # anything below WARNING stays silent unless debugging
    await client.call_tool("set_mute", {"muted": True})
    assert not [r for r in caplog.records if r.name == "onkyo_mcp"]


async def test_setter_in_standby_says_so(client, receiver):
    # Like a TX-NR7100, the fake ignores setters in standby without replying.
    # The model must hear why, not just "Error executing tool set_volume".
    receiver["PWR"] = "00"
    result = await client.call_tool("set_volume", {"level": 20})
    assert result.is_error
    assert text(result) == ("Error executing tool set_volume: The receiver at 127.0.0.1 "
                            "is in standby. Turn it on with set_power first.")
    assert receiver["MVL"] == "50"  # unchanged


async def test_status_works_in_standby(client, receiver):
    receiver["PWR"] = "00"  # queries are still answered in standby
    result = await client.call_tool("get_status", {})
    assert json.loads(text(result))["power"] == "standby"


async def test_unreachable_receiver_says_so(client, receiver, monkeypatch):
    monkeypatch.setattr(onkyo_mcp, "PORT", fake_receiver.free_port())  # nothing listening
    result = await client.call_tool("set_mute", {"muted": True})
    assert result.is_error
    assert "Can't connect to a receiver at 127.0.0.1" in text(result)


async def test_zone_arguments_in_schema(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("get_status", "set_power", "set_volume", "set_mute", "set_input"):
        zone = tools[name].input_schema["properties"]["zone"]
        assert set(zone["enum"]) == {"main", "zone2", "zone3"}, name
        assert zone["default"] == "main", name
    assert "zone" not in tools["set_listening_mode"].input_schema["properties"]


async def test_zone2_status(client, receiver):
    result = await client.call_tool("get_status", {"zone": "zone2"})
    assert json.loads(text(result)) == {"receiver": "127.0.0.1", "zone": "zone2", "power": "standby",
                                        "volume": 40.0, "muted": False, "input": "same-as-main"}


async def test_zone2_on_net_at_capped_volume(client, receiver):
    # The request that started this: "turn on Zone 2 with network audio"
    assert text(await client.call_tool("set_power", {"on": True, "zone": "zone2"})).startswith(
        "Zone 2: Power is now on.")
    assert text(await client.call_tool("set_input", {"source": "net", "zone": "zone2"})) == \
        "Zone 2: Input is now net"
    result = await client.call_tool("set_volume", {"level": 90, "zone": "zone2"})
    assert text(result) == "Zone 2: Volume is now 75.0 (requested 90.0, capped at 75.0)"
    assert text(await client.call_tool("set_mute", {"muted": True, "zone": "zone2"})) == "Zone 2: Muted"
    assert (receiver["ZPW"], receiver["SLZ"], receiver["ZVL"], receiver["ZMT"]) == ("01", "2B", "96", "01")
    # The main zone is untouched
    assert (receiver["SLI"], receiver["MVL"], receiver["AMT"]) == ("10", "50", "00")


async def test_zone2_in_standby_says_so(client, receiver):
    result = await client.call_tool("set_input", {"source": "net", "zone": "zone2"})
    assert result.is_error
    assert ("Zone 2 of the receiver at 127.0.0.1 is in standby. "
            "Turn it on with set_power with zone='zone2' first.") in text(result)
    assert receiver["SLZ"] == "80"  # unchanged


async def test_missing_zone_says_so(client, receiver):
    # The fake has no zone 3: it answers "N/A" to PW3QSTN
    result = await client.call_tool("get_status", {"zone": "zone3"})
    assert result.is_error
    assert "doesn't have Zone 3" in text(result)


async def test_same_as_main_only_for_other_zones(client, receiver):
    result = await client.call_tool("set_input", {"source": "same-as-main"})
    assert result.is_error
    assert receiver["SLI"] == "10"  # never sent



async def test_select_net_service(client, receiver):
    result = await client.call_tool("select_net_service", {"service": "pandora"})
    assert text(result) == "Network service is now Pandora"
    assert receiver["NLT"].startswith("04")


async def test_select_unavailable_net_service_says_so(client, receiver):
    # The fake doesn't offer Spotify, so it never confirms the switch
    result = await client.call_tool("select_net_service", {"service": "spotify"})
    assert result.is_error
    assert "didn't switch to spotify" in text(result)


async def test_net_service_names_match_code_table(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    service = tools["select_net_service"].input_schema["properties"]["service"]
    assert set(service["enum"]) == set(onkyo_mcp.NET_SERVICE_CODES)


async def test_now_playing_idle(client, receiver):
    result = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert result == {"receiver": "127.0.0.1", "service": "NET", "state": "stopped",
                      "title": None, "artist": None, "album": None, "position": None}


async def test_now_playing_track_with_accents(client, receiver):
    receiver.update(NST="Pxx1", NTI="Déjà Vu", NAT="Beyoncé", NAL="B'Day",
                    NTM="00:01:02/00:04:00")
    await client.call_tool("select_net_service", {"service": "pandora"})
    result = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert result == {"receiver": "127.0.0.1", "service": "pandora", "state": "playing",
                      "title": "Déjà Vu", "artist": "Beyoncé", "album": "B'Day",
                      "position": "00:01:02/00:04:00"}


async def test_mute_rejected_is_not_reported_as_unmuted(client, receiver):
    del receiver["AMT"]  # the fake answers N/A, as a TX-NR7100 zone 3 in standby did
    result = await client.call_tool("set_mute", {"muted": True})
    assert "rejected" in text(result)
