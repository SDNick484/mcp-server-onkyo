import pytest

import fake_receiver
import onkyo_mcp


# Async tests use anyio's pytest plugin (marked with pytest.mark.anyio). Unlike
# pytest-asyncio, it runs an async fixture's setup and teardown in the same task,
# which the MCP SDK's Client (built on anyio) requires.
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def receiver(monkeypatch):
    """A fresh fake receiver on a free port, with the server pointed at it.
    Yields the fake's state dict so tests can inspect or tweak it."""
    fake_receiver.reset_state()
    server, udp, port = await fake_receiver.start(port=0)
    # The server reads these module globals on every call, so patching works
    monkeypatch.setattr(onkyo_mcp, "HOST", "127.0.0.1")
    monkeypatch.setattr(onkyo_mcp, "PORT", port)
    monkeypatch.setattr(onkyo_mcp, "DISCOVERY_ADDR", "127.0.0.1")
    # The fake answers instantly, so only tests that expect no reply ever wait
    monkeypatch.setattr(onkyo_mcp, "TIMEOUT", 0.5)
    yield fake_receiver.state
    udp.close()
    server.close()
    await server.wait_closed()
