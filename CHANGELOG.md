# Changelog

## [Unreleased]

### Added

- `GET /onkyo/mcp/healthz`, the health check under the MCP path (the only one a tunnel routing `/onkyo/mcp` reaches), and both health routes report the installed `commit` and `repo` from `/opt/mcp-server-onkyo/COMMIT`, which `deploy/alpine/install.sh` now writes (pinning the default source to `main`'s current commit first). `remote.py` and the new `deploy/alpine/commit.sh` synced from mcp-server-shared a48209b; the install needs `git`.
- Every tool has an authorization tier (`read`, `power-off`, `everyday` or `costly`). `set_power` is `power-off` for `state="off"` and `everyday` for `on`. None is costly; no tool asks for confirmation (a costly tool would ask the person to confirm, and refuse where the app can't show the prompt).
- `MCP_ALLOWED_SERVICE_TOKENS` and `MCP_AUTOMATION_TIERS` settings.

### Changed

- A Cloudflare Access service token now gets 403 unless its client ID is in `MCP_ALLOWED_SERVICE_TOKENS`, and automated callers get only `MCP_AUTOMATION_TIERS` (default `read,power-off`). Deployments that use a service token must set the variable.
- Synced `remote.py` and `ratelimit.py` (and their tests) to mcp-server-shared `f4719e3`.

## [0.2.0]

First release on PyPI.

### Added

- MCP server for Onkyo AV receivers over eISCP (TCP 60128): power, volume (capped per zone by the server), mute, input, listening mode, network services, stations, playback and now playing, for one or more receivers and their zones, never guessing between them.
- Resources (`onkyo://...`) and prompts, including movie night and all off.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `discover`, `doctor`, `call` and `simulate` (simulated TX-NR6050 and TX-NR7100 receivers, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
