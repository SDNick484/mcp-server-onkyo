# Changelog

## [Unreleased]

## [0.2.0]

First release on PyPI.

### Added

- MCP server for Onkyo AV receivers over eISCP (TCP 60128): power, volume (capped per zone by the server), mute, input, listening mode, network services, stations, playback and now playing, for one or more receivers and their zones, never guessing between them.
- Resources (`onkyo://...`) and prompts, including movie night and all off.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `discover`, `doctor`, `call` and `simulate` (simulated TX-NR6050 and TX-NR7100 receivers, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
