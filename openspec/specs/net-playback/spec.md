# net-playback Specification

## Purpose
Control the receiver's network player: choose a streaming service, browse its menus,
play stations and tracks by name, and report what's playing. A receiver has one
network player, shared by every zone whose input is `net`.

## Requirements

### Requirement: Select a network service
select_net_service SHALL switch the network player to one of `pandora`, `spotify`,
`deezer`, `tidal`, `amazon-music`, `airplay`, `tunein` or `music-server`, using the
service codes the receivers report in their own description (AirPlay 44, TIDAL 1B,
Amazon Music 1C). Because `NSV` has no echo, the server SHALL treat the receiver's
pushed `NLT<service code>...` menu title as the confirmation.

#### Scenario: Service opens
- **WHEN** select_net_service is called with `service="pandora"` and the receiver pushes `NLT0401000000480100FF0400Pandora`
- **THEN** the tool replies "Network service is now Pandora"

#### Scenario: Service not signed in
- **WHEN** the pushed title is a popup or keyboard screen (UI type 3 or 4), e.g. "TIDAL Login"
- **THEN** the tool fails saying the service isn't ready, quoting the receiver's screen, and suggesting signing in from the Onkyo Controller app

#### Scenario: Service not offered
- **WHEN** no matching menu title arrives in time
- **THEN** the tool fails saying the receiver may not offer the service or it isn't signed in

### Requirement: Now playing
get_now_playing SHALL return the playing `service`, `station`, `menu` on screen,
play `state`, `title`, `artist`, `album` and `position`, using null for empty fields.
The menu and the playing service MAY differ, since browsing doesn't stop playback.

#### Scenario: Browsing while Pandora plays
- **WHEN** Pandora is playing and the screen has gone back to the top menu
- **THEN** `service` is `pandora` and `menu` names the top menu

### Requirement: Browse a service
list_stations SHALL open a service's top menu, then each name in `folder` in turn,
and return the `playable` items (icontype `M`, or `0` for the item playing) and the
`folders` (icontype `F`) at that level, each de-duplicated and capped at 300 names
with a `truncated` note. If a level has neither, it SHALL return the receiver's
message items (e.g. "No Favorites available") instead. Menus SHALL be read in pages
of 100 items.

#### Scenario: Pandora stations
- **WHEN** list_stations is called with `service="pandora"` and no folder
- **THEN** it returns the account's stations as `playable`

#### Scenario: Music server albums
- **WHEN** list_stations is called with `service="music-server"` and `folder=["MiniDLNA", "Music", "Album"]` on a server holding 700 albums
- **THEN** each folder is opened in turn, the menu is read in 7 pages, and the first 300 album names are returned with a `truncated` note

### Requirement: Opening folders
When opening a folder, the server SHALL wait for the menu title whose layer number is
one deeper than the current menu, since the previous menu's title keeps arriving
with the same prefix, and SHALL allow three times `ONKYO_TIMEOUT` for it.

#### Scenario: Old menu title arrives first
- **WHEN** the server opens a layer-01 folder and the receiver pushes another layer-01 title before the layer-02 one
- **THEN** the layer-01 title is skipped

### Requirement: Matching names
Station and folder names SHALL match an exact title ignoring case, or else the only
title containing the given text. Titles listed twice count once. No match SHALL fail
with the available names (up to 30); several matches SHALL fail listing them (up to 10).

#### Scenario: Partial name
- **WHEN** play_station is called with `station="pearl jam"` and only "Pearl Jam Radio" contains it
- **THEN** "Pearl Jam Radio" is played

#### Scenario: Ambiguous name
- **WHEN** `station="radio"` matches "Pearl Jam Radio" and "Foo Fighters Radio"
- **THEN** the tool fails listing both and asking which one

### Requirement: Play a station
play_station SHALL select a playable item by name in the given service and `folder`,
and SHALL confirm it started by waiting for the player to report "playing" (`NSTP`),
for up to three times `ONKYO_TIMEOUT`.

#### Scenario: Nothing playable at this level
- **WHEN** play_station is called at a TuneIn top menu that holds only folders
- **THEN** the tool fails naming the folders and suggesting adding one to `folder`

### Requirement: Playback control
control_playback SHALL support `play`, `pause`, `stop`, `next` and `previous`.
Play, pause and stop SHALL be confirmed by the player's state (P, p, S). Next and
previous have no state change, so the server SHALL instead poll the track title
until it changes, failing if it doesn't within `ONKYO_TIMEOUT`.

#### Scenario: Skip limit reached
- **WHEN** control_playback `next` is sent and the title never changes (Pandora's hourly skip limit)
- **THEN** the tool fails saying the service may not allow that right now

### Requirement: Browsing is not read-only
list_stations SHALL be annotated as a state-changing tool, not read-only, because
opening a different service stops what's playing; its description SHALL tell the model
to check get_now_playing and ask before browsing another service while something plays.

#### Scenario: Tool annotations
- **WHEN** a client calls `tools/list`
- **THEN** list_stations has `readOnlyHint: false`
