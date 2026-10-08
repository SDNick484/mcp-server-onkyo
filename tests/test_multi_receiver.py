"""Several receivers behind one server: picking one by name, and never guessing.

Two fakes run side by side on different ports of 127.0.0.1 (no IPv6 or extra
loopback addresses needed), each with its own state, like two receivers on
one LAN.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from onkyo_mcp import server
from onkyo_mcp.config import ReceiverSettings
from onkyo_mcp.receivers import Registry
from onkyo_mcp.sim.fake_receiver import FakeReceiver

from .conftest import settings_for

pytestmark = pytest.mark.anyio


def text(result) -> str:
    return result.content[0].text


@pytest.fixture
async def two(fake: FakeReceiver, fake2: FakeReceiver, configure):
    configure(settings_for(fake, fake2, names=("Family Room", "Theater")))
    async with Client(server.mcp) as c:
        yield c


async def test_pick_by_name_ignoring_case_and_spaces(two, fake, fake2):
    family = json.loads(text(await two.call_tool("get_status", {"receiver": "family room"})))
    theater = json.loads(text(await two.call_tool("get_status", {"receiver": "THEATER"})))
    assert (family["receiver"], family["input"]) == ("Family Room", "bd-dvd")
    assert (theater["receiver"], theater["input"]) == ("Theater", "game")


async def test_pick_by_address(two, fake2):
    result = json.loads(text(await two.call_tool("get_status", {"receiver": f"127.0.0.1:{fake2.port}"})))
    assert result["receiver"] == "Theater"


@pytest.mark.parametrize(
    "tool, args, code, value",
    [
        ("set_power", {"on": False}, "PWR", "00"),
        ("set_volume", {"level": 20}, "MVL", "28"),
        ("set_mute", {"muted": True}, "AMT", "01"),
        ("set_input", {"source": "tv"}, "SLI", "12"),
        ("set_listening_mode", {"mode": "direct"}, "LMD", "01"),
    ],
)
async def test_set_tools_change_only_the_named_receiver(two, fake, fake2, tool, args, code, value):
    before = fake.state[code]
    result = await two.call_tool(tool, {**args, "receiver": "Theater"})
    assert not result.is_error, text(result)
    assert fake2.state[code] == value
    assert fake.state[code] == before  # the other receiver is untouched


@pytest.mark.parametrize("tool, args", [("set_power", {"on": False}), ("set_volume", {"level": 20})])
async def test_writes_without_receiver_are_refused_not_guessed(two, fake, fake2, tool, args):
    result = await two.call_tool(tool, args)
    assert result.is_error
    assert "Several receivers are configured" in text(result)
    assert "Family Room" in text(result) and "Theater" in text(result)
    assert fake.received == [] and fake2.received == []  # nothing was sent to either


async def test_unknown_name_lists_the_receivers(two):
    result = await two.call_tool("set_mute", {"muted": True, "receiver": "Kitchen"})
    assert result.is_error
    assert "No receiver named 'Kitchen'. Receivers: Family Room" in text(result)


async def test_duplicate_names_are_refused():
    reg = Registry(
        settings_for(
            *(),
            receivers=(ReceiverSettings("10.0.0.1", "Den"), ReceiverSettings("10.0.0.2", "den")),
        )
    )
    with pytest.raises(Exception, match="matches several receivers"):
        await reg.pick("Den")


async def test_concurrent_calls_take_turns_on_one_receiver(fake, configure):
    # Five get_status calls at once (as from two MCP clients over HTTP) must
    # not open five connections at once: the receiver lock queues them.
    configure(settings_for(fake))
    async with Client(server.mcp) as c:
        results = await asyncio.gather(*(c.call_tool("get_status", {}) for _ in range(5)))
    assert not any(r.is_error for r in results)
    assert fake.peak_connections == 1


async def test_calls_to_different_receivers_run_side_by_side(two, fake, fake2):
    fake.faults.delay = fake2.faults.delay = 0.05
    start = asyncio.get_running_loop().time()
    await asyncio.gather(
        two.call_tool("set_mute", {"muted": True, "receiver": "Family Room"}),
        two.call_tool("set_mute", {"muted": True, "receiver": "Theater"}),
    )
    # One receiver's lock doesn't hold up the other: both finish in about one delay
    assert asyncio.get_running_loop().time() - start < 0.5
