"""Contract tests: the server's exact eISCP traffic, against transcripts.

Each fixture in tests/fixtures/eiscp/ is one tool call as a transcript:
every command the server must send, in order, and what the receiver
answers (pushes included). ReplayReceiver serves it strictly, so these
tests pin the protocol down to the byte: a changed command, an extra query
or a different order fails with the exchange where it diverged.

The fixtures are hand-built from traffic seen on the owner's TX-NR6050 and
TX-NR7100 (see each one's "source") and name the assumptions they rest on.
Transcripts captured from real hardware (`doctor --dump`, or `--debug`
logs) can be added the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from onkyo_mcp import server
from onkyo_mcp.assumptions import BY_ID
from onkyo_mcp.config import ReceiverSettings, Settings
from onkyo_mcp.sim.fake_receiver import ReplayReceiver

pytestmark = pytest.mark.anyio

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "eiscp").glob("*.json"))


def text(result) -> str:
    return result.content[0].text


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
async def test_transcript(path: Path, configure):
    fixture = json.loads(path.read_text())
    assert set(fixture["assumptions"]) <= set(BY_ID), "fixtures must cite real assumption ids"
    replay = await ReplayReceiver(fixture["exchanges"]).start()
    try:
        configure(Settings(receivers=(ReceiverSettings(replay.host, None, replay.port),), timeout=0.3))
        async with Client(server.mcp) as c:
            result = await c.call_tool(fixture["tool"], fixture["args"])
    finally:
        await replay.stop()
    assert replay.mismatches == []
    assert replay.finished, f"the server stopped after {replay.position} of {len(replay.exchanges)} exchanges"
    if "expect_error" in fixture:
        assert result.is_error and fixture["expect_error"] in text(result)
    else:
        assert not result.is_error, text(result)
        for key, value in fixture["expect"].items():
            assert result.structured_content[key] == value, key


def test_every_fixture_says_where_it_came_from():
    for path in FIXTURES:
        fixture = json.loads(path.read_text())
        assert fixture["source"] and fixture["description"] and fixture["assumptions"], path.name
