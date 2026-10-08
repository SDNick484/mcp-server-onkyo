"""MCP resources (context a client attaches) and prompts (workflows a user picks)."""

from __future__ import annotations

import json
import re

import pytest
from mcp import Client

from onkyo_mcp import server
from onkyo_mcp.codes import NET_SERVICE_CODES, SOURCE_CODES
from onkyo_mcp.sim.fake_receiver import FakeReceiver, free_port

from .conftest import settings_for

pytestmark = pytest.mark.anyio


async def read(c: Client, uri: str) -> dict:
    return json.loads((await c.read_resource(uri)).contents[0].text)


@pytest.fixture
async def two(fake: FakeReceiver, fake2: FakeReceiver, configure):
    configure(settings_for(fake, fake2, names=("Family Room", "Theater")))
    async with Client(server.mcp) as c:
        yield c


async def test_resources_are_listed(two):
    assert {str(r.uri) for r in (await two.list_resources()).resources} == {"onkyo://catalog", "onkyo://receivers"}
    templates = {t.uri_template for t in (await two.list_resource_templates()).resource_templates}
    assert templates == {"onkyo://receivers/{receiver}"}


async def test_catalog_is_the_code_tables(two):
    doc = await read(two, "onkyo://catalog")
    assert doc["inputs"] == SOURCE_CODES
    assert doc["net_services"] == NET_SERVICE_CODES
    assert doc["zones"]["zone2"]["commands"]["volume"] == "ZVL"
    # Each table cites the assumption its codes rest on, so a reader knows what's unconfirmed
    assert doc["assumptions"]["inputs"] == "O-SOURCE-CODES"


async def test_receivers_resource_describes_each_receiver(two, fake, fake2):
    doc = await read(two, "onkyo://receivers")
    family, theater = doc["receivers"]
    assert (family["receiver"], family["model"], family["reachable"]) == ("Family Room", "TX-NR6050", True)
    assert set(family["zones"]) == {"main", "zone2"}
    assert family["zones"]["main"]["volume_cap"] == server.registry().settings.cap("main")
    assert {"code": "04", "receiver_name": "Pandora", "name": "pandora"} in family["net_services"]
    # The TX-NR7100 profile: Zone 2 drives height speakers, so it has no volume control of its own
    assert theater["zones"]["zone2"]["volume_control"] is False


async def test_reading_twice_uses_the_cached_layout(two, fake):
    await read(two, "onkyo://receivers")
    before = len(fake.received)
    doc = await read(two, "onkyo://receivers")
    assert len(fake.received) == before  # no second NRIQSTN
    assert doc["receivers"][0]["reachable"] is None  # not contacted this time


async def test_resources_send_nothing_that_changes_anything(two, fake, fake2):
    await read(two, "onkyo://receivers")
    await read(two, "onkyo://receivers/Theater")
    assert all(cmd.endswith("QSTN") for cmd in fake.received + fake2.received)


async def test_one_receiver_by_url_encoded_name(two):
    doc = await read(two, "onkyo://receivers/family%20room")
    assert doc["receiver"] == "Family Room"


async def test_unknown_receiver_is_an_error_naming_the_real_ones(two):
    with pytest.raises(Exception, match="Family Room"):
        await two.read_resource("onkyo://receivers/Kitchen")


async def test_unreachable_receiver_is_described_not_raised(fake, configure):
    # The second "receiver" is a port nothing listens on
    configure(settings_for(fake, receivers=(*settings_for(fake).receivers, _dead_receiver())))
    async with Client(server.mcp) as c:
        doc = await read(c, "onkyo://receivers")
    alive, dead = doc["receivers"]
    assert alive["reachable"] is True
    assert dead["reachable"] is False and dead["error"]
    assert dead["described"] is False and list(dead["zones"]) == ["main"]


def _dead_receiver():
    from onkyo_mcp.config import ReceiverSettings

    return ReceiverSettings("127.0.0.1", "Garage", free_port())


async def test_no_receivers_configured_explains_discovery(configure):
    configure(settings_for())
    async with Client(server.mcp) as c:
        doc = await read(c, "onkyo://receivers")
    assert doc["receivers"] == [] and "discover_receivers" in doc["note"]


# --- prompts ----------------------------------------------------------------------------
TOOL_NAME = re.compile(r"\b(?:get|set|select|list|play|control|discover)_[a-z_]+\b")


async def test_prompts_are_listed_with_their_arguments(two):
    prompts = {p.name: p for p in (await two.list_prompts()).prompts}
    assert set(prompts) == {"play_music", "movie_night", "all_off"}
    required = {a.name for a in prompts["play_music"].arguments or [] if a.required}
    assert required == {"room"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("play_music", {"room": "patio"}),
        ("play_music", {"room": "patio", "service": "pandora", "station": "Jazz", "volume": "30"}),
        ("movie_night", {}),
        ("movie_night", {"receiver": "Theater", "source": "strm-box", "listening_mode": "dolby-surround"}),
        ("all_off", {}),
        ("all_off", {"receiver": "Theater"}),
    ],
)
async def test_prompts_only_name_tools_that_exist(two, name, args):
    # A prompt that names a renamed or removed tool would send the model looking for it
    tools = {t.name for t in (await two.list_tools()).tools}
    got = await two.get_prompt(name, args)
    text = got.messages[0].content.text
    named = set(TOOL_NAME.findall(text))
    assert named, "a prompt should walk through tools"
    assert named <= tools, f"{name} names unknown tools: {named - tools}"


async def test_prompt_fills_in_its_arguments(two):
    text = (await two.get_prompt("play_music", {"room": "patio", "service": "pandora"})).messages[0].content.text
    assert "'patio'" in text and "service='pandora'" in text
    text = (await two.get_prompt("movie_night", {})).messages[0].content.text
    assert "Don't guess the wiring" in text  # no source given: ask rather than pick an input
