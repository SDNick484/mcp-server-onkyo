"""The real Onkyo tools over Streamable HTTP, behind the Cloudflare Access check.

test_remote.py (shared with the sibling servers) tests the transport with a
one-tool server. This runs *this* server's tools end to end: a uvicorn server
on a free port, MCP over HTTP, Access assertions signed with a throwaway RSA
key (so nothing here needs Cloudflare), and a fake receiver behind it all.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from onkyo_mcp import remote, server
from onkyo_mcp.sim.fake_receiver import FakeReceiver

from .conftest import settings_for

pytestmark = pytest.mark.anyio

TEAM = "example.cloudflareaccess.com"
AUD = "onkyo-aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POLICY = remote.AccessPolicy(TEAM, (AUD,))


def token(**overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "nick@example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


async def public_key(_token: str) -> object:
    return KEY.public_key()


@asynccontextmanager
async def serving(fake: FakeReceiver, configure) -> AsyncIterator[str]:
    configure(settings_for(fake))
    cfg = remote.HttpConfig(path="/onkyo/mcp", access=POLICY)
    app = remote.build_app(server.mcp, cfg, remote.AccessVerifier(POLICY, public_key))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(srv.serve, [sock])
        while not srv.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/onkyo/mcp"
        srv.should_exit = True


def client(url: str, assertion: str | None) -> Client:
    headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


async def test_tools_work_over_http_with_a_valid_assertion(fake, configure):
    async with serving(fake, configure) as url, client(url, token()) as c:
        status = json.loads((await c.call_tool("get_status", {})).content[0].text)
        assert (status["power"], status["volume"]) == ("on", 40.0)
        assert (await c.call_tool("set_volume", {"level": 30})).content[0].text == "Volume is now 30.0"
    assert fake.state["MVL"] == "3C"


@pytest.mark.parametrize(
    "assertion",
    [
        None,  # no header: didn't come through Access
        "garbage",
        token(exp=int(time.time()) - 3600),  # expired
        token(aud=["someone-elses-app"]),  # wrong audience
        token(iss="https://evil.cloudflareaccess.com"),  # wrong issuer
    ],
    ids=["missing", "garbage", "expired", "wrong-audience", "wrong-issuer"],
)
async def test_bad_assertions_never_reach_the_receiver(fake, configure, assertion):
    async with serving(fake, configure) as url, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "set_power"}}
        resp = await http.post(url, json=body, headers=headers)
        assert (resp.status_code, resp.json()) == (403, {"error": "forbidden"})
    assert fake.received == []  # the receiver heard nothing


async def test_two_http_sessions_take_turns_on_the_receiver(fake, configure):
    # Two clients calling at once share one server process; the receiver
    # lock means the receiver only ever sees one connection at a time.
    async with serving(fake, configure) as url:

        async def session() -> list[bool]:
            async with client(url, token()) as c:
                results = await asyncio.gather(*(c.call_tool("get_status", {}) for _ in range(4)))
                return [r.is_error for r in results]

        assert await asyncio.gather(session(), session()) == [[False] * 4, [False] * 4]
    assert fake.peak_connections == 1
