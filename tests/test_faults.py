"""Failure injection: the fake receiver misbehaving on purpose, and what the model is told.

Real receivers time out, drop connections, push floods of status messages,
reboot, and (rarely) send garbage. Every case must end in a message the model
can act on, never a hang or a bare "Error executing tool".
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from onkyo_mcp import server
from onkyo_mcp.sim.fake_receiver import FakeReceiver

from .conftest import settings_for

pytestmark = pytest.mark.anyio


def text(result) -> str:
    return result.content[0].text


async def run(fake: FakeReceiver, configure, tool: str, args: dict, **settings: object):
    configure(settings_for(fake, **settings))
    async with Client(server.mcp) as c:
        return await c.call_tool(tool, args)


async def test_a_query_that_never_gets_an_answer_times_out_with_advice(fake, configure):
    fake.faults.silent.add("PWR")
    result = await run(fake, configure, "get_status", {"zone": "main"})
    assert not result.is_error  # reported as unreachable, not raised
    (r,) = result.structured_content["receivers"]
    assert r["reachable"] is False and "didn't reply in time" in r["error"]


async def test_slow_replies_within_the_timeout_are_fine(fake, configure):
    fake.faults.delay = 0.1
    result = await run(fake, configure, "set_mute", {"muted": True}, timeout=1.0)
    assert not result.is_error and result.structured_content["detail"] == "Muted"


async def test_slower_than_the_timeout_says_so(fake, configure):
    fake.faults.delay = 0.4
    result = await run(fake, configure, "set_mute", {"muted": True}, timeout=0.2)
    assert result.is_error and "didn't reply in time" in text(result)


async def test_a_dropped_connection_mid_call_is_retried_for_queries(fake, configure):
    # The receiver hangs up after every 2 commands; get_status sends ~10
    # queries, each safe to repeat, so the call still succeeds.
    fake.faults.drop_after = 2
    result = await run(fake, configure, "get_status", {})
    assert not result.is_error
    assert result.structured_content["receivers"][0]["reachable"] is True


async def test_a_dropped_connection_on_a_setter_is_not_retried(fake, configure):
    # The setter gets no reply and then the receiver hangs up. It may or may
    # not have applied it, so it isn't sent again: the model is told instead.
    fake.faults.silent.add("AMT")
    fake.faults.drop_after = 1
    result = await run(fake, configure, "set_mute", {"muted": True})
    assert result.is_error
    assert "closed the connection" in text(result) or "didn't reply" in text(result)
    assert fake.received.count("AMT01") == 1  # sent once, never repeated


async def test_a_flood_of_pushes_before_the_reply(fake, configure):
    fake.faults.extra_pushes = 50  # e.g. a playing track's progress
    result = await run(fake, configure, "set_volume", {"level": 30})
    assert not result.is_error and result.structured_content["detail"] == "Volume is now 30.0"


async def test_a_garbled_reply_is_a_readable_error(fake, configure):
    fake.faults.garble.add("AMT")
    result = await run(fake, configure, "set_mute", {"muted": True})
    assert result.is_error and "couldn't be read" in text(result)


async def test_a_rebooting_receiver_refuses_connections(fake, configure):
    fake.faults.refuse_all = True
    result = await run(fake, configure, "set_power", {"state": "on"})
    assert result.is_error
    assert "closed the connection" in text(result) or "Can't connect" in text(result)


async def test_a_receiver_that_allows_one_connection_still_works_under_load(fake, configure):
    # If a receiver accepted only one connection (ASSUMPTION O-CONNECTIONS is
    # unknown), parallel calls must still succeed: the lock serializes them.
    fake.faults.max_connections = 1
    configure(settings_for(fake))
    async with Client(server.mcp) as c:
        results = await asyncio.gather(*(c.call_tool("set_mute", {"muted": i % 2 == 0}) for i in range(6)))
    assert not any(r.is_error for r in results)
    assert fake.peak_connections == 1
