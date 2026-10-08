"""Every protocol detail this server relies on, and whether hardware has confirmed it.

Why a registry instead of comments: a comment saying "unverified" is easy to
miss and never goes away. Here each claim has an id that:
  - code cites next to where it depends on it (``# ASSUMPTION O-NRI-SERVICES``),
  - a test cites to document it,
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - the README's verification table lists, with its status,
  - ``mcp-server-onkyo doctor`` prints.

tests/test_assumptions.py fails if they drift apart.

Unlike the sibling servers, much of Onkyo's protocol *has* been seen on real
hardware (a TX-NR6050 and a TX-NR7100, 2026-10): those claims are recorded
as "hardware-verified", with what was seen in ``note``. The rest are what
the simulator implements and nothing has confirmed.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it.

Confidence is about the *claim*, judged from its source:
  high   - seen on hardware, or relied on by mature projects (onkyo-eiscp, Home Assistant)
  medium - from the onkyo-eiscp command tables or another project's parser, not seen here
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    Assumption(
        "O-FRAMING",
        "eISCP runs on TCP and UDP 60128; each message is a 16-byte ISCP header plus '!1' + command + CR.",
        "onkyo-eiscp; used against both receivers",
        "high",
        "hardware-verified",
        "TX-NR6050 and TX-NR7100, 2026-10",
    ),
    Assumption(
        "O-PUSHES",
        "Receivers push unsolicited status messages, including same-prefix ones (AMT00 right after power-on), "
        "so replies are matched by prefix and, for setters, by the echoed value.",
        "observed on a TX-NR7100",
        "high",
        "hardware-verified",
        "TX-NR7100 pushes a status burst after PWR01",
    ),
    Assumption(
        "O-STANDBY-SILENT",
        "A zone in standby answers queries but ignores setters without replying (not even N/A).",
        "observed on a TX-NR7100",
        "high",
        "hardware-verified",
        "TX-NR7100; the TX-NR6050 answers N/A to volume/mute in standby",
    ),
    Assumption(
        "O-MULTI-COMMAND",
        "One TCP connection carries several commands in a row, each reply arriving (amid pushes) before the next "
        "command is sent. A tool call now uses one connection for all its commands instead of one per command.",
        "onkyo-eiscp and Home Assistant keep one connection open for everything",
        "high",
    ),
    Assumption(
        "O-CONNECTIONS",
        "How many simultaneous eISCP connections a receiver accepts is unknown; the server never opens more than "
        "one per receiver (a lock queues tool calls).",
        "none: the limit is unknown, so the server avoids depending on it",
        "low",
    ),
    Assumption(
        "O-DISCOVERY",
        "A '!xECNQSTN' UDP broadcast to 60128 gets 'ECN<model>/<port>/<region>/<mac>' from each receiver, from its IP.",
        "onkyo-eiscp; seen from the owner's Linux laptop",
        "high",
        "hardware-verified",
        "both receivers answered the 255.255.255.255 broadcast on Wi-Fi (not from WSL2 NAT)",
    ),
    Assumption(
        "O-VOLUME-STEPS",
        "2021+ models step volume by 0.5: raw MVL/ZVL 0x00-0xC8 is 0.0-100.0 on the front panel.",
        "observed: MVL60 read back as 48.0 on a TX-NR6050",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "O-ZONE2-CODES",
        "Zone 2 is ZPW / ZVL / ZMT / SLZ, with the main zone's value formats.",
        "onkyo-eiscp tables; used on a TX-NR6050",
        "high",
        "hardware-verified",
        "TX-NR6050 Zone 2 power, input and volume",
    ),
    Assumption(
        "O-ZONE3-CODES",
        "Zone 3 is PW3 / VL3 / MT3 / SL3, with the main zone's value formats.",
        "onkyo-eiscp tables; neither owner receiver has a working Zone 3",
        "medium",
    ),
    Assumption(
        "O-NRI-ZONES",
        "NRIQSTN's <zonelist> marks each zone value='1' if present and volmax='0' if it has no volume control.",
        "seen in a TX-NR6050's NRI; the TX-NR7100 with Zone 2 used for heights is unconfirmed",
        "high",
        "hardware-verified",
        "TX-NR6050 (Zone 2 present, Zone 3 value=0). Whether the TX-NR7100 reports its height-channel Zone 2 "
        "as volmax=0 is not known; the simulator assumes it does.",
    ),
    Assumption(
        "O-NRI-SERVICES",
        "NRIQSTN's <netservicelist> has one <netservice id='<hex>' value='1' name='...'/> per offered service.",
        "the codes were seen in the receivers' NRI (AirPlay 44, TIDAL 1b, Amazon 1c); the element and attribute "
        "layout comes from other projects' parsers",
        "medium",
    ),
    Assumption(
        "O-NSV-CODES",
        "NSV service codes: pandora 04, spotify 0A, deezer 12, tidal 1B, amazon-music 1C, airplay 44, tunein 0E, "
        "music-server 00. NSV has no echo; the receiver confirms with a pushed NLT<code>...<name>.",
        "the receivers' own NRI (onkyo-eiscp's tables are wrong for TIDAL and AirPlay)",
        "high",
        "hardware-verified",
        "all eight opened on a TX-NR6050",
    ),
    Assumption(
        "O-NET-SHARED",
        "A receiver has one network player: every zone whose input is 'net' plays it, and NSV/NLS/NTC can't target "
        "a zone.",
        "the owner's report; consistent with NSV/NLS/NTC taking no zone",
        "high",
    ),
    Assumption(
        "O-PLAY-STATE",
        "NST's first character is the play state (P playing, p paused, S stopped); NMS ends with the playing "
        "service's two-character icon code.",
        "observed on a TX-NR6050 while Pandora and the music server played",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "O-MENU-LISTS",
        "NLT<code>01... gives a menu's item count and layer; NLAL<seq><layer><start><count> returns the items as "
        "XML (reply prefix NLAX); NLSI<5-digit position> opens a folder (NLT one layer deeper) or plays an item "
        "(NSTP).",
        "observed on a TX-NR6050 (Pandora stations, TuneIn presets, a MiniDLNA library)",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "O-SOURCE-CODES",
        "Input codes: bd-dvd 10, game 02, cbl-sat 01, strm-box 11, pc 05, aux 03, tv 12, phono 22, cd 23, fm 24, "
        "am 25, net 2B, bluetooth 2E, same-as-main 80 (zones 2/3).",
        "onkyo-eiscp tables; bd-dvd and net seen on a TX-NR6050",
        "medium",
    ),
    Assumption(
        "O-LMD-CODES",
        "Listening-mode codes as named for 2021 models: 80 = Dolby Surround, 82 = DTS Neural:X, 03/05/06/0E = "
        "the game modes (older models give the same codes other names).",
        "onkyo-eiscp tables; all-ch-stereo (0C) seen on a TX-NR6050",
        "medium",
    ),
)

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    return [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
