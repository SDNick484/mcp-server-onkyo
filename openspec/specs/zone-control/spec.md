# zone-control Specification

## Purpose
Read and change each zone's power, volume, mute and input. A zone is the main room
(`main`) or speakers in another room (`zone2`, `zone3`); zones are independent of
each other.

## Requirements

### Requirement: Zone selection
The `get_status`, `set_power`, `set_volume`, `set_mute` and `set_input` tools SHALL
take an optional `zone` of `main` (default), `zone2` or `zone3`, and SHALL use that
zone's own eISCP commands (main: PWR/MVL/AMT/SLI, zone 2: ZPW/ZVL/ZMT/SLZ,
zone 3: PW3/VL3/MT3/SL3). Replies about zones other than main SHALL be prefixed
with the zone's name (e.g. "Zone 2: Volume is now 30.0").

#### Scenario: Zone 2 volume
- **WHEN** set_volume is called with `level=30` and `zone="zone2"`
- **THEN** the server sends `ZVL3C` and replies "Zone 2: Volume is now 30.0"

### Requirement: Zone layout check
Before acting on `zone2` or `zone3`, the server SHALL consult the receiver's
self-description (`NRIQSTN`, fetched once per host and port and cached) and fail fast
with a clear error for a zone the receiver reports absent, or, for set_volume, a zone
without volume control (`volmax="0"`). If the receiver gives no usable description,
the server SHALL send the command anyway.

#### Scenario: Absent zone
- **WHEN** set_power is called with `zone="zone3"` on a TX-NR6050 whose zone list marks zone 3 `value="0"`
- **THEN** the tool fails immediately with "The TX-NR6050 at <host> has no Zone 3." and sends no power command

#### Scenario: Zone without volume control
- **WHEN** set_volume targets a present zone whose zone list entry has `volmax="0"`
- **THEN** the tool fails with an error saying the zone has no volume control

#### Scenario: Older receiver without NRI
- **WHEN** the receiver answers `NRIQSTN` with `N/A`
- **THEN** the zone command is sent and its own reply decides the outcome

### Requirement: Zone status
get_status SHALL return the zone's `receiver`, `zone`, `power` (`on` or `standby`),
`volume` on the 0-100 display scale (or null), `muted`, and `input` by name. Input
codes not in the server's table SHALL be reported raw (e.g. `SLI2C`). For the main
zone it SHALL also return `listening_mode` and `other_zones`: the power state of
each other zone the receiver reports present, leaving out zones that don't answer.

#### Scenario: Main zone status
- **WHEN** get_status is called for the main zone of a TX-NR6050 with Zone 2 playing
- **THEN** the result includes `listening_mode` and `"other_zones": {"zone2": "on"}`

#### Scenario: Zone that answers N/A to power
- **WHEN** the zone's power query returns `N/A`
- **THEN** the tool fails saying the receiver doesn't have that zone

### Requirement: Power control
set_power SHALL turn a zone on or put it into standby and SHALL wait up to three
times `ONKYO_TIMEOUT` for confirmation, since some receivers take about 10 seconds
to confirm standby. A power-on reply SHALL tell the model that some receivers need
about 15 seconds before accepting other commands.

#### Scenario: Slow power-on
- **WHEN** set_power `on=true` is confirmed with `PWR01` after 4 seconds (TX-NR7100)
- **THEN** the tool replies that power is on and warns about the ~15 second start-up

### Requirement: Volume control
set_volume SHALL take a volume on the receiver's 0-100 display scale and convert it to
raw hex steps using `ONKYO_VOLUME_STEPS` (default 2, i.e. 0.5 steps), rounding to the
nearest supported step. The reply SHALL state the volume the receiver confirmed.

#### Scenario: Half-step volume
- **WHEN** set_volume is called with `level=40.3` and `ONKYO_VOLUME_STEPS=2`
- **THEN** the server sends `MVL51` and replies with the confirmed volume (40.5)

#### Scenario: Receiver rejects the change
- **WHEN** the receiver answers `N/A`
- **THEN** the tool replies that the change was rejected because the zone may be off or have a fixed volume

### Requirement: Mute control
set_mute SHALL mute or unmute a zone and reply "Muted" or "Unmuted" from the
receiver's confirmation.

#### Scenario: Mute the main zone
- **WHEN** set_mute is called with `muted=true`
- **THEN** the server sends `AMT01` and replies "Muted" once `AMT01` is echoed

### Requirement: Input selection
set_input SHALL select an input by its front-panel name (`bd-dvd`, `game`, `cbl-sat`,
`strm-box`, `pc`, `aux`, `tv`, `phono`, `cd`, `fm`, `am`, `net`, `bluetooth`) and,
for zone2 and zone3 only, `same-as-main`.

#### Scenario: same-as-main on the main zone
- **WHEN** set_input is called with `source="same-as-main"` and `zone="main"`
- **THEN** the tool fails without contacting the receiver

### Requirement: Diagnosing silence
When a receiver connects but doesn't reply in time, the server SHALL report the
likeliest cause instead of a bare timeout: for a zone power command on zone2/zone3,
that the zone doesn't exist or isn't set up; for a setter on a zone whose power query
reports standby, that the zone is in standby and must be turned on with set_power;
otherwise, that the receiver may still be starting up. A connection failure SHALL be
reported separately, with a hint to check the IP address and network.

#### Scenario: Setter sent in standby (TX-NR7100)
- **WHEN** set_volume gets no reply and the follow-up `PWRQSTN` returns `00`
- **THEN** the tool fails with "The receiver at <host> is in standby. Turn it on with set_power first."

#### Scenario: Zone 3 that ignores power-on
- **WHEN** set_power on zone3 gets no reply
- **THEN** the tool fails saying the receiver doesn't have Zone 3 or it isn't set up in its speaker configuration

### Requirement: Matching replies
The server SHALL match a reply by its 3-character command prefix and skip unsolicited
status pushes. For a setter, only a reply echoing the value sent, or `N/A`, SHALL
count as the answer.

#### Scenario: Status push after power-on
- **WHEN** the server sends `AMT01` and the receiver first pushes `AMT00` as part of its power-on status burst
- **THEN** `AMT00` is skipped and the server keeps waiting for `AMT01` or `N/A`
