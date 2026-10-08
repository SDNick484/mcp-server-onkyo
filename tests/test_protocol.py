"""Pure functions: packet framing, volume conversion, input tables. No network."""

import struct
from typing import get_args

import pytest

from onkyo_mcp.codes import (
    CODE_MODES,
    CODE_SOURCES,
    MODE_CODES,
    SOURCE_CODES,
    ListeningMode,
    Source,
    raw_to_volume,
    volume_to_raw,
)
from onkyo_mcp.eiscp import build_packet, decode_datagram


def test_build_packet_layout():
    pkt = build_packet("PWR01")
    assert pkt[:4] == b"ISCP"
    header_size, data_size, version = struct.unpack(">IIB", pkt[4:13])
    assert (header_size, version) == (16, 1)
    assert pkt[16:] == b"!1PWR01\r"
    assert data_size == len(b"!1PWR01\r")


def test_build_packet_discovery_unit():
    assert build_packet("ECNQSTN", unit="x").endswith(b"!xECNQSTN\r")


def test_decode_datagram_strips_prefix_and_terminators():
    data = b"!1ECNTX-NR7100/60128/DX/0009B0123456\x1a\r\n"
    pkt = b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data
    assert decode_datagram(pkt) == "ECNTX-NR7100/60128/DX/0009B0123456"


def test_decode_datagram_rejects_bad_magic():
    with pytest.raises(ValueError):
        decode_datagram(b"XXXX" + struct.pack(">IIB3x", 16, 0, 1))


@pytest.mark.parametrize("raw, volume", [("00", 0.0), ("3C", 30.0), ("50", 40.0), ("C8", 100.0)])
def test_volume_half_steps(raw, volume):
    # Default ONKYO_VOLUME_STEPS=2: raw 0x00-0xC8 maps to 0.0-100.0
    assert raw_to_volume(raw, 2) == volume
    assert volume_to_raw(volume, 2) == raw


def test_volume_half_step_rounds_trip():
    assert volume_to_raw(30.5, 2) == "3D"
    assert raw_to_volume("3D", 2) == 30.5


def test_volume_whole_steps():
    # ONKYO_VOLUME_STEPS=1, for older models: raw is the display value
    assert volume_to_raw(40, 1) == "28"
    assert raw_to_volume("28", 1) == 40.0


def test_source_literal_matches_code_table():
    # The Literal (what the model sees) and the dict (what we send) must agree
    assert set(get_args(Source)) == set(SOURCE_CODES)


def test_source_codes_are_unique():
    assert len(CODE_SOURCES) == len(SOURCE_CODES)


def test_listening_mode_literal_matches_code_table():
    assert set(get_args(ListeningMode)) == set(MODE_CODES)


def test_listening_mode_codes_are_unique():
    assert len(CODE_MODES) == len(MODE_CODES)


def test_decode_datagram_utf8():
    data = "!1NTIDéjà Vu\x1a\r\n".encode()
    pkt = b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data
    assert decode_datagram(pkt) == "NTIDéjà Vu"


def test_empty_settings_use_defaults(tmp_path):
    # WSL passes variables listed in WSLENV but unset on Windows as "", which
    # must mean "use the default", not crash (float("")).
    from onkyo_mcp.config import load_settings

    names = ("ONKYO_HOST", "ONKYO_PORT", "ONKYO_MAX_VOLUME", "ONKYO_VOLUME_STEPS", "ONKYO_TIMEOUT", "ONKYO_DEBUG")
    s = load_settings({"ONKYO_CONFIG_DIR": str(tmp_path), **dict.fromkeys(names, ""), "ONKYO_DISCOVERY_ADDR": ""})
    assert (s.receivers, s.discovery_port, s.cap("main"), s.timeout, s.problems) == ((), 60128, 75.0, 5.0, ())


def test_parse_ecn_tolerates_missing_and_bad_fields():
    from onkyo_mcp.eiscp import Found, parse_ecn

    assert parse_ecn("ECNTX-NR6050/60128/DX/0009B0F76CFD", "10.0.0.2") == Found(
        "10.0.0.2", "TX-NR6050", 60128, "DX", "00:09:B0:F7:6C:FD"
    )
    # A truncated reply still parses, with the default port
    assert parse_ecn("ECNTX-NR6050", "10.0.0.2") == Found("10.0.0.2", "TX-NR6050", 60128, "", "")
    assert parse_ecn("ECNTX-NR6050/notaport/DX/", "10.0.0.2").port == 60128
    assert parse_ecn("NLSU0-x", "10.0.0.2") is None  # not a discovery reply


async def _stream(data: bytes):
    import asyncio

    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.anyio
async def test_read_packet_rejects_implausible_header():
    # A garbled stream must fail fast, not wait for (or allocate) 4 GB of "data"
    from onkyo_mcp.eiscp import read_packet

    bad = b"ISCP" + struct.pack(">IIB3x", 16, 0xFFFFFFFF, 1)
    with pytest.raises(ValueError, match="Implausible"):
        await read_packet(await _stream(bad))


@pytest.mark.anyio
async def test_read_packet_rejects_bad_magic():
    from onkyo_mcp.eiscp import read_packet

    with pytest.raises(ValueError, match="Bad magic"):
        await read_packet(await _stream(b"XSCP" + struct.pack(">IIB3x", 16, 2, 1) + b"!1"))


@pytest.mark.parametrize("steps", [1, 2])
@pytest.mark.parametrize("cap", [0.0, 37.3, 50.0, 60.3, 75.0, 100.0])
def test_volume_never_exceeds_the_cap_and_lands_on_the_nearest_step(steps, cap):
    # Every request from 0 to 100 in 0.05 steps
    for i in range(0, 2001):
        level = i / 20
        shown = raw_to_volume(volume_to_raw(level, steps, cap), steps)
        assert 0 <= shown <= cap, (level, cap)
        if level <= cap - 1 / steps:  # far enough below the cap that clamping can't apply
            assert abs(shown - level) <= 0.5 / steps + 1e-9, (level, shown)
