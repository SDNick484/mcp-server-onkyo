# discovery Specification

## Purpose
Find Onkyo/Integra/Pioneer receivers on the LAN and decide which receiver a tool
call talks to when the model doesn't name one.

## Requirements

### Requirement: Discovery query
The server SHALL discover receivers by sending the eISCP query `!xECNQSTN` over UDP
to `ONKYO_DISCOVERY_ADDR` (default `255.255.255.255`) on `ONKYO_PORT`, collecting
replies for a fixed window, and returning one entry per replying IP address with its
`host`, `model`, `port`, `region` and `mac`, sorted by model.

#### Scenario: One receiver answers
- **WHEN** a TX-NR6050 at 192.168.1.50 replies `ECNTX-NR6050/60128/DX/0009B0123456`
- **THEN** discover_receivers returns `{"host": "192.168.1.50", "model": "TX-NR6050", "port": 60128, "region": "DX", "mac": "00:09:B0:12:34:56"}`

#### Scenario: A receiver answers twice
- **WHEN** the same IP sends two ECN replies within the window
- **THEN** it is listed once

#### Scenario: Another device answers on the port
- **WHEN** a UDP packet that is not valid eISCP, or not an ECN reply, arrives
- **THEN** it is ignored and discovery continues

#### Scenario: Reply with missing fields
- **WHEN** an ECN reply lacks the port field
- **THEN** the entry's port defaults to 60128

#### Scenario: Nothing answers
- **WHEN** no receiver replies within the window (e.g. broadcasts are filtered)
- **THEN** discover_receivers returns an empty list rather than an error

### Requirement: Default receiver resolution
Every tool except discover_receivers SHALL take an optional `receiver` IP address and
SHALL resolve the receiver to use in this order: the `receiver` argument, then
`ONKYO_HOST`, then the single receiver that answers discovery. The server MUST NOT
fall back to a made-up default address.

#### Scenario: Receiver named by the model
- **WHEN** a tool is called with `receiver="192.168.1.60"` and `ONKYO_HOST` is set to another address
- **THEN** the command goes to 192.168.1.60

#### Scenario: Exactly one receiver found
- **WHEN** neither `receiver` nor `ONKYO_HOST` is given and one receiver answers discovery
- **THEN** that receiver is used, and remembered for later calls without rediscovering

#### Scenario: Several receivers found
- **WHEN** neither `receiver` nor `ONKYO_HOST` is given and several receivers answer
- **THEN** the tool fails with an error listing each model and IP and asking the model to pass one as `receiver`

#### Scenario: No receiver found
- **WHEN** neither `receiver` nor `ONKYO_HOST` is given and nothing answers discovery
- **THEN** the tool fails with an error telling the user to set `ONKYO_HOST` and pointing to the README's "When discovery finds nothing" section

### Requirement: Command-line discovery
Running `mcp-server-onkyo --discover` SHALL run discovery without starting the MCP
server and print one line per receiver (IP, model, MAC, port) to stdout.

#### Scenario: Nothing found from the CLI
- **WHEN** `--discover` finds no receivers
- **THEN** it prints nothing to stdout and a pointer to the README's troubleshooting section to stderr
