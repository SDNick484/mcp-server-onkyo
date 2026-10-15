# safety Specification

## Purpose
Limits the server enforces itself, whatever the model asks for. Tool descriptions and
annotations only inform the model; these rules are what actually protect the
speakers, the user's ears and the user's streaming accounts.

## Requirements

### Requirement: Volume safety cap
The server SHALL clamp every requested volume to the range 0 to `ONKYO_MAX_VOLUME`
(default 75, on the display scale) in every zone, before sending anything to the
receiver, and SHALL say in its reply when it clamped.

#### Scenario: Request above the cap
- **WHEN** set_volume is called with `level=90` and the default cap
- **THEN** the zone is set to 75
- **AND** the reply reads "Volume is now 75.0 (requested 90.0, capped at 75.0)"

#### Scenario: Cap applies to other zones
- **WHEN** set_volume is called with `level=90` and `zone="zone2"`
- **THEN** Zone 2 is set to 75

#### Scenario: Negative request
- **WHEN** set_volume is called with `level=-5`
- **THEN** the zone is set to 0

### Requirement: Never select account items
The server SHALL only select menu items whose icontype marks them as music (`M`) or
the item playing (`0`) when playing, and folders (`F`) when browsing. It MUST NOT
select items such as "Sign Out", "Account Info" or "Create new station".

#### Scenario: Name matches an account item
- **WHEN** play_station is called with `station="sign out"`
- **THEN** the tool fails with no match, because "Sign Out" is not among the playable items

### Requirement: No guessed addresses
The server MUST NOT send commands to an address it wasn't given or didn't discover.
With no `receiver` argument and no `ONKYO_HOST`, it SHALL use discovery or fail.

#### Scenario: Nothing configured
- **WHEN** a tool is called with no `receiver`, `ONKYO_HOST` unset and no receiver answering discovery
- **THEN** no command is sent and the tool fails with setup instructions
