"""Settings: which receivers, what to call them, and the safety limits.

Receivers come from ``config.json`` (in ``ONKYO_CONFIG_DIR``, default
``~/.config/mcp-server-onkyo``) and/or the environment::

    {
      "receivers": [
        {"host": "192.168.1.147", "name": "Family Room"},
        {"host": "192.168.1.245", "name": "Theater", "volume_steps": 2}
      ],
      "max_volume": {"main": 75, "zone2": 50}
    }

``name`` is optional; without one a receiver is known by its address. Tools
accept either. ``ONKYO_HOSTS`` (comma-separated ``host``, ``host:port`` or
``Name=host``) or the older single ``ONKYO_HOST`` replaces the file's list,
keeping the file's names for hosts it also lists.

With no receiver configured at all, the server falls back to discovery and
uses the receiver that answers, if exactly one does.

eISCP needs no pairing and no credentials: anything that can reach TCP 60128
can control a receiver. So, like mcp-server-harmony and unlike
mcp-server-shieldtv, nothing secret is stored here.

Loading never fails: anything wrong becomes a sentence in
``Settings.problems`` (logged at startup and shown by ``doctor``) and the bad
part is skipped or defaulted, so one typo doesn't stop every receiver.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import Zone
from .eiscp import DEFAULT_PORT

DEFAULT_MAX_VOLUME = 75.0
DEFAULT_TIMEOUT = 5.0
ZONES: tuple[Zone, ...] = ("main", "zone2", "zone3")


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("ONKYO_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-onkyo"


@dataclass(frozen=True)
class ReceiverSettings:
    host: str
    name: str | None = None
    port: int = DEFAULT_PORT
    # Raw volume steps per display unit; None = the global setting.
    volume_steps: int | None = None

    @property
    def label(self) -> str:
        """How messages and results refer to it: its name, else its address."""
        return self.name or self.host


@dataclass(frozen=True)
class Settings:
    receivers: tuple[ReceiverSettings, ...] = ()
    # Safety cap on the display scale (0-100), per zone. Enforced by the
    # server, never left to the model.
    max_volume: Mapping[Zone, float] = field(default_factory=lambda: dict.fromkeys(ZONES, DEFAULT_MAX_VOLUME))
    # 2021+ models (TX-NR6050, TX-NR7100) use 0.5 steps: raw 0x00-0xC8 is
    # 0.0-100.0, so 2 raw steps per display unit. Older models: 1.
    volume_steps: int = 2
    # Seconds to connect, then to wait for each reply. A TX-NR6050 answers in
    # ~0.1 s, a TX-NR7100 takes ~1.5 s even for a query. Power commands get 3x.
    timeout: float = DEFAULT_TIMEOUT
    discovery_addr: str = "255.255.255.255"
    discovery_port: int = DEFAULT_PORT
    # Read the receivers, but send nothing that changes anything; every write
    # reports what it would have sent (ONKYO_DRY_RUN=1, serve --dry-run).
    dry_run: bool = False
    debug: bool = False
    problems: tuple[str, ...] = ()

    def cap(self, zone: Zone) -> float:
        return self.max_volume.get(zone, DEFAULT_MAX_VOLUME)

    def steps_for(self, receiver: ReceiverSettings) -> int:
        return receiver.volume_steps or self.volume_steps


# --- parsing helpers ---------------------------------------------------------------
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)


def is_address(text: str) -> bool:
    """An IP address or a hostname (what a receiver's `host` may be)."""
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return bool(_HOSTNAME.fullmatch(text))


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _setting(env: Mapping[str, str], name: str) -> str | None:
    """An ONKYO_* variable, or None if unset *or empty*. Empty happens in
    practice: WSL turns a variable listed in WSLENV but not set on the Windows
    side into "", which must mean "default", not crash float("")."""
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _number(raw: Any, what: str, problems: list[str], *, lo: float, hi: float) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(f"ignoring {what}={raw!r}: not a number")
        return None
    if not lo <= value <= hi:
        problems.append(f"ignoring {what}={raw!r}: must be from {lo:g} to {hi:g}")
        return None
    return value


def _port(raw: Any, what: str, problems: list[str]) -> int | None:
    value = _number(raw, what, problems, lo=1, hi=65535)
    if value is None:
        return None
    if value != int(value):
        problems.append(f"ignoring {what}={raw!r}: must be a whole number")
        return None
    return int(value)


def _steps(raw: Any, what: str, problems: list[str]) -> int | None:
    if raw in (1, 2, "1", "2"):
        return int(raw)
    problems.append(f"ignoring {what}={raw!r}: must be 1 or 2")
    return None


def _read_json(path: Path, problems: list[str]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        problems.append(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}); ignoring it")
        return {}
    except OSError as exc:
        problems.append(f"can't read {path} ({exc}); ignoring it")
        return {}
    if not isinstance(data, dict):
        problems.append(f"{path} should hold a JSON object, not {type(data).__name__}; ignoring it")
        return {}
    return data


def _file_receivers(data: dict[str, Any], default_port: int, problems: list[str]) -> list[ReceiverSettings]:
    out: list[ReceiverSettings] = []
    entries = data.get("receivers") or []
    if not isinstance(entries, list):
        problems.append('"receivers" in config.json should be a list; ignoring it')
        entries = []
    for e in entries:
        if isinstance(e, str):
            e = {"host": e}
        if not isinstance(e, dict) or not isinstance(e.get("host"), str) or not e["host"].strip():
            problems.append(f"ignoring receiver entry {e!r} in config.json: expected a host or {{host, name}}")
            continue
        host = e["host"].strip()
        if not is_address(host):
            problems.append(f'ignoring receiver {host!r}: not an IP address or hostname (put the port in "port")')
            continue
        name = e.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        port = default_port if e.get("port") is None else _port(e["port"], f"port of {host}", problems)
        steps = (
            None if e.get("volume_steps") is None else _steps(e["volume_steps"], f"volume_steps of {host}", problems)
        )
        out.append(ReceiverSettings(host, name, port or default_port, steps))
    return out


def _env_receivers(value: str, default_port: int, problems: list[str]) -> list[ReceiverSettings]:
    """ONKYO_HOSTS: "192.168.1.147, Theater=192.168.1.245, 10.0.0.9:60129"."""
    out: list[ReceiverSettings] = []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        name, _, address = item.rpartition("=")
        host, port = address.strip(), default_port
        # host:port, but not an IPv6 address (which has several colons)
        if host.count(":") == 1:
            host, raw_port = host.split(":")
            parsed = _port(raw_port, f"port in ONKYO_HOSTS entry {item!r}", problems)
            if parsed is None:
                continue
            port = parsed
        if not is_address(host):
            problems.append(f"ignoring ONKYO_HOSTS entry {item!r}: {host!r} is not an IP address or hostname")
            continue
        out.append(ReceiverSettings(host, name.strip() or None, port))
    return out


def _dedupe(receivers: list[ReceiverSettings], problems: list[str]) -> tuple[ReceiverSettings, ...]:
    seen_addr: set[tuple[str, int]] = set()
    seen_name: dict[str, str] = {}
    out: list[ReceiverSettings] = []
    for r in receivers:
        if (r.host, r.port) in seen_addr:
            problems.append(f"receiver {r.host}:{r.port} is listed twice; using the first entry")
            continue
        seen_addr.add((r.host, r.port))
        if r.name:
            key = name_key(r.name)
            if key in seen_name:
                problems.append(
                    f"two receivers are named {r.name!r} ({seen_name[key]} and {r.host}); give one another name, "
                    "or tools can only tell them apart by address"
                )
            seen_name[key] = r.host
        out.append(r)
    return tuple(out)


def name_key(name: str) -> str:
    """Names match ignoring case, spaces and punctuation: "Family Room" = "family-room"."""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _caps(data: dict[str, Any], env: Mapping[str, str], problems: list[str]) -> dict[Zone, float]:
    """The volume cap per zone. A number caps every zone; an object sets some."""
    caps: dict[Zone, float] = dict.fromkeys(ZONES, DEFAULT_MAX_VOLUME)
    raw = data.get("max_volume")
    if isinstance(raw, dict):
        for zone, value in raw.items():
            if zone not in ZONES:
                problems.append(f"ignoring max_volume for {zone!r}: zones are {', '.join(ZONES)}")
            elif (cap := _number(value, f"max_volume.{zone}", problems, lo=0, hi=100)) is not None:
                caps[zone] = cap
    elif raw is not None and (cap := _number(raw, "max_volume", problems, lo=0, hi=100)) is not None:
        caps = dict.fromkeys(ZONES, cap)
    # The environment wins: ONKYO_MAX_VOLUME for every zone, then per zone.
    if (value := _setting(env, "ONKYO_MAX_VOLUME")) and (
        cap := _number(value, "ONKYO_MAX_VOLUME", problems, lo=0, hi=100)
    ) is not None:
        caps = dict.fromkeys(ZONES, cap)
    other_zones: tuple[Zone, ...] = ("zone2", "zone3")
    for zone in other_zones:
        name = f"ONKYO_MAX_VOLUME_{zone.upper()}"
        if (value := _setting(env, name)) and (cap := _number(value, name, problems, lo=0, hi=100)) is not None:
            caps[zone] = cap
    return caps


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    problems: list[str] = []
    data = _read_json(config_dir(env) / "config.json", problems)

    default_port = DEFAULT_PORT
    if (value := _setting(env, "ONKYO_PORT")) and (port := _port(value, "ONKYO_PORT", problems)):
        default_port = port

    receivers = _file_receivers(data, default_port, problems)
    from_env = _setting(env, "ONKYO_HOSTS") or _setting(env, "ONKYO_HOST")
    if from_env:
        file_entries = {r.host: r for r in receivers}
        receivers = [
            ReceiverSettings(
                r.host,
                r.name or (file_entries[r.host].name if r.host in file_entries else None),
                r.port,
                file_entries[r.host].volume_steps if r.host in file_entries else None,
            )
            for r in _env_receivers(from_env, default_port, problems)
        ]

    steps = 2
    raw_steps = _setting(env, "ONKYO_VOLUME_STEPS") or data.get("volume_steps")
    if raw_steps is not None and (parsed := _steps(raw_steps, "volume_steps", problems)):
        steps = parsed

    timeout = DEFAULT_TIMEOUT
    raw_timeout = _setting(env, "ONKYO_TIMEOUT") or data.get("timeout")
    if raw_timeout is not None and (t := _number(raw_timeout, "timeout", problems, lo=0.1, hi=120)) is not None:
        timeout = t

    return Settings(
        receivers=_dedupe(receivers, problems),
        max_volume=_caps(data, env, problems),
        volume_steps=steps,
        timeout=timeout,
        discovery_addr=_setting(env, "ONKYO_DISCOVERY_ADDR") or "255.255.255.255",
        discovery_port=default_port,
        dry_run=_flag(env.get("ONKYO_DRY_RUN")),
        debug=_flag(env.get("ONKYO_DEBUG")),
        problems=tuple(problems),
    )
