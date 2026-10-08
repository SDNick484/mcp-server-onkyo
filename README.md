# mcp-server-onkyo

[![CI](https://github.com/SDNick484/mcp-server-onkyo/actions/workflows/ci.yml/badge.svg)](https://github.com/SDNick484/mcp-server-onkyo/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server for Onkyo AV receivers, so Claude Code, Claude
Desktop, the Claude mobile app (through a connector) or any other MCP client can turn zones on,
set volumes, pick inputs and listening modes, and play network audio. It talks **eISCP** (the
Integra Serial Control Protocol over Ethernet, TCP/UDP 60128) directly: no cloud, no pairing.

It's one of four sibling servers with the same conventions (multi-device by name, dry run,
`doctor`, Streamable HTTP behind Cloudflare Access):
[mcp-server-harmony](https://github.com/SDNick484/mcp-server-harmony),
[mcp-server-sofabaton](https://github.com/SDNick484/mcp-server-sofabaton) and
[mcp-server-shieldtv](https://github.com/SDNick484/mcp-server-shieldtv).

> **Status.** The core protocol (power, volume, inputs, Zone 2, network services, stations,
> folders) was used against a TX-NR6050 and a TX-NR7100 in 2026-10. The features added since
> (several receivers by name, per-zone caps, resources and prompts, dry run, `doctor`, HTTP,
> the Alpine service) are **verified against the simulator only**. [Verification
> status](#verification-status) lists every protocol assumption and whether hardware has
> confirmed it; [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) is the checklist that will.
> This is also a learning project for MCP, so the code is commented for that.

## Tools

| Tool | Read-only | What it does |
| --- | --- | --- |
| `get_status` | yes | Every receiver and zone: power, volume (0-100), the zone's volume cap, mute, input; the main zone's listening mode; which zones are on the network player |
| `set_power` | | Turn a zone on, or put it in standby |
| `set_volume` | | Set a zone's volume (0.5 steps on newer models), lowered to the zone's cap if higher |
| `set_mute` | | Mute or unmute a zone |
| `set_input` | | A zone's input: `bd-dvd`, `game`, `cbl-sat`, `strm-box`, `pc`, `aux`, `tv`, `phono`, `cd`, `fm`, `am`, `net`, `bluetooth`; zones 2/3 also `same-as-main` |
| `set_listening_mode` | | The main zone's mode: `stereo`, `direct`, `pure-audio`, `all-ch-stereo`, `full-mono`, `theater-dimensional`, `dolby-surround`, `dts-neural-x`, `game-rpg`, `game-action`, `game-rock`, `game-sports` |
| `select_net_service` | | Switch the network player to `pandora`, `spotify`, `deezer`, `tidal`, `amazon-music`, `airplay`, `tunein` or `music-server` |
| `list_net_services` | yes | The services this receiver lists itself, and which of them the tools can select |
| `get_now_playing` | yes | The network player's service, station, play state, title, artist, album, position |
| `list_stations` | | A service's menu: what can be played, and folders (refuses to open a different service, which would stop the music, unless `interrupt=true`) |
| `play_station` | | Play a station or track by (part of its) name, optionally inside `folder` |
| `control_playback` | | Play, pause, stop, next, previous |
| `discover_receivers` | yes | UDP broadcast: each receiver's address, model, port and MAC |

**Arguments shared by the tools.** `receiver` is a configured name (case, spaces and punctuation
ignored: `family-room` = `Family Room`) or an address. With one receiver configured it can be
left out. With several, a setter without it is **refused, never guessed**: the error names the
receivers so the model can ask you. `get_status` without it covers them all. `zone` is `main`
(the default), `zone2` or `zone3`.

**Results.** Read-only tools return typed JSON (each tool's `outputSchema`). Every setter returns
the same `ActionResult` shape as the sibling servers:

```json
{"receiver": "Family Room", "zone": "zone2", "outcome": "done",
 "detail": "Zone 2: Volume is now 50.0", "sent": ["ZVL64"],
 "warnings": ["Requested 90, capped at 50 (the owner's limit for this zone)."]}
```

`outcome` is `done` or `dry_run`; `sent` is the eISCP commands that change something. Failures
are MCP errors (`isError: true`) with a sentence that says what to do: `Zone 2 of Theater
(192.168.1.245) is in standby. Turn it on with set_power with zone='zone2' first.`

**Annotations.** Read-only tools are marked `readOnlyHint`. Setters are non-destructive and
idempotent (volume 30 twice is volume 30), so clients may retry them; `control_playback` isn't
idempotent ("next" twice skips two tracks). `discover_receivers` is the one open-world tool, since
it lists whatever answers a broadcast. `list_stations` is marked as a setter, not read-only:
browsing moves the receiver's on-screen menu.

### Zones

The server asks each receiver which zones it has (`NRIQSTN`, once, then cached), so a missing
zone or one without volume control gets an immediate answer instead of a timeout. Zones are
independent: Zone 2 can play while the main zone is in standby.

| Model | Zones | Multi-zone outputs |
| --- | --- | --- |
| TX-RZ71, TX-RZ70 | 3 | Main, powered Zone 2 / line out, Zone 3 line out; HDMI Zone 2 |
| TX-RZ61, TX-RZ51, TX-RZ50 | 3 | Main, powered Zone 2 / line out, Zone 3 line out |
| TX-NR7200 | 3 | Main, powered Zone 2, Zone 3 line out |
| TX-NR7100 | 3 | Main, powered Zone 2 / line out, Zone 3 line out; HDMI Zone 2 |
| TX-RZ31, TX-RZ30 | 2 | Main, powered Zone 2 / line out |
| TX-NR6200, TX-NR6100, TX-NR6050, TX-NR5100 | 2 | Main, powered Zone 2 / line out |

What's usable also depends on the speaker setup. A powered Zone 2's amplifier channels can drive
other speakers instead (height channels in a 5.2.4 layout, as on the TX-NR7100 here), and then
that zone has no volume control. Zone 3 codes (`PW3`/`VL3`/`MT3`/`SL3`) come from the onkyo-eiscp
tables and haven't been tried: neither receiver here has a working Zone 3.

### The shared network player

A receiver has **one** network player, heard in every zone whose input is `net`. The commands
that drive it (`NSV`, `NLS`, `NTC`) don't name a zone, so Zone 2 can't play Pandora while the
main zone plays TuneIn. The tools say so: `set_input source=net` warns when another zone is
already on `net`, and `select_net_service` warns when nobody will hear it or several zones will.

Folders: `list_stations` and `play_station` take a `folder` path, e.g. TuneIn's `["My Presets"]`
or a music server's `["MiniDLNA", "Music", "Album", "21"]` (part of each name is enough). Long
menus are read 100 items at a time. A signed-out service opens a sign-in popup instead of its
menu; the tools quote it ("TIDAL Login") and say to sign in with the Onkyo Controller app.

| Service | What happens (TX-NR6050) |
| --- | --- |
| Pandora | Stations listed and played by name |
| TuneIn, Music Server | Browsed through folders; tracks and stations played by name |
| TIDAL, Amazon Music, Deezer | Menu if signed in (and subscribed); otherwise a clear "isn't ready" |
| Spotify, AirPlay | Selected; playback starts from a phone (Spotify Connect, AirPlay) |

## Resources and prompts

Resources are context a client attaches (in Claude Code: `@onkyo:onkyo://receivers`). Reading
one sends nothing but `NRIQSTN`, once per receiver.

| Resource | What it holds |
| --- | --- |
| `onkyo://receivers` | Every configured receiver: model, zones (volume control, cap), network services |
| `onkyo://receivers/{receiver}` | One of them, by name or address (`onkyo://receivers/Family%20Room`) |
| `onkyo://catalog` | The inputs, listening modes, services and zones the tools accept, with each eISCP code and the assumption it rests on |

Prompts are workflows you pick (in Claude Code: `/mcp__onkyo__play_music patio pandora`). Each
walks the model through this server's tools in order and names the traps.

| Prompt | Arguments | Does |
| --- | --- | --- |
| `play_music` | `room`, `service`, `station`, `volume` | Finds the receiver and zone for the room (asks if unclear), turns it on, `net`, service, station |
| `movie_night` | `receiver`, `source`, `listening_mode`, `volume` | The receiver's part: main zone on, the video input (asks rather than guess the wiring), mode, volume |
| `all_off` | `receiver` | Every zone that's on into standby, then confirms |

### Movie night across servers

The servers don't know about each other. That's deliberate: each one works alone, and a client
with several connected composes them. With all four connected, "movie night in the theater"
can be one request:

> Movie night in the theater: start the Harmony activity "Watch Shield", open Plex on the Shield,
> put Theater on strm-box in Dolby Surround at 45, and turn off Zone 2 of the Family Room.

The model then calls, in order (tool names as of these versions):

```
harmony.start_activity   activity="Watch Shield"            # TV and receiver on, inputs switched
shieldtv.launch_app      app="plex"
onkyo.get_status         receiver="Theater"                  # confirm Harmony left it on strm-box
onkyo.set_input          receiver="Theater" source="strm-box"   # only if it didn't
onkyo.set_listening_mode receiver="Theater" mode="dolby-surround"
onkyo.set_volume         receiver="Theater" level=45         # capped server-side if above the cap
onkyo.set_power          receiver="Family Room" zone="zone2" state="off"
```

Two things make this safe to hand to a model: every server refuses to guess a device when there
are several (so "the receiver" can't become the wrong room), and limits like the volume cap are
enforced by the server, not by the prompt. If a Harmony activity already sets the receiver's
input, let it: the Onkyo calls then only confirm and adjust.

## Install

```sh
pip install git+https://github.com/SDNick484/mcp-server-onkyo
```

Python 3.11+. Dependencies: the official `mcp` SDK, `pyjwt[crypto]` and `uvicorn` (only used by
the HTTP transport), `typing_extensions` (typed results on Python 3.11). Every compiled
dependency ships musllinux wheels, so it installs on Alpine without a compiler.

The receiver needs **Network Standby** on (Setup → Hardware → Power Management) to be turned on
over the network.

## First contact

```sh
mcp-server-onkyo discover                 # who answers a broadcast
mcp-server-onkyo doctor --host 192.168.1.147
```

```
Receiver 192.168.1.147: TX-NR6050
   ok tcp       connected in 4 ms
   ok eiscp     main zone on; answered in 98 ms
   ok describe  TX-NR6050; zones: main, zone2; 8 network services
   ok zones     main on, zone2 standby
```

`doctor` checks each layer in turn (TCP, eISCP framing, self-description, each zone) and stops at
the first failure with what it means and which [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md)
step covers it. It only sends queries. `--json` for a machine-readable report, `--dump DIR` to
save each receiver's raw replies (redacted) for an issue or to compare with the simulator.

## Configuration

Receivers go in `config.json`, in `ONKYO_CONFIG_DIR` (default `~/.config/mcp-server-onkyo`):

```json
{
  "receivers": [
    {"host": "192.168.1.147", "name": "Family Room"},
    {"host": "192.168.1.245", "name": "Theater"}
  ],
  "max_volume": {"main": 75, "zone2": 50}
}
```

`name` is optional (a receiver without one is known by its address). `port` and `volume_steps`
can be set per receiver. `max_volume` is a number for every zone, or an object per zone. With no
receivers configured, the server uses the one receiver that answers discovery, and refuses if
several do. A typo never stops the server: each problem is logged at startup and shown by
`doctor`, and only the bad entry is skipped.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONKYO_CONFIG_DIR` | `~/.config/mcp-server-onkyo` | Where `config.json` lives |
| `ONKYO_HOSTS` | *(from `config.json`)* | Comma-separated `host`, `host:port` or `Name=host`; replaces the file's list (keeping its names) |
| `ONKYO_HOST` | none | One receiver (the older form of `ONKYO_HOSTS`) |
| `ONKYO_PORT` | `60128` | Default eISCP port |
| `ONKYO_MAX_VOLUME` | `75` | Volume cap for every zone, on the 0-100 display scale. Enforced by the server |
| `ONKYO_MAX_VOLUME_ZONE2`, `_ZONE3` | as above | Per-zone caps; override the file |
| `ONKYO_VOLUME_STEPS` | `2` | Raw steps per display unit: `2` for 0.5-step models (TX-NR6050, TX-NR7100), `1` for older ones |
| `ONKYO_TIMEOUT` | `5` | Seconds to wait for a reply (power gets 3x; a TX-NR7100 takes ~10 s to confirm standby) |
| `ONKYO_DISCOVERY_ADDR` | `255.255.255.255` | Where the discovery broadcast goes |
| `ONKYO_DRY_RUN` | off | Read the receivers, send nothing that changes anything (`serve --dry-run`) |
| `ONKYO_DEBUG` | off | Log MCP and eISCP traffic to stderr (`--debug`) |
| `ONKYO_LOG_UNREDACTED` | off | Show LAN addresses and MACs in logs (`--no-redact`); secrets stay hidden |

## Use it with an MCP client

Local (stdio): the client starts the server.

```sh
claude mcp add onkyo -- mcp-server-onkyo
```

```json
{ "mcpServers": { "onkyo": { "command": "mcp-server-onkyo" } } }
```

(the second for Claude Desktop's `claude_desktop_config.json`). Then: *"turn on Zone 2 and play
my Pearl Jam station"*.

### HTTP and Cloudflare Access

To use one server from Claude Code, Claude Desktop and the mobile app, run it as a service and
reach it through a Cloudflare Tunnel with Access in front:

```sh
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com CF_ACCESS_AUD=<aud tag> \
  mcp-server-onkyo serve --http --public-host mcp.example.com      # 127.0.0.1:8711/onkyo/mcp
```

| Flag | Variable | Default |
| --- | --- | --- |
| `--bind` | `MCP_HTTP_BIND` | `127.0.0.1` |
| `--port` | `MCP_HTTP_PORT` | `8711` (Shield 8712, Harmony 8713) |
| `--path` | `MCP_HTTP_PATH` | `/onkyo/mcp` |
| `--public-host` | `MCP_PUBLIC_HOSTS` | none: only loopback Host headers pass |
| | `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | none: no Access checks |
| | `MCP_ALLOWED_EMAILS` | anyone Access lets in |

With Access configured, every request needs a valid `Cf-Access-Jwt-Assertion`: signature (the
team's published keys), audience, issuer, expiry, and the email allow-list if set. Anything else
gets a plain 403. The server **refuses to start** on a non-loopback address without Access,
unless `--insecure-no-auth` (trusted LAN testing only). `Host` and `Origin` are checked too
(DNS-rebinding protection), including on `--bind 0.0.0.0`. `GET /healthz` answers without auth,
for monitoring. Details are in `src/onkyo_mcp/remote.py`, which is identical in all four servers.

### As a service on Alpine (Proxmox LXC)

```sh
sh deploy/alpine/install.sh            # as root; or: install.sh /path/to/checkout
vi /etc/mcp-server-onkyo/config.json   # the receivers
vi /etc/mcp-server-onkyo/env           # HTTP and Access settings
rc-update add mcp-server-onkyo default && rc-service mcp-server-onkyo start
```

| Path | Owner, mode | Holds |
| --- | --- | --- |
| `/opt/mcp-server-onkyo/venv` | root, 0755 | The code |
| `/etc/mcp-server-onkyo/` | root:mcp-onkyo, 0750 (files 0640) | `config.json`, `env` |
| `/var/lib/mcp-server-onkyo/` | mcp-onkyo, 0750 | State (none yet) |
| `/var/log/mcp-server-onkyo/server.log` | mcp-onkyo, 0750 | Logs, redacted |
| `/etc/init.d/mcp-server-onkyo` | root, 0755 | OpenRC script (`supervise-daemon`, restarts on exit) |

It runs as the unprivileged `mcp-onkyo` user, which can read its config but not change it.
`/opt/mcp-server-onkyo/run.sh` is exactly what the service runs. Run cloudflared in the same LXC
and keep the default `127.0.0.1` bind. `deploy/alpine/smoke-test.sh` does the whole install in a
`python:3.12-alpine` container against the simulator (CI runs it on every push).

## A session against the simulator

`mcp-server-onkyo simulate --write-config DIR` starts a fake TX-NR6050 ("Family Room") and
TX-NR7100 ("Theater") and writes a `config.json` naming them; `call` runs one tool the way the
model would. This is real output:

```
$ export ONKYO_CONFIG_DIR=/tmp/sim
$ mcp-server-onkyo call set_power state=on
Error executing tool set_power: Several receivers are configured: Family Room (127.0.0.1:35405),
Theater (127.0.0.1:35339). Pass the one you mean as `receiver` (its name or address); call
get_status without one to see them all.

$ mcp-server-onkyo call set_power state=on receiver=family-room zone=zone2
{ "receiver": "Family Room", "zone": "zone2", "outcome": "done",
  "detail": "Zone 2: Power is now on", "sent": ["ZPW01"],
  "warnings": ["Some receivers need about 15 seconds to start up before they accept other commands."] }

$ mcp-server-onkyo call set_input source=net receiver=family-room zone=zone2
{ ..., "detail": "Zone 2: Input is now net", "sent": ["SLZ2B"], "warnings": [] }

$ mcp-server-onkyo call play_station station=pearl receiver=family-room
{ ..., "detail": "Playing Pearl Jam Radio on pandora", "sent": ["NSV040", "NLSI00003"] }

$ mcp-server-onkyo call set_volume level=90 receiver=family-room zone=zone2
{ ..., "detail": "Zone 2: Volume is now 75.0", "sent": ["ZVL96"],
  "warnings": ["Requested 90, capped at 75 (the owner's limit for this zone)."] }

$ mcp-server-onkyo call set_input source=net receiver=family-room
{ ..., "detail": "Input is now net", "sent": ["SLI2B"],
  "warnings": ["It now plays the same network audio as Zone 2: a receiver has one network
  player, so changing the service or station changes it in every zone on \"net\"."] }

$ mcp-server-onkyo call set_volume level=30 receiver=theater zone=zone2
Error executing tool set_volume: Zone 2 of Theater (127.0.0.1) has no volume control
(fixed-level output, or its outputs are used for other speakers).

$ mcp-server-onkyo call --dry-run set_volume level=40 receiver=theater
{ "receiver": "Theater", "zone": "main", "outcome": "dry_run",
  "detail": "DRY RUN, nothing sent: would set Main zone volume to 40.0", "sent": ["MVL50"] }
```

(Lines wrapped and some fields elided with `...`.)

## Safety design

- **Never guess a receiver.** Several configured and no `receiver` means an error that lists
  them. A bare word that isn't a configured name is an error too, never a DNS lookup.
- **Limits live in the server.** Volume is capped per zone (default 75) whatever the model asks,
  and the reply says it was capped.
- **One exchange at a time per receiver.** A tool call holds the receiver's lock and one TCP
  connection for its whole exchange, so parallel calls or two HTTP clients queue instead of
  mixing replies or opening many connections.
- **Only queries are retried.** A query dropped mid-exchange is asked again on a fresh
  connection; a setter never is (it may have been applied).
- **Not stopping the music by accident.** `list_stations` won't open a different service (which
  stops what's playing) unless `interrupt=true`, and the description tells the model to ask.
- **Dry run** reads everything and sends nothing that changes anything; results say
  `"outcome": "dry_run"` and list what would have been sent.
- **Logs are redacted and on stderr** (stdout is the stdio transport): LAN addresses and MACs
  are masked (`x.x.x.147`); Access JWTs, bearer tokens and keys are always removed.

## Troubleshooting

### When discovery finds nothing

Discovery broadcasts one UDP query (`!xECNQSTN`) to port 60128, and every receiver that hears it
replies with its model and MAC. Broadcasts only reach the local subnet, and switches, access
points and mesh systems often filter them. Then `discover` returns nothing even though the
receivers are fine. You don't need discovery: configure the receivers by address.

- `mcp-server-onkyo discover --debug` shows whether any reply arrives.
- A unicast query, `ONKYO_DISCOVERY_ADDR=192.168.1.147 mcp-server-onkyo discover`, tells you if
  only the broadcast is blocked.
- Try the subnet's broadcast address (`ONKYO_DISCOVERY_ADDR=192.168.1.255`) on a machine with
  several network adapters.
- **WSL2:** NAT networking keeps broadcasts off the LAN. Set `networkingMode=mirrored` in
  `%UserProfile%\.wslconfig` and `wsl --shutdown`.
- Finding the IP otherwise: the router's DHCP leases, the receiver's Setup → Network screen, or
  the ARP table (Onkyo MACs start `00:09:B0`): `ip neigh | grep -i 00:09:b0`.

### Other problems

- **Power-on does nothing:** turn on Network Standby.
- **"didn't reply in time" right after power-on:** some models ignore commands for ~15 s after
  power-on. Wait and retry.
- **Volume numbers don't match the front panel:** set `ONKYO_VOLUME_STEPS` (or `volume_steps`
  for that receiver).
- **Anything else:** `doctor`, then `--debug` (both redacted, safe to paste).

## Development

No receiver needed. Each test runs its own fake receivers on free ports.

```sh
pip install -e '.[dev]'
pytest -q                     # ~250 tests, ~20 s
ruff check . && ruff format --check . && mypy
```

| Command | What it's for |
| --- | --- |
| `mcp-server-onkyo simulate [--write-config DIR]` | Fake receivers on localhost, to point Claude or the Inspector at |
| `mcp-server-onkyo call <tool> key=value ...` | One tool call through the real MCP layer (`call tools` lists them) |
| `mcp-server-onkyo serve --debug` | Log every MCP message and eISCP packet to stderr |
| `npx @modelcontextprotocol/inspector mcp-server-onkyo` | Browse tools, resources and prompts by hand |
| `python -m onkyo_mcp.sim.transcript LOG` | Turn a `call --debug` log into a contract-test fixture |

| Tests | Cover |
| --- | --- |
| `test_transport.py`, `test_protocol.py` | eISCP framing, reply matching, discovery |
| `test_tools.py`, `test_multi_receiver.py` | The MCP contract: schemas, annotations, results, errors, never guessing |
| `test_resources.py` | Resources and prompts (prompts may only name tools that exist) |
| `test_faults.py` | Failure injection: silence, delays, drops mid-exchange, garbled packets, connection limits, push storms |
| `test_contract.py` | Byte-exact replays of transcripts in `tests/fixtures/eiscp/` |
| `test_http_tools.py`, `test_remote.py` | Streamable HTTP end to end, Access JWTs (valid, expired, wrong audience, missing) with locally generated keys |
| `test_config.py`, `test_cli.py`, `test_logsafe.py` | Config parsing, `doctor`/`simulate`/`call`, redaction |
| `test_assumptions.py` | Every assumption is cited by code, and listed here and in HARDWARE_VALIDATION.md |

The fake (`src/onkyo_mcp/sim/fake_receiver.py`) has two profiles from the owner's receivers: the
TX-NR6050 (Zone 2; in standby it changes inputs and answers N/A to volume) and the TX-NR7100
(Zone 2 driving height speakers, so no Zone 2 volume; silent in standby). `Faults` makes either
misbehave on purpose. CI runs the checks on Python 3.11-3.14 and the Alpine smoke test.

`CLAUDE.md` holds the project's conventions and what was seen on hardware.

### How it works

Each eISCP message is a 16-byte header (`ISCP`, header size, data size, version) and a command
like `!1MVL3C\r` (main volume to raw 0x3C, 30.0 on a 0.5-step model). Receivers also push status
nobody asked for, so replies are matched by their 3-letter prefix and, for setters, by the
echoed value. `eiscp.py` speaks the protocol, `receivers.py` decides which receiver a call means
and serializes access to it, `server.py` holds the tools: each `@mcp.tool()` function's
docstring becomes the description the model reads and its type hints the JSON Schema.

## Verification status

Hardware-verified means seen on the owner's TX-NR6050 and/or TX-NR7100 (what was seen is in each
assumption's `note` in `src/onkyo_mcp/assumptions.py`). Simulator-only means the simulator
implements it and nothing has confirmed it.

| Assumption | Confidence | Status |
| --- | --- | --- |
| `O-FRAMING` | high | hardware-verified |
| `O-PUSHES` | high | hardware-verified |
| `O-STANDBY-SILENT` | high | hardware-verified |
| `O-MULTI-COMMAND` | high | simulator-only |
| `O-CONNECTIONS` | low | simulator-only |
| `O-DISCOVERY` | high | hardware-verified |
| `O-VOLUME-STEPS` | high | hardware-verified |
| `O-ZONE2-CODES` | high | hardware-verified |
| `O-ZONE3-CODES` | medium | simulator-only |
| `O-NRI-ZONES` | high | hardware-verified |
| `O-NRI-SERVICES` | medium | simulator-only |
| `O-NSV-CODES` | high | hardware-verified |
| `O-NET-SHARED` | high | simulator-only |
| `O-PLAY-STATE` | high | hardware-verified |
| `O-MENU-LISTS` | high | hardware-verified |
| `O-SOURCE-CODES` | medium | simulator-only |
| `O-LMD-CODES` | medium | simulator-only |

`O-MULTI-COMMAND` is the one the restructure depends on most: tool calls used to open one
connection per command, and now share one. `doctor` exercises it first
([step 2](HARDWARE_VALIDATION.md#2-connect-tcp-and-eiscp-one-receiver-at-a-time-then-both-by-name)).
`test_assumptions.py` fails if this table and `assumptions.py` disagree.

## Roadmap

- [x] Zone 2 / Zone 3, inputs, listening modes, network services, stations, folders
- [x] Several receivers by name; typed results; resources and prompts
- [x] Streamable HTTP behind Cloudflare Access; OpenRC service on Alpine
- [ ] Hardware validation of the above ([HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md))
- [ ] A persistent connection per receiver, with pushes as MCP resource updates (once the Claude
      apps support subscriptions)
- [ ] Publish to PyPI and the MCP registry

## Acknowledgements

Command tables come from [miracle2k/onkyo-eiscp](https://github.com/miracle2k/onkyo-eiscp), with
corrections from the receivers themselves (their NRI lists TIDAL as `1B` and AirPlay as `44`).

## License

[MIT](LICENSE)
