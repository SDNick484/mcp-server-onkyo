# mcp-interface Specification

## Purpose
How the server presents itself to MCP clients, independent of any one tool:
transport, tool annotations, error reporting and debug logging.

## Requirements

### Requirement: stdio transport
The server SHALL run on the MCP stdio transport by default and MUST NOT write anything
but JSON-RPC to stdout while serving. Diagnostics SHALL go to stderr.

#### Scenario: Debug logging enabled
- **WHEN** the server runs with `ONKYO_DEBUG=1` and handles a tools/call
- **THEN** the log lines appear on stderr and stdout carries only JSON-RPC

### Requirement: Tool annotations
Every tool SHALL declare MCP tool annotations: discover_receivers is read-only and
open-world; get_status and get_now_playing are read-only and closed-world; the
`set_*`, `select_net_service`, `list_stations` and `play_station` tools are
non-destructive, idempotent and closed-world; control_playback is non-destructive,
closed-world and not idempotent.

#### Scenario: Skipping is not idempotent
- **WHEN** a client reads control_playback's annotations
- **THEN** `idempotentHint` is false, since "next" twice skips two tracks

### Requirement: Explanatory tool errors
Failures the model can act on (network errors, rejected commands, missing zones,
signed-out services, ambiguous names) SHALL be returned as tool errors (`isError`)
whose message says what went wrong and what to try, rather than as generic
"Error executing tool" crashes.

#### Scenario: Receiver unreachable
- **WHEN** a tool can't open a TCP connection to the receiver
- **THEN** the result has `isError: true` and a message naming the host and suggesting checking the IP address and network

### Requirement: Traffic debugging
With `ONKYO_DEBUG=1` or `--debug`, the server SHALL log every MCP message (tagged
with its JSON-RPC id) and every eISCP packet sent or received, marking unsolicited
packets it skipped.

#### Scenario: Unsolicited push while waiting
- **WHEN** debug logging is on and a status push arrives while the server waits for `MVL3C`
- **THEN** the push is logged with "(unsolicited, skipped)"

### Requirement: Settings from the environment
Settings SHALL come from `ONKYO_*` environment variables, and an empty variable SHALL
be treated the same as an unset one.

#### Scenario: Empty variable from WSL
- **WHEN** `ONKYO_MAX_VOLUME` is set to the empty string
- **THEN** the default cap of 75 applies and the server starts normally
