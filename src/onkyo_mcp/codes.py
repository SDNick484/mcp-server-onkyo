"""eISCP command codes and the names the tools use for them. Pure data, no I/O.

Each table pairs a `Literal` (what the model sees: it becomes a JSON Schema
enum, so the model can only pick one of these names) with a dict of codes
(what is sent). tests/test_protocol.py checks the two stay in sync.

Codes marked ``# ASSUMPTION <id>`` are not confirmed on hardware; see
assumptions.py.
"""

from __future__ import annotations

from typing import Literal

# Input selector (SLI) codes, named after the TX-NR7100/6050 front-panel labels.
# The Literal type becomes a JSON Schema "enum", so the model can only pick
# one of these names. Keep the two in sync.
Source = Literal[
    "bd-dvd",
    "game",
    "cbl-sat",
    "strm-box",
    "pc",
    "aux",
    "tv",
    "phono",
    "cd",
    "fm",
    "am",
    "net",
    "bluetooth",
    "same-as-main",
]
SOURCE_CODES: dict[str, str] = {
    "bd-dvd": "10",
    "game": "02",
    "cbl-sat": "01",
    "strm-box": "11",
    "pc": "05",
    "aux": "03",
    "tv": "12",
    "phono": "22",
    "cd": "23",
    "fm": "24",
    "am": "25",
    "net": "2B",
    "bluetooth": "2E",
    "same-as-main": "80",  # zones 2/3 only: play whatever the main zone plays
}
# Reverse lookup, for turning the receiver's replies back into names
CODE_SOURCES = {code: name for name, code in SOURCE_CODES.items()}

# Listening mode (LMD) codes. Several codes have older and newer meanings in
# onkyo-eiscp's table (80 = PLII Movie / Dolby Surround, 82 = Neo:6 Cinema /
# DTS Neural:X, 03 = Film / Game-RPG); these names are the 2021-model ones.
ListeningMode = Literal[
    "stereo",
    "direct",
    "pure-audio",
    "all-ch-stereo",
    "full-mono",
    "theater-dimensional",
    "dolby-surround",
    "dts-neural-x",
    "game-rpg",
    "game-action",
    "game-rock",
    "game-sports",
]
MODE_CODES: dict[str, str] = {
    "stereo": "00",
    "direct": "01",
    "pure-audio": "11",
    "all-ch-stereo": "0C",
    "full-mono": "13",
    "theater-dimensional": "0D",
    "dolby-surround": "80",
    "dts-neural-x": "82",
    "game-rpg": "03",
    "game-action": "05",
    "game-rock": "06",
    "game-sports": "0E",
}
CODE_MODES = {code: name for name, code in MODE_CODES.items()}

# Network services (NSV codes): the services the Onkyo Controller app offers
# for a TX-NR6050/7100, plus TuneIn and the music server (DLNA), which both
# receivers list though the app doesn't show them. Codes as the receivers
# list them in their own description (NRIQSTN, <netservicelist>);
# onkyo-eiscp's tables have TIDAL as 19, AirPlay as 18 and no Amazon Music.
# All verified on a TX-NR6050. Most must be signed in on the receiver first.
NetService = Literal["pandora", "spotify", "deezer", "tidal", "amazon-music", "airplay", "tunein", "music-server"]
NET_SERVICE_CODES: dict[str, str] = {
    "pandora": "04",
    "spotify": "0A",
    "deezer": "12",
    "tidal": "1B",
    "amazon-music": "1C",
    "airplay": "44",
    "tunein": "0E",
    "music-server": "00",
}
CODE_NET_SERVICES = {code: name for name, code in NET_SERVICE_CODES.items()}
# Other sources the network player can be playing (NMS service icons; AirPlay
# shows as 18 there even though it is selected as 44)
CODE_NET_SERVICES |= {"18": "airplay", "F0": "usb", "F1": "usb", "F4": "bluetooth"}
# NST play state: first character of the reply ("Pxx1" = playing)
PLAY_STATES = {"P": "playing", "p": "paused", "S": "stopped", "F": "fast-forward", "R": "rewind", "E": "end"}

# Zones 2 and 3 drive speakers in other rooms. Each zone has its own power,
# volume, mute and input, with its own 3-letter command for each. Volumes and
# input codes use the same scale and table as the main zone.
Zone = Literal["main", "zone2", "zone3"]
ZONE_CODES: dict[str, dict[str, str]] = {
    "main": {"power": "PWR", "volume": "MVL", "mute": "AMT", "input": "SLI"},
    "zone2": {"power": "ZPW", "volume": "ZVL", "mute": "ZMT", "input": "SLZ"},
    "zone3": {"power": "PW3", "volume": "VL3", "mute": "MT3", "input": "SL3"},
}
ZONE_LABELS = {"main": "Main zone", "zone2": "Zone 2", "zone3": "Zone 3"}


# --- volume -------------------------------------------------------------------
# Volumes are hex raw steps. With 2 steps per display unit (2021+ models),
# raw 0x00-0xC8 is 0.0-100.0 on the front panel: "50" -> 80 raw -> 40.0.
def raw_to_volume(raw: str, steps: int) -> float:
    return int(raw, 16) / steps


def volume_to_raw(volume: float, steps: int) -> str:
    # 40.0 -> 80 raw steps -> "50". round() snaps e.g. 40.3 to the nearest
    # step the receiver supports; :02X is the two-digit uppercase hex it expects.
    return f"{round(volume * steps):02X}"
