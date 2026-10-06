"""Pure functions: packet framing, volume conversion, input tables. No network."""
import struct
from typing import get_args

import pytest

import onkyo_mcp
from onkyo_mcp import (CODE_MODES, CODE_SOURCES, MODE_CODES, SOURCE_CODES, ListeningMode, Source,
                       build_packet, decode_datagram, raw_to_volume, volume_to_raw)


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
    assert raw_to_volume(raw) == volume
    assert volume_to_raw(volume) == raw


def test_volume_half_step_rounds_trip():
    assert volume_to_raw(30.5) == "3D"
    assert raw_to_volume("3D") == 30.5


def test_volume_whole_steps(monkeypatch):
    monkeypatch.setattr(onkyo_mcp, "VOLUME_STEPS", 1)
    assert volume_to_raw(40) == "28"
    assert raw_to_volume("28") == 40.0


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
    data = "!1NTIDéjà Vu\x1a\r\n".encode("utf-8")
    pkt = b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data
    assert decode_datagram(pkt) == "NTIDéjà Vu"
