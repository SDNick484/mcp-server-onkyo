"""Settings: which receivers, their names, and the limits. Loading never fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onkyo_mcp.config import ReceiverSettings, load_settings


def load(tmp_path: Path, config: object = None, **env: str):
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config) if not isinstance(config, str) else config)
    return load_settings({"ONKYO_CONFIG_DIR": str(tmp_path), **env})


def test_nothing_configured(tmp_path):
    s = load(tmp_path)
    assert (s.receivers, s.problems, s.dry_run) == ((), (), False)
    assert {z: s.cap(z) for z in ("main", "zone2", "zone3")} == {"main": 75.0, "zone2": 75.0, "zone3": 75.0}


def test_receivers_from_config_json(tmp_path):
    s = load(
        tmp_path,
        {"receivers": [{"host": "192.168.1.147", "name": "Family Room"}, {"host": "10.0.0.9", "port": 60129}]},
    )
    assert s.receivers == (
        ReceiverSettings("192.168.1.147", "Family Room", 60128),
        ReceiverSettings("10.0.0.9", None, 60129),
    )


def test_onkyo_hosts_replaces_the_file_list_but_keeps_its_names(tmp_path):
    config = {"receivers": [{"host": "192.168.1.147", "name": "Family Room"}, {"host": "192.168.1.245"}]}
    s = load(tmp_path, config, ONKYO_HOSTS="192.168.1.147, Theater=10.0.0.9:60129")
    assert s.receivers == (
        ReceiverSettings("192.168.1.147", "Family Room", 60128),
        ReceiverSettings("10.0.0.9", "Theater", 60129),
    )


def test_single_onkyo_host_still_works(tmp_path):
    assert load(tmp_path, ONKYO_HOST="192.168.1.50").receivers == (ReceiverSettings("192.168.1.50"),)


def test_per_zone_caps(tmp_path):
    s = load(tmp_path, {"max_volume": {"main": 70, "zone2": 45}}, ONKYO_MAX_VOLUME_ZONE3="30")
    assert (s.cap("main"), s.cap("zone2"), s.cap("zone3")) == (70.0, 45.0, 30.0)


def test_one_cap_for_every_zone(tmp_path):
    s = load(tmp_path, {"max_volume": 60})
    assert {s.cap(z) for z in ("main", "zone2", "zone3")} == {60.0}
    # The environment wins over the file, and per-zone variables over the global one
    s = load(tmp_path, {"max_volume": 60}, ONKYO_MAX_VOLUME="50", ONKYO_MAX_VOLUME_ZONE2="40")
    assert (s.cap("main"), s.cap("zone2"), s.cap("zone3")) == (50.0, 40.0, 50.0)


@pytest.mark.parametrize(
    "config, env, problem",
    [
        ("{not json", {}, "is not valid JSON"),
        ([1, 2], {}, "should hold a JSON object"),
        ({"receivers": "x"}, {}, '"receivers" in config.json should be a list'),
        ({"receivers": [{"name": "no host"}]}, {}, "expected a host"),
        ({"receivers": [{"host": "192.168.1.5:60128"}]}, {}, "put the port in"),
        ({"receivers": [{"host": "10.0.0.1"}, {"host": "10.0.0.1"}]}, {}, "listed twice"),
        (
            {"receivers": [{"host": "10.0.0.1", "name": "Den"}, {"host": "10.0.0.2", "name": "den"}]},
            {},
            "two receivers",
        ),
        ({"max_volume": 150}, {}, "max_volume=150"),
        ({"max_volume": {"zone9": 10}}, {}, "zones are main, zone2, zone3"),
        ({}, {"ONKYO_MAX_VOLUME": "loud"}, "not a number"),
        ({}, {"ONKYO_VOLUME_STEPS": "3"}, "must be 1 or 2"),
        ({}, {"ONKYO_PORT": "70000"}, "ONKYO_PORT"),
        ({}, {"ONKYO_HOSTS": "Den=not a host"}, "not an IP address"),
    ],
)
def test_problems_are_reported_not_raised(tmp_path, config, env, problem):
    s = load(tmp_path, config, **env)
    assert any(problem in p for p in s.problems), s.problems


def test_bad_values_fall_back_to_defaults(tmp_path):
    s = load(tmp_path, {"max_volume": 150, "timeout": -1}, ONKYO_VOLUME_STEPS="3")
    assert (s.cap("main"), s.timeout, s.volume_steps) == (75.0, 5.0, 2)


def test_dry_run_and_debug_flags(tmp_path):
    s = load(tmp_path, ONKYO_DRY_RUN="yes", ONKYO_DEBUG="1")
    assert (s.dry_run, s.debug) == (True, True)


def test_per_receiver_volume_steps(tmp_path):
    s = load(tmp_path, {"receivers": [{"host": "10.0.0.1", "volume_steps": 1}, {"host": "10.0.0.2"}]})
    assert [s.steps_for(r) for r in s.receivers] == [1, 2]
