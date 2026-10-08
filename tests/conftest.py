"""Shared fixtures. Every test gets its own fake receivers on free ports; no hardware or LAN needed."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable

import pytest
from mcp import Client

from onkyo_mcp import server
from onkyo_mcp.config import ReceiverSettings, Settings
from onkyo_mcp.sim.fake_receiver import FakeReceiver, make


# Async tests use anyio's pytest plugin (marked with pytest.mark.anyio). Unlike
# pytest-asyncio, it runs an async fixture's setup and teardown in the same task,
# which the MCP SDK's Client (built on anyio) requires.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def fake() -> AsyncIterator[FakeReceiver]:
    """A fresh fake TX-NR6050 on a free port."""
    f = await make("TX-NR6050").start()
    yield f
    await f.stop()


@pytest.fixture
async def fake2() -> AsyncIterator[FakeReceiver]:
    """A second, independent fake: a TX-NR7100, on its own free port."""
    f = await make("TX-NR7100").start()
    f.state["SLI"] = "02"  # on "game", so tests can tell the two apart
    yield f
    await f.stop()


def settings_for(*fakes: FakeReceiver, names: tuple[str | None, ...] = (), **overrides: object) -> Settings:
    """Settings pointing at the given fakes. The fakes answer instantly, so a
    short timeout keeps the tests that expect *no* reply fast."""
    receivers = tuple(
        ReceiverSettings(f.host, names[i] if i < len(names) else None, f.port) for i, f in enumerate(fakes)
    )
    base = Settings(
        receivers=receivers,
        timeout=0.5,
        discovery_addr="127.0.0.1",
        discovery_port=fakes[0].port if fakes else 9,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch) -> Callable[[Settings], None]:
    """Use these settings for the next server run (the lifespan reads them)."""

    def apply(settings: Settings) -> None:
        monkeypatch.setattr(server, "settings_factory", lambda: settings)

    return apply


@pytest.fixture
async def receiver(fake: FakeReceiver, configure: Callable[[Settings], None]) -> AsyncIterator[dict[str, object]]:
    """The server pointed at one fake receiver. Yields the fake's state dict, so
    tests can inspect or tweak what the "receiver" holds."""
    configure(settings_for(fake))
    yield fake.state


@pytest.fixture
async def client(receiver: dict[str, object]) -> AsyncIterator[Client]:
    async with Client(server.mcp) as c:
        yield c
