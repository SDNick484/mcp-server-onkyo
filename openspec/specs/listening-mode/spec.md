# listening-mode Specification

## Purpose
Set and report the main zone's listening mode (surround processing). Zones 2 and 3
have no surround processing.

## Requirements

### Requirement: Set listening mode
set_listening_mode SHALL set the main zone's mode from a fixed list of 2021-model
names: `stereo`, `direct`, `pure-audio`, `all-ch-stereo`, `full-mono`,
`theater-dimensional`, `dolby-surround`, `dts-neural-x`, `game-rpg`, `game-action`,
`game-rock`, `game-sports`. It SHALL NOT take a `zone` argument.

#### Scenario: Mode accepted
- **WHEN** set_listening_mode is called with `mode="dolby-surround"`
- **THEN** the server sends `LMD80` and replies "Listening mode is now dolby-surround"

#### Scenario: Mode rejected for the current signal
- **WHEN** the receiver answers `N/A`
- **THEN** the tool replies that the mode was rejected, because the receiver is off or the mode doesn't suit the signal

### Requirement: Report listening mode
get_status for the main zone SHALL include `listening_mode` by name, reporting codes
not in the server's table raw (e.g. `LMD42`).

#### Scenario: Unknown mode code
- **WHEN** the receiver reports `LMD42`
- **THEN** get_status returns `"listening_mode": "LMD42"`
