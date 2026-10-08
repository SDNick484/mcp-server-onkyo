"""`mcp-server-onkyo doctor`: check everything between this machine and each receiver, layer by layer.

First contact with a receiver (or a new network) fails in a handful of ways,
and "it doesn't work" doesn't say which. So each receiver is checked one
layer at a time, and the first failure says what it means and what to try:

  1. config     - config.json parses; hosts and limits are valid (Settings.problems)
  2. tcp        - something accepts a connection on the receiver's eISCP port (O-FRAMING)
  3. eiscp      - it answers a power query (PWRQSTN) in the expected framing (O-PUSHES)
  4. describe   - it describes itself (NRIQSTN): model, zones, services (O-NRI-ZONES, O-NRI-SERVICES)
  5. zones      - each zone it lists answers a power query
  6. discovery  - (once) a discovery broadcast is answered, and by whom (O-DISCOVERY)

Nothing here changes anything: every command is a query. `--dump DIR` also
writes each receiver's raw replies (NRI XML, per-zone answers) to
DIR/<receiver>.json, with addresses and MACs redacted, for comparing with
the simulator or attaching to an issue.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import time
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from . import eiscp
from .assumptions import unverified
from .codes import ZONE_CODES
from .config import ReceiverSettings, Settings
from .logsafe import redact
from .receivers import Layout, parse_nri

# HARDWARE_VALIDATION.md step that covers each layer, for the hints.
STEP = {"tcp": 2, "eiscp": 2, "describe": 3, "zones": 4, "discovery": 1}


@dataclass
class Check:
    step: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class ReceiverReport:
    host: str
    port: int
    name: str | None = None
    model: str | None = None
    checks: list[Check] = field(default_factory=list)
    zones: dict[str, str] = field(default_factory=dict)  # zone -> "on" / "standby" / "no answer"
    services: list[str] = field(default_factory=list)
    dumped_to: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


@dataclass
class Report:
    versions: dict[str, str]
    config_problems: list[str]
    dry_run: bool
    receivers: list[ReceiverReport]
    discovery: list[str]
    warnings: list[str]
    unverified_assumptions: list[str]

    @property
    def ok(self) -> bool:
        return not self.config_problems and bool(self.receivers) and all(r.ok for r in self.receivers)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-onkyo", "mcp", "pydantic", "uvicorn", "pyjwt"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _hint(layer: str, text: str) -> str:
    return f"{text} (HARDWARE_VALIDATION.md step {STEP[layer]})"


async def check_receiver(r: ReceiverSettings, settings: Settings, timeout: float, dump: Path | None) -> ReceiverReport:
    report = ReceiverReport(r.host, r.port, r.name)
    raw: dict[str, Any] = {"host": r.host, "port": r.port}

    # 2. TCP: is anything listening?
    t0 = time.perf_counter()
    try:
        conn = await eiscp.Connection.open(r.host, r.port, timeout)
    except OSError as exc:
        report.checks.append(
            Check(
                "tcp",
                False,
                f"no connection to {r.host}:{r.port} ({exc})",
                _hint("tcp", "Is the receiver on, on the network, and at this address? Network Standby must be on."),
            )
        )
        return report
    report.checks.append(Check("tcp", True, f"connected in {1000 * (time.perf_counter() - t0):.0f} ms"))

    async with conn:
        # 3. eISCP: does it speak the protocol?
        t0 = time.perf_counter()
        try:
            power = await conn.request("PWRQSTN", "PWR", timeout)
        except (TimeoutError, ValueError, asyncio.IncompleteReadError, OSError) as exc:
            report.checks.append(
                Check(
                    "eiscp",
                    False,
                    f"connected, but no answer to PWRQSTN ({type(exc).__name__})",
                    _hint("eiscp", "Something else may be using this port, or the receiver is still booting."),
                )
            )
            return report
        took = 1000 * (time.perf_counter() - t0)
        state = {"01": "on", "00": "standby"}.get(power, power)
        report.checks.append(Check("eiscp", True, f"main zone {state}; answered in {took:.0f} ms"))

        # 4. Self-description
        try:
            xml = await conn.request("NRIQSTN", "NRI", 3 * timeout)
        except (TimeoutError, ValueError, asyncio.IncompleteReadError, OSError) as exc:
            xml = ""
            report.checks.append(Check("describe", False, f"no NRI reply ({type(exc).__name__})"))
        raw["nri"] = xml
        layout: Layout | None = parse_nri(xml) if xml else None
        if xml and layout is None:
            report.checks.append(
                Check(
                    "describe",
                    True,
                    "no self-description (NRI answered N/A): an older model; zones are probed directly",
                )
            )
        elif layout is not None:
            report.model = layout.model
            report.services = sorted(layout.net_services.values())
            zones = ", ".join(
                f"{z}{'' if i.volume else ' (no volume control)'}" for z, i in layout.zones.items() if i.present
            )
            report.checks.append(
                Check("describe", True, f"{layout.model}; zones: {zones}; {len(report.services)} network services")
            )

        # 5. Each zone answers its power query
        present = [z for z, i in layout.zones.items() if i.present] if layout else ["main", "zone2", "zone3"]
        raw["zones"] = {}
        for zone in present:
            code = ZONE_CODES[zone]["power"]
            try:
                reply = await conn.request(f"{code}QSTN", code, timeout)
            except (TimeoutError, ValueError, asyncio.IncompleteReadError, OSError):
                reply = None
            raw["zones"][zone] = reply
            report.zones[zone] = {"01": "on", "00": "standby", None: "no answer"}.get(reply, str(reply))
        silent = [z for z, s in report.zones.items() if s == "no answer" and (layout is not None or z == "main")]
        report.checks.append(
            Check(
                "zones",
                not silent,
                ", ".join(f"{z} {s}" for z, s in report.zones.items()),
                _hint("zones", f"{', '.join(silent)} listed but silent") if silent else "",
            )
        )

    if dump is not None:
        dump.mkdir(parents=True, exist_ok=True)
        path = dump / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', r.name or r.host)}.json"
        path.write_text(redact(json.dumps(raw, indent=2, ensure_ascii=False)) + "\n")
        report.dumped_to = str(path)
    return report


async def run_doctor(settings: Settings, timeout: float = 5.0, dump: Path | None = None) -> Report:
    warnings: list[str] = []
    if not settings.receivers:
        warnings.append("No receivers configured: the server will use discovery (see below).")
    if os.environ.get("CF_ACCESS_TEAM_DOMAIN") and not os.environ.get("CF_ACCESS_AUD"):
        warnings.append("CF_ACCESS_TEAM_DOMAIN is set but CF_ACCESS_AUD isn't: `serve --http` will refuse to start.")
    reports = list(await asyncio.gather(*(check_receiver(r, settings, timeout, dump) for r in settings.receivers)))
    found = await eiscp.discover(settings.discovery_addr, settings.discovery_port, timeout=min(timeout, 3.0))
    discovery = [f"{f.model} at {f.host} (port {f.port}, MAC {f.mac})" for f in found]
    configured = {(r.host, r.port) for r in settings.receivers}
    for f in found:
        if (f.host, f.port) not in configured and settings.receivers:
            warnings.append(f"Discovery found {f.model} at {f.host}, which isn't in your config.")
    return Report(
        versions=versions(),
        config_problems=list(settings.problems),
        dry_run=settings.dry_run,
        receivers=reports,
        discovery=discovery,
        warnings=warnings,
        unverified_assumptions=[f"{a.id} ({a.confidence}): {a.claim}" for a in unverified()],
    )


def render(report: Report, redacted: bool = True) -> str:
    lines = ["mcp-server-onkyo doctor", ""]
    lines += [f"  {k}: {v}" for k, v in report.versions.items()]
    lines += ["", "1. config"]
    lines += [f"   x {p}" for p in report.config_problems] or ["   ok"]
    if report.dry_run:
        lines.append("   note: dry run is on (ONKYO_DRY_RUN): setters will send nothing")
    for r in report.receivers:
        title = f"{r.name} ({r.host}:{r.port})" if r.name else f"{r.host}:{r.port}"
        lines += ["", f"Receiver {title}" + (f": {r.model}" if r.model else "")]
        for c in r.checks:
            lines.append(f"   {'ok' if c.ok else 'x '} {c.step:<9} {c.detail}")
            if c.hint:
                lines.append(f"      -> {c.hint}")
        if r.dumped_to:
            lines.append(f"   raw replies written to {r.dumped_to}")
    lines += ["", "6. discovery (UDP broadcast)"]
    lines += [f"   {d}" for d in report.discovery] or [
        "   nothing answered: broadcasts may be filtered here (WSL2 NAT, Wi-Fi isolation, VLANs). Configured "
        "receivers still work by address. (HARDWARE_VALIDATION.md step 1)"
    ]
    if report.warnings:
        lines += ["", "Warnings"] + [f"   ! {w}" for w in report.warnings]
    lines += ["", f"Not yet confirmed on hardware ({len(report.unverified_assumptions)}):"]
    lines += [f"   {u}" for u in report.unverified_assumptions]
    lines += ["", "OK" if report.ok else "PROBLEMS FOUND"]
    text = "\n".join(lines)
    return redact(text) if redacted else text


def to_json(report: Report, redacted: bool = True) -> str:
    text = json.dumps({**asdict(report), "ok": report.ok}, indent=2)
    return redact(text) if redacted else text
