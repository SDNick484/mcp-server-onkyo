## MODIFIED Requirements

### Requirement: Discovery query
The server SHALL discover receivers by sending the eISCP query `!xECNQSTN` over UDP
on `ONKYO_PORT` to every address in `ONKYO_DISCOVERY_ADDR`, a comma-separated list
of broadcast addresses and/or receiver IP addresses (default `255.255.255.255`),
collecting replies for one shared window. It SHALL return one entry per replying IP
address with its `host`, `model`, `port`, `region` and `mac`, sorted by model. An
entry that isn't an IP address, or that the query can't be sent to, SHALL be skipped with a warning on stderr, and
discovery SHALL continue with the remaining addresses.

#### Scenario: One receiver answers
- **WHEN** a TX-NR6050 at 192.168.1.50 replies `ECNTX-NR6050/60128/DX/0009B0123456`
- **THEN** discover_receivers returns `{"host": "192.168.1.50", "model": "TX-NR6050", "port": 60128, "region": "DX", "mac": "00:09:B0:12:34:56"}`

#### Scenario: Broadcast filtered, receivers listed by IP
- **WHEN** `ONKYO_DISCOVERY_ADDR=192.168.1.50,192.168.1.51` and broadcasts don't reach the LAN (e.g. WSL2 with NAT networking)
- **THEN** discover_receivers returns both receivers within a single discovery window

#### Scenario: Broadcast plus a receiver on another subnet
- **WHEN** `ONKYO_DISCOVERY_ADDR=255.255.255.255,10.0.20.15`
- **THEN** receivers answering the broadcast and the receiver at 10.0.20.15 are all returned

#### Scenario: A receiver answers twice
- **WHEN** the same IP replies to both the broadcast and its own address, or sends two ECN replies within the window
- **THEN** it is listed once

#### Scenario: Unusable address in the list
- **WHEN** `ONKYO_DISCOVERY_ADDR=receiver.invalid,192.168.1.50`
- **THEN** a warning naming `receiver.invalid` is logged to stderr
- **AND** the receiver at 192.168.1.50 is still returned

#### Scenario: Spaces and empty entries
- **WHEN** `ONKYO_DISCOVERY_ADDR=" 192.168.1.50 , ,192.168.1.51"`
- **THEN** the query goes to 192.168.1.50 and 192.168.1.51 only

#### Scenario: Single address, as before
- **WHEN** `ONKYO_DISCOVERY_ADDR=192.168.1.255`
- **THEN** discovery behaves exactly as it did with one address

#### Scenario: Another device answers on the port
- **WHEN** a UDP packet that is not valid eISCP, or not an ECN reply, arrives
- **THEN** it is ignored and discovery continues

#### Scenario: Reply with missing fields
- **WHEN** an ECN reply lacks the port field
- **THEN** the entry's port defaults to 60128

#### Scenario: Nothing answers
- **WHEN** no receiver replies within the window
- **THEN** discover_receivers returns an empty list rather than an error
