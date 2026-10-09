"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list schemas, argument
validation, and tool results, not just the Python functions underneath.
"""

import json
import logging

import pytest
from mcp import Client

from onkyo_mcp import codes, eiscp, server
from onkyo_mcp.sim.fake_receiver import free_port

from .conftest import settings_for

pytestmark = pytest.mark.anyio


def text(result) -> str:
    """What the model reads: an error's message, or a setter's detail plus its
    warnings (ActionResult), or the plain text of other results."""
    sc = result.structured_content
    if not result.is_error and isinstance(sc, dict) and "detail" in sc:
        return ". ".join([sc["detail"], *sc["warnings"]])
    return result.content[0].text


async def test_tools_list(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "discover_receivers",
        "get_status",
        "set_power",
        "set_volume",
        "set_mute",
        "set_input",
        "set_listening_mode",
        "select_net_service",
        "list_net_services",
        "get_now_playing",
        "list_stations",
        "play_station",
        "control_playback",
    }
    # Literal["bd-dvd", ...] becomes a JSON Schema enum the model must pick from
    source = tools["set_input"].input_schema["properties"]["source"]
    assert set(source["enum"]) == set(codes.SOURCE_CODES)
    mode = tools["set_listening_mode"].input_schema["properties"]["mode"]
    assert set(mode["enum"]) == set(codes.MODE_CODES)


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


def zones(result) -> dict:
    """get_status's zones for the (only) receiver, by name."""
    (r,) = result.structured_content["receivers"]
    return {z["zone"]: z for z in r["zones"]}


async def test_get_status(client, fake):
    result = await client.call_tool("get_status", {})
    assert not result.is_error
    assert result.structured_content == {
        "dry_run": False,
        "receivers": [
            {
                "receiver": "127.0.0.1",
                "host": "127.0.0.1",
                "model": "TX-NR6050",  # from its self-description
                "reachable": True,
                "error": None,
                # Every zone it has, each in full: the fake has Zone 2 (in standby), no Zone 3
                "zones": [
                    {
                        "zone": "main",
                        "power": "on",
                        "volume": 40.0,
                        "volume_cap": 75.0,
                        "volume_control": True,
                        "muted": False,
                        "input": "bd-dvd",
                        "listening_mode": "stereo",
                    },
                    {
                        "zone": "zone2",
                        "power": "standby",
                        "volume": 40.0,
                        "volume_cap": 75.0,
                        "volume_control": True,
                        "muted": False,
                        "input": "same-as-main",
                        "listening_mode": None,
                    },
                ],
                "net_zones": [],
            }
        ],
    }


async def test_get_status_publishes_an_output_schema(client):
    schema = {t.name: t for t in (await client.list_tools()).tools}["get_status"].output_schema
    assert schema is not None and set(schema["required"]) == {"dry_run", "receivers"}


async def test_net_zones_lists_zones_sharing_the_network_player(client, receiver):
    receiver.update(SLI="2B", ZPW="01", SLZ="2B")  # both zones on "net"
    result = await client.call_tool("get_status", {})
    assert result.structured_content["receivers"][0]["net_zones"] == ["main", "zone2"]


async def test_every_receiver_tool_takes_receiver_argument(client):
    for tool in (await client.list_tools()).tools:
        if tool.name != "discover_receivers":
            assert "receiver" in tool.input_schema["properties"], tool.name
            assert "receiver" not in tool.input_schema.get("required", []), tool.name


async def test_get_status_unknown_input_shown_raw(client, receiver):
    receiver["SLI"] = "2C"  # an input not in SOURCE_CODES
    result = await client.call_tool("get_status", {})
    assert zones(result)["main"]["input"] == "SLI2C"


async def test_set_power(client, receiver):
    assert text(await client.call_tool("set_power", {"state": "off"})) == "Power is now standby"
    assert receiver["PWR"] == "00"
    assert text(await client.call_tool("set_power", {"state": "on"})).startswith("Power is now on.")
    assert receiver["PWR"] == "01"


async def test_set_mute(client, receiver):
    assert text(await client.call_tool("set_mute", {"muted": True})) == "Muted"
    assert receiver["AMT"] == "01"


async def test_set_volume(client, receiver):
    assert text(await client.call_tool("set_volume", {"level": 30.5})) == "Volume is now 30.5"
    assert receiver["MVL"] == "3D"


async def test_set_volume_capped(client, receiver):
    result = await client.call_tool("set_volume", {"level": 90})
    assert text(result) == "Volume is now 75.0. Requested 90, capped at 75 (the owner's limit for this zone)."
    assert receiver["MVL"] == "96"  # raw 0x96 = 75.0, never above the cap


async def test_set_volume_rejected_by_receiver(client, receiver):
    # Some receivers answer "MVLN/A" when they can't take a command. The fake
    # does the same for any command it doesn't know. A rejection is an error
    # (isError), so the model can't mistake it for success.
    del receiver["MVL"]
    result = await client.call_tool("set_volume", {"level": 20})
    assert result.is_error
    assert "rejected the volume change" in text(result)


async def test_set_input(client, receiver):
    assert text(await client.call_tool("set_input", {"source": "game"})) == "Input is now game"
    assert receiver["SLI"] == "02"


async def test_set_input_rejected_by_receiver(client, receiver):
    del receiver["SLI"]
    result = await client.call_tool("set_input", {"source": "tv"})
    assert result.is_error and "rejected input 'tv'" in text(result)


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
    assert result.is_error and "rejected listening mode" in text(result)


async def test_set_listening_mode_invalid_name(client, receiver):
    result = await client.call_tool("set_listening_mode", {"mode": "thx"})
    assert result.is_error
    assert receiver["LMD"] == "00"  # unchanged


async def test_debug_logs_mcp_and_eiscp_traffic(client, receiver, caplog):
    caplog.set_level(logging.DEBUG, logger="onkyo_mcp")
    await client.call_tool("set_mute", {"muted": True})
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("onkyo_mcp")]
    # In order: the request in, the packets out and back, the result out
    assert any(m.startswith("MCP <- [") and "tools/call" in m and '"muted": true' in m for m in lines)
    assert "eISCP -> 127.0.0.1 AMT01" in lines
    assert "eISCP <- 127.0.0.1 NLSU0-Now Playing (unsolicited, skipped)" in lines
    assert "eISCP <- 127.0.0.1 AMT01" in lines
    assert any(m.startswith("MCP -> [") and "Muted" in m for m in lines)


async def test_no_traffic_logged_by_default(client, receiver, caplog):
    caplog.set_level(logging.INFO)  # anything below WARNING stays silent unless debugging
    await client.call_tool("set_mute", {"muted": True})
    assert not [r for r in caplog.records if r.name.startswith("onkyo_mcp")]


@pytest.mark.parametrize("style", ["silent", "na"])
async def test_setter_in_standby_says_so(client, receiver, fake, style):
    # In standby a TX-NR7100 ignores setters without replying ("silent"); a
    # TX-NR6050 answers volume and mute with N/A ("na"). ASSUMPTION O-STANDBY-SILENT.
    # Either way the model must hear why, not just "Error executing tool set_volume".
    fake.standby = style
    receiver["PWR"] = "00"
    result = await client.call_tool("set_volume", {"level": 20})
    assert result.is_error
    assert text(result) == (
        "Error executing tool set_volume: The receiver at 127.0.0.1 is in standby. Turn it on with set_power first."
    )
    assert receiver["MVL"] == "50"  # unchanged


async def test_status_works_in_standby(client, receiver):
    receiver["PWR"] = "00"  # queries are still answered in standby
    result = await client.call_tool("get_status", {})
    assert zones(result)["main"]["power"] == "standby"


async def test_unreachable_receiver_says_so(fake, configure):
    fake.port = free_port()  # point the server where nothing listens
    configure(settings_for(fake))
    async with Client(server.mcp) as c:
        result = await c.call_tool("set_mute", {"muted": True})
    assert result.is_error
    assert "Can't connect to the receiver at 127.0.0.1" in text(result)


async def test_zone_arguments_in_schema(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("set_power", "set_volume", "set_mute", "set_input"):
        zone = tools[name].input_schema["properties"]["zone"]
        assert set(zone["enum"]) == {"main", "zone2", "zone3"}, name
        assert zone["default"] == "main", name
    # get_status's zone is a filter: omitted means every zone
    status_zone = tools["get_status"].input_schema["properties"]["zone"]
    assert status_zone["default"] is None
    assert "zone" not in tools["set_listening_mode"].input_schema["properties"]


async def test_zone2_status(client, receiver):
    result = await client.call_tool("get_status", {"zone": "zone2"})
    assert list(zones(result)) == ["zone2"]  # just the zone asked for
    assert zones(result)["zone2"]["power"] == "standby"


async def test_zone2_on_net_at_capped_volume(client, receiver):
    # The request that started this: "turn on Zone 2 with network audio"
    assert text(await client.call_tool("set_power", {"state": "on", "zone": "zone2"})).startswith(
        "Zone 2: Power is now on."
    )
    assert text(await client.call_tool("set_input", {"source": "net", "zone": "zone2"})) == "Zone 2: Input is now net"
    result = await client.call_tool("set_volume", {"level": 90, "zone": "zone2"})
    assert text(result) == "Zone 2: Volume is now 75.0. Requested 90, capped at 75 (the owner's limit for this zone)."
    assert text(await client.call_tool("set_mute", {"muted": True, "zone": "zone2"})) == "Zone 2: Muted"
    assert (receiver["ZPW"], receiver["SLZ"], receiver["ZVL"], receiver["ZMT"]) == ("01", "2B", "96", "01")
    # The main zone is untouched
    assert (receiver["SLI"], receiver["MVL"], receiver["AMT"]) == ("10", "50", "00")


async def test_zone2_input_changes_in_standby_on_a_tx_nr6050(client, receiver):
    # Seen on hardware: a TX-NR6050 changes a zone's input while it's in standby
    result = await client.call_tool("set_input", {"source": "net", "zone": "zone2"})
    assert text(result) == "Zone 2: Input is now net"
    assert (receiver["ZPW"], receiver["SLZ"]) == ("00", "2B")


async def test_zone2_in_standby_says_so(client, receiver, fake):
    fake.standby = "silent"  # like a TX-NR7100
    result = await client.call_tool("set_input", {"source": "net", "zone": "zone2"})
    assert result.is_error
    assert (
        "Zone 2 of the receiver at 127.0.0.1 is in standby. Turn it on with set_power with zone='zone2' first."
    ) in text(result)
    assert receiver["SLZ"] == "80"  # unchanged


async def test_missing_zone_says_so(client, receiver):
    # The fake describes itself (NRIQSTN) as a TX-NR6050, which has no zone 3
    result = await client.call_tool("get_status", {"zone": "zone3"})
    assert result.is_error
    assert "The receiver at 127.0.0.1, a TX-NR6050, has no Zone 3." in text(result)


async def test_missing_zone_without_self_description(client, receiver):
    # Older models answer "N/A" to NRIQSTN; then the zone's own reply decides
    receiver["NRI"] = "N/A"
    result = await client.call_tool("get_status", {"zone": "zone3"})
    assert "doesn't have Zone 3" in text(result)


async def test_fixed_volume_zone_says_so(client, receiver):
    # Like the owner's TX-NR7100, whose Zone 2 outputs drive height speakers
    receiver["NRI"] = receiver["NRI"].replace('name="Zone2" volmax="100"', 'name="Zone2" volmax="0"')
    result = await client.call_tool("set_volume", {"level": 20, "zone": "zone2"})
    assert result.is_error
    assert "Zone 2 of the receiver at 127.0.0.1 has no volume control" in text(result)
    assert receiver["ZVL"] == "50"  # never sent


async def test_same_as_main_only_for_other_zones(client, receiver):
    result = await client.call_tool("set_input", {"source": "same-as-main"})
    assert result.is_error
    assert receiver["SLI"] == "10"  # never sent


async def test_select_net_service(client, receiver):
    result = await client.call_tool("select_net_service", {"service": "pandora"})
    assert text(result).startswith("Network service is now Pandora")
    assert receiver["NLT"].startswith("04")


async def test_select_unavailable_net_service_says_so(client, receiver):
    # The fake doesn't offer Spotify, so it never confirms the switch
    result = await client.call_tool("select_net_service", {"service": "spotify"})
    assert result.is_error
    assert "didn't switch to spotify" in text(result)


async def test_net_service_names_match_code_table(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    service = tools["select_net_service"].input_schema["properties"]["service"]
    assert set(service["enum"]) == set(codes.NET_SERVICE_CODES)


async def test_now_playing_idle(client, receiver):
    result = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert result == {
        "receiver": "127.0.0.1",
        "service": None,
        "station": None,
        "menu": "NET",
        "state": "stopped",
        "title": None,
        "artist": None,
        "album": None,
        "position": None,
    }


async def test_now_playing_service_survives_menu_browsing(client, receiver):
    # Pandora keeps playing after someone goes back to the top menu: NLT then
    # says "NET", but NMS still ends with Pandora's icon
    await client.call_tool("select_net_service", {"service": "pandora"})
    receiver["NLT"] = "F3000000000E0000FFFF00NET"
    result = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert (result["service"], result["menu"]) == ("pandora", "NET")


async def test_now_playing_track_with_accents(client, receiver):
    receiver.update(NST="Pxx1", NTI="Déjà Vu", NAT="Beyoncé", NAL="B'Day", NTM="00:01:02/00:04:00")
    await client.call_tool("select_net_service", {"service": "pandora"})
    result = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert result == {
        "receiver": "127.0.0.1",
        "service": "pandora",
        "station": None,
        "menu": "Pandora",
        "state": "playing",
        "title": "Déjà Vu",
        "artist": "Beyoncé",
        "album": "B'Day",
        "position": "00:01:02/00:04:00",
    }


async def test_mute_rejected_is_not_reported_as_unmuted(client, receiver):
    del receiver["AMT"]  # the fake answers N/A, as a TX-NR7100 zone 3 in standby did
    result = await client.call_tool("set_mute", {"muted": True})
    assert result.is_error and "rejected the mute change" in text(result)


async def test_list_stations_only_music(client, receiver):
    result = await client.call_tool("list_stations", {"service": "pandora"})
    # No "Create new station" or "Sign Out"; the duplicate listed once
    assert json.loads(text(result))["playable"] == ["Shuffle", "Pearl Jam Radio", "Beyoncé Radio"]
    assert json.loads(text(result))["folders"] == []


async def test_play_station_by_partial_name(client, receiver, fake):
    result = await client.call_tool("play_station", {"station": "pearl jam"})
    assert text(result).startswith("Playing Pearl Jam Radio on pandora")
    assert fake.selected == [3]  # the first Pearl Jam Radio
    playing = json.loads(text(await client.call_tool("get_now_playing", {})))
    assert (playing["station"], playing["state"], playing["title"]) == ("Pearl Jam Radio", "playing", "Black")


async def test_play_station_never_selects_account_items(client, receiver, fake):
    for name in ("Sign Out", "Create new station"):
        result = await client.call_tool("play_station", {"station": name})
        assert result.is_error and "No station matching" in text(result)
    assert fake.selected == []


async def test_play_station_ambiguous(client, receiver, fake):
    result = await client.call_tool("play_station", {"station": "radio"})
    assert result.is_error
    assert "Pearl Jam Radio, Beyoncé Radio" in text(result)
    assert fake.selected == []


async def test_control_playback(client, receiver):
    await client.call_tool("play_station", {"station": "Beyoncé Radio"})
    assert text(await client.call_tool("control_playback", {"action": "pause"})) == "Paused"
    assert receiver["NST"].startswith("p")
    assert text(await client.call_tool("control_playback", {"action": "play"})) == "Playing"
    assert text(await client.call_tool("control_playback", {"action": "next"})) == "Now playing Interstate Love Song"


async def test_previous_refused_says_so(client, receiver):
    await client.call_tool("play_station", {"station": "Shuffle"})
    result = await client.call_tool("control_playback", {"action": "previous"})
    assert result.is_error and "didn't change" in text(result)


async def test_play_with_nothing_selected_says_so(client, receiver):
    result = await client.call_tool("control_playback", {"action": "play"})
    assert result.is_error and "play_station" in text(result)


async def test_playback_annotations(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["control_playback"].annotations.idempotent_hint is False  # "next" twice != once


async def test_playing_station_still_listed_and_playable(client, receiver):
    # The receiver marks the playing station icontype "0" instead of "M"
    await client.call_tool("play_station", {"station": "Beyoncé Radio"})
    result = await client.call_tool("list_stations", {})
    assert json.loads(text(result))["playable"] == ["Shuffle", "Pearl Jam Radio", "Beyoncé Radio"]
    assert text(await client.call_tool("play_station", {"station": "beyoncé"})).startswith(
        "Playing Beyoncé Radio on pandora"
    )


async def test_no_host_uses_the_one_discovered_receiver(fake, configure):
    configure(settings_for(fake, receivers=()))  # nothing configured: discovery decides
    async with Client(server.mcp) as c:
        result = (await c.call_tool("get_status", {})).structured_content
        assert result["receivers"][0]["host"] == "127.0.0.1"  # the fake answered discovery
        assert server.registry()._discovered is not None  # remembered for later calls


async def test_no_host_and_nothing_discovered_says_so(fake, configure):
    configure(settings_for(fake, receivers=(), discovery_port=free_port()))  # nobody answers there
    async with Client(server.mcp) as c:
        result = await c.call_tool("set_mute", {"muted": True})
    assert result.is_error and "Set ONKYO_HOST" in text(result)
    assert fake.state["AMT"] == "00"  # nothing was sent anywhere


async def test_no_host_and_several_receivers_asks_which(fake, configure, monkeypatch):
    async def two_receivers(address, port=60128, timeout=3.0):
        return [
            eiscp.Found("192.168.1.147", "TX-NR6050", 60128, "DX", ""),
            eiscp.Found("192.168.1.245", "TX-NR7100", 60128, "DX", ""),
        ]

    configure(settings_for(fake, receivers=()))
    monkeypatch.setattr(eiscp, "discover", two_receivers)
    async with Client(server.mcp) as c:
        result = await c.call_tool("get_status", {})
    assert result.is_error
    assert "TX-NR6050 at 192.168.1.147, TX-NR7100 at 192.168.1.245" in text(result)


async def test_signed_out_service_says_so(client, receiver):
    result = await client.call_tool("select_net_service", {"service": "tidal"})
    assert result.is_error
    assert 'tidal isn\'t ready: the receiver shows "TIDAL Login"' in text(result)
    result = await client.call_tool("play_station", {"station": "anything", "service": "tidal"})
    assert result.is_error and "TIDAL Login" in text(result)


async def test_browse_folders(client, receiver):
    top = json.loads(text(await client.call_tool("list_stations", {"service": "tunein"})))
    assert (top["playable"], top["folders"]) == ([], ["My Presets", "Local Radio"])
    presets = json.loads(text(await client.call_tool("list_stations", {"service": "tunein", "folder": ["presets"]})))
    assert presets["playable"] == ["KQED Public Radio", "KCSM Jazz"]


async def test_play_from_folder(client, receiver, fake):
    result = await client.call_tool("play_station", {"station": "jazz", "service": "tunein", "folder": ["My Presets"]})
    assert text(result).startswith("Playing KCSM Jazz on tunein")
    assert fake.selected == [2]  # position inside the folder


async def test_play_at_folder_level_points_inside(client, receiver):
    result = await client.call_tool("play_station", {"station": "kqed", "service": "tunein"})
    assert result.is_error
    assert "It has folders: My Presets, Local Radio" in text(result)


async def test_missing_folder_lists_what_is_there(client, receiver):
    result = await client.call_tool("list_stations", {"service": "tunein", "folder": ["Podcasts"]})
    assert result.is_error
    assert "No folder matching 'Podcasts'. Here: My Presets, Local Radio." in text(result)


async def test_empty_folder_passes_on_receiver_message(client, receiver):
    result = await client.call_tool("list_stations", {"service": "tunein", "folder": ["local"]})
    assert json.loads(text(result))["message"] == "No stations available"


async def test_long_lists_are_read_in_pages(client, receiver):
    # 250 albums: three NLA pages (100 + 100 + 50), positions counted across pages
    albums = json.loads(text(await client.call_tool("list_stations", {"service": "music-server", "folder": ["Album"]})))
    assert len(albums["folders"]) == 250 and albums["folders"][-1] == "Album 250"
    result = await client.call_tool(
        "play_station", {"station": "Track 237", "service": "music-server", "folder": ["Album", "Album 237"]}
    )
    assert text(result).startswith("Playing Track 237 on music-server")


async def test_volume_cap_off_the_step_grid_is_never_exceeded(fake, configure):
    # ONKYO_MAX_VOLUME=60.3 on a 0.5-step receiver: 60.3 isn't a step, and
    # rounding to the nearest one (60.5) used to overshoot it.
    configure(settings_for(fake, max_volume={"main": 60.3, "zone2": 60.3, "zone3": 60.3}))
    async with Client(server.mcp) as c:
        result = await c.call_tool("set_volume", {"level": 100})
    assert text(result) == "Volume is now 60.0. Requested 100, capped at 60.3 (the owner's limit for this zone)."
    assert fake.state["MVL"] == "78"  # 120 raw = 60.0, the highest step at or below 60.3


@pytest.mark.parametrize("level, shown", [(30.25, 30.5), (30.75, 31.0), (31.25, 31.5), (30.1, 30.0), (30.3, 30.5)])
async def test_volume_rounds_to_the_nearest_step_halves_up(client, level, shown):
    assert text(await client.call_tool("set_volume", {"level": level})) == f"Volume is now {shown}"


async def test_volume_outside_0_to_100_is_rejected_by_the_schema(client, receiver):
    for level in (-5, 150):
        result = await client.call_tool("set_volume", {"level": level})
        assert result.is_error
    assert receiver["MVL"] == "50"  # nothing sent
    schema = {t.name: t for t in (await client.list_tools()).tools}["set_volume"].input_schema
    assert (schema["properties"]["level"]["minimum"], schema["properties"]["level"]["maximum"]) == (0, 100)


async def test_per_zone_cap(fake, configure):
    configure(settings_for(fake, max_volume={"main": 75.0, "zone2": 45.0, "zone3": 75.0}))
    fake.state["ZPW"] = "01"
    async with Client(server.mcp) as c:
        zone2 = await c.call_tool("set_volume", {"level": 60, "zone": "zone2"})
        main = await c.call_tool("set_volume", {"level": 60})
    assert text(zone2) == "Zone 2: Volume is now 45.0. Requested 60, capped at 45 (the owner's limit for this zone)."
    assert text(main) == "Volume is now 60.0"


async def test_browsing_another_service_while_one_plays_is_refused(client, receiver, fake):
    await client.call_tool("play_station", {"station": "Shuffle"})  # Pandora playing
    result = await client.call_tool("list_stations", {"service": "tunein"})
    assert result.is_error and "pandora is playing. Browsing tunein would stop it" in text(result)
    assert receiver["NLT"].startswith("04")  # still on Pandora: nothing was switched
    ok = await client.call_tool("list_stations", {"service": "tunein", "interrupt": True})
    assert not ok.is_error
    # Browsing the playing service itself is always fine
    await client.call_tool("play_station", {"station": "Shuffle"})
    assert not (await client.call_tool("list_stations", {"service": "pandora"})).is_error


async def test_garbled_menu_list_is_a_readable_error(client, receiver, fake):
    fake.faults.garble.add("NLA")
    result = await client.call_tool("list_stations", {"service": "pandora"})
    assert result.is_error
    assert "couldn't be read" in text(result)


async def test_unreachable_receiver_is_reported_not_raised(fake, configure):
    fake.port = free_port()
    configure(settings_for(fake))
    async with Client(server.mcp) as c:
        result = await c.call_tool("get_status", {})
    assert not result.is_error
    (r,) = result.structured_content["receivers"]
    assert (r["reachable"], r["zones"]) == (False, [])
    assert "Can't connect" in r["error"]


async def test_zone_without_volume_control_reports_none(client, receiver):
    receiver["NRI"] = receiver["NRI"].replace('name="Zone2" volmax="100"', 'name="Zone2" volmax="0"')
    z2 = zones(await client.call_tool("get_status", {}))["zone2"]
    assert (z2["volume"], z2["volume_control"]) == (None, False)


async def test_playing_with_no_zone_on_net_says_nothing_will_be_heard(client, receiver):
    result = await client.call_tool("play_station", {"station": "Shuffle"})
    assert 'No zone is on input "net" yet' in text(result)


async def test_playing_with_one_zone_on_net_has_no_warning(client, receiver):
    receiver["SLI"] = "2B"
    assert text(await client.call_tool("play_station", {"station": "Shuffle"})) == "Playing Shuffle on pandora"


async def test_service_change_warns_when_two_zones_share_the_player(client, receiver):
    receiver.update(SLI="2B", ZPW="01", SLZ="2B")
    result = await client.call_tool("select_net_service", {"service": "tunein"})
    assert text(result) == (
        'Network service is now TuneIn Radio. Note: Main zone and Zone 2 are both on "net", and a receiver has '
        "one network player, so both hear this. To keep one zone out, give it another input."
    )


async def test_putting_a_second_zone_on_net_says_it_follows_the_first(client, receiver):
    receiver.update(SLI="2B", ZPW="01")
    result = await client.call_tool("set_input", {"source": "net", "zone": "zone2"})
    assert text(result).startswith("Zone 2: Input is now net. It now plays the same network audio as Main zone")


async def test_list_net_services(client, receiver):
    result = (await client.call_tool("list_net_services", {})).structured_content
    by_code = {s["code"]: s for s in result["services"]}
    assert by_code["04"] == {"code": "04", "receiver_name": "Pandora", "name": "pandora", "selectable": True}
    assert by_code["44"]["name"] == "airplay"
    assert result["receiver"] == "127.0.0.1"


async def test_list_net_services_shows_ones_this_server_cant_select(client, receiver):
    receiver["NRI"] = receiver["NRI"].replace(
        "</netservicelist>", '<netservice id="13" value="1" name="iHeartRadio"/></netservicelist>'
    )
    services = (await client.call_tool("list_net_services", {})).structured_content["services"]
    assert {"code": "13", "receiver_name": "iHeartRadio", "name": None, "selectable": False} in services


async def test_setters_return_a_structured_result(client, receiver):
    result = await client.call_tool("set_volume", {"level": 30, "zone": "main"})
    assert result.structured_content == {
        "receiver": "127.0.0.1",
        "zone": "main",
        "outcome": "done",
        "detail": "Volume is now 30.0",
        "sent": ["MVL3C"],  # exactly what went to the receiver
        "warnings": [],
    }
    schema = {t.name: t for t in (await client.list_tools()).tools}["set_volume"].output_schema
    assert set(schema["required"]) == {"receiver", "zone", "outcome", "detail", "sent", "warnings"}


@pytest.mark.parametrize(
    "tool, args, sent",
    [
        ("set_power", {"state": "off"}, ["PWR00"]),
        ("set_volume", {"level": 90}, ["MVL96"]),  # still capped in a dry run: 75.0
        ("set_mute", {"muted": True}, ["AMT01"]),
        ("set_input", {"source": "net"}, ["SLI2B"]),
        ("set_listening_mode", {"mode": "direct"}, ["LMD01"]),
        ("select_net_service", {"service": "pandora"}, ["NSV040"]),
        ("play_station", {"station": "pearl jam"}, ["NSV040", "NLSI<position of 'pearl jam'>"]),
        ("control_playback", {"action": "next"}, ["NTCTRUP"]),
    ],
)
async def test_dry_run_sends_nothing_and_says_what_it_would_send(fake, configure, tool, args, sent):
    configure(settings_for(fake, dry_run=True))
    before = dict(fake.state)
    async with Client(server.mcp) as c:
        result = await c.call_tool(tool, args)
        status = (await c.call_tool("get_status", {})).structured_content
    assert not result.is_error, text(result)
    assert result.structured_content["outcome"] == "dry_run"
    assert result.structured_content["detail"].startswith("DRY RUN, nothing sent: would ")
    assert result.structured_content["sent"] == sent
    assert all(cmd.endswith("QSTN") for cmd in fake.received), fake.received  # only queries reached the receiver
    assert fake.state == before
    assert status["dry_run"] is True


async def test_dry_run_still_checks_the_call(fake, configure):
    # Reads still happen, so a dry run catches what a real call would refuse
    configure(settings_for(fake, dry_run=True))
    async with Client(server.mcp) as c:
        result = await c.call_tool("set_volume", {"level": 20, "zone": "zone3"})
    assert result.is_error and "has no Zone 3" in text(result)
