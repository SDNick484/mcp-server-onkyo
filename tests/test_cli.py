"""The command line: subcommands, and the old spellings that must keep working."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from onkyo_mcp.cli import build_parser, normalize


@pytest.mark.parametrize(
    "argv, cmd",
    [
        ([], "serve"),
        (["--http"], "serve"),  # Onkyo's original spelling
        (["serve", "--http"], "serve"),  # the siblings' spelling
        (["--discover"], "discover"),  # the original flag
        (["discover", "--timeout", "1"], "discover"),
    ],
)
def test_spellings(argv, cmd):
    assert build_parser().parse_args(normalize(argv)).cmd == cmd


def test_http_flags_parse_both_ways():
    a = build_parser().parse_args(normalize(["--http", "--port", "9000"]))
    b = build_parser().parse_args(normalize(["serve", "--http", "--port", "9000"]))
    assert (a.http, a.port) == (b.http, b.port) == (True, 9000)


def test_unsafe_bind_is_refused_before_serving(tmp_path):
    exe = shutil.which("mcp-server-onkyo", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "ONKYO_CONFIG_DIR": str(tmp_path)}
    env.pop("CF_ACCESS_TEAM_DOMAIN", None)
    out = subprocess.run([exe, "--http", "--bind", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 2
    assert "Refusing to serve on 0.0.0.0 without Cloudflare Access" in out.stderr
