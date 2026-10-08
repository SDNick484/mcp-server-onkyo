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


@pytest.mark.anyio
async def test_doctor_checks_each_layer(fake, fake2, tmp_path):
    from onkyo_mcp.doctor import render, run_doctor

    from .conftest import settings_for

    fake2.faults.silent.add("ZPW")  # the 7100 lists Zone 2 but never answers for it
    settings = settings_for(fake, fake2, names=("Family Room", "Theater"))
    report = await run_doctor(settings, timeout=0.5, dump=tmp_path)
    family, theater = report.receivers
    assert family.ok and [c.step for c in family.checks] == ["tcp", "eiscp", "describe", "zones"]
    assert family.model == "TX-NR6050" and family.zones == {"main": "on", "zone2": "standby"}
    assert not theater.ok
    assert theater.checks[-1].hint.startswith("zone2 listed but silent")
    assert not report.ok
    assert "Theater" in render(report) and "PROBLEMS FOUND" in render(report)
    # Every command doctor sent was a query: it never changes anything
    assert all(cmd.endswith("QSTN") for cmd in fake.received + fake2.received)
    assert (tmp_path / "Family_Room.json").exists()


@pytest.mark.anyio
async def test_doctor_unreachable_receiver_says_what_to_check():
    from onkyo_mcp.config import ReceiverSettings, Settings
    from onkyo_mcp.doctor import run_doctor
    from onkyo_mcp.sim.fake_receiver import free_port

    s = Settings(receivers=(ReceiverSettings("127.0.0.1", "Den", free_port()),), discovery_port=free_port())
    report = await run_doctor(s, timeout=0.3)
    (den,) = report.receivers
    assert [c.step for c in den.checks] == ["tcp"] and not den.ok
    assert "Network Standby" in den.checks[0].hint and "step 2" in den.checks[0].hint


def test_call_and_simulate_end_to_end(tmp_path):
    """The README's walkthrough: simulate, then call tools against it, as subprocesses."""
    import json
    import signal
    import time

    exe = shutil.which("mcp-server-onkyo", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "ONKYO_CONFIG_DIR": str(tmp_path)}
    sim = subprocess.Popen(
        [exe, "simulate", "--port", "0", "--write-config", str(tmp_path)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if (tmp_path / "config.json").exists():
                break
            time.sleep(0.05)
        out = subprocess.run(
            [exe, "call", "set_volume", "level=30", "receiver=theater"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["sent"] == ["MVL3C"]
        refused = subprocess.run(
            [exe, "call", "set_volume", "level=30"], env=env, capture_output=True, text=True, timeout=30
        )
        assert refused.returncode == 1 and "Several receivers are configured" in refused.stderr
        listed = subprocess.run([exe, "call", "tools"], env=env, capture_output=True, text=True, timeout=30)
        assert "get_status" in listed.stdout
    finally:
        sim.send_signal(signal.SIGINT)
        sim.wait(timeout=10)
