# mcp-server-onkyo

An [MCP](https://modelcontextprotocol.io) server for controlling Onkyo AV
receivers over the network, so Claude Code, Claude Desktop, or any other MCP
client can power them on, set the volume, mute them, and find them on your
LAN.

It speaks **eISCP** (the Integra Serial Control Protocol over Ethernet, TCP/UDP
port 60128) directly and needs nothing but the official MCP Python SDK.

> **Status: early / pre-alpha.** This is also a learning project for MCP server
> development, so the code favors readability over cleverness. The current tools
> are tested against the bundled simulated receiver; validation on real hardware
> (TX-NR7100, TX-NR6050) is in progress.

## Tools

| Tool | What it does |
| --- | --- |
| `discover_receivers` | Broadcasts an eISCP discovery query and returns each receiver's IP, model, port and MAC |
| `get_status` | A zone's power state, volume (0–100 display scale), mute state and input, plus the listening mode for the main zone |
| `set_power` | Turn a zone on, or put it into standby |
| `set_volume` | Set a zone's volume (0.5 steps on newer models); clamped to a configurable safety cap in every zone |
| `set_mute` | Mute or unmute a zone |
| `set_input` | Select a zone's input (`bd-dvd`, `game`, `cbl-sat`, `strm-box`, `pc`, `aux`, `tv`, `phono`, `cd`, `fm`, `am`, `net`, `bluetooth`; zones 2/3 also `same-as-main`) |
| `set_listening_mode` | Set the main zone's listening mode (`stereo`, `direct`, `pure-audio`, `all-ch-stereo`, `full-mono`, `theater-dimensional`, `dolby-surround`, `dts-neural-x`, `game-rpg`, `game-action`, `game-rock`, `game-sports`) |
| `select_net_service` | Switch the network player to a streaming service (`pandora`, `spotify`, `deezer`, `tidal`, `amazon-music`, `airplay`, `tunein`) |
| `get_now_playing` | The network player's service, station, play state, title, artist, album and position |
| `list_stations` | What a service's top menu can play, e.g. your Pandora stations |
| `play_station` | Start a station by name (part of the name is enough) |
| `control_playback` | Play, pause, stop, next or previous track on the network player |

Every tool except `discover_receivers` takes an optional `receiver` argument
(an IP address from `discover_receivers`) for networks with several receivers.
Without it, tools talk to `ONKYO_HOST`, or, if that's unset, to the one
receiver discovery finds (with several, the model is asked to pick one).

**Zones.** `get_status`, `set_power`, `set_volume`, `set_mute` and `set_input`
take an optional `zone`: `main` (the default, the room the receiver is in),
`zone2` or `zone3` (speakers in other rooms). Zones are independent: Zone 2 can
play while the main zone is in standby. The server asks each receiver which
zones it has (once, via its `NRIQSTN` self-description), so a missing zone,
or a zone without volume control, gets a clear answer instead of a timeout.

Zones on recent Onkyo models:

| Model | Zones | Multi-zone outputs |
| --- | --- | --- |
| TX-RZ71, TX-RZ70 | 3 | Main, powered Zone 2 / line out, Zone 3 line out; HDMI Zone 2 |
| TX-RZ61, TX-RZ51, TX-RZ50 | 3 | Main, powered Zone 2 / line out, Zone 3 line out |
| TX-NR7200 | 3 | Main, powered Zone 2, Zone 3 line out |
| TX-NR7100 | 3 | Main, powered Zone 2 / line out, Zone 3 line out; HDMI Zone 2 |
| TX-RZ31, TX-RZ30 | 2 | Main, powered Zone 2 / line out |
| TX-NR6200, TX-NR6100, TX-NR6050, TX-NR5100 | 2 | Main, powered Zone 2 / line out |

What's usable also depends on the speaker setup. A powered Zone 2's amplifier
channels can be assigned to other speakers instead (e.g. height channels in
a 5.2.4 layout), and then that zone can't be used, or has no volume control.

**Network audio.** A receiver has one network player, shared by every zone
whose input is `net`. To play Pandora in Zone 2: `set_power` and
`set_input net` with `zone2`, then `play_station` with a station name from
`list_stations` (e.g. "Pearl Jam Radio"). Only music items can be played, so
menu entries like "Sign Out" are never selected. Which
services work depends on the model, region and firmware, and each must be
signed in on the receiver (e.g. in the Onkyo Controller app). The list
matches what the Onkyo Controller app offers for the TX-NR6050/7100, plus
TuneIn, which the receivers list themselves; only
Pandora has been verified so far. AirPlay and Spotify are normally started
from a phone (AirPlay, Spotify Connect).

Each tool also declares MCP tool annotations. `discover_receivers`,
`get_status` and `get_now_playing` are read-only. The `set_*`, `select_*`,
`list_stations` and `play_station` tools are marked non-destructive and
idempotent, so clients can tell they're safe to retry. `control_playback` is
not idempotent: "next" twice skips two tracks.

## Requirements

- Python 3.11+
- A network-connected Onkyo receiver (Integra and some Pioneer models speak the
  same protocol). For power-on to work, enable **Network Standby** on the
  receiver (Setup → Hardware → Power Management).

## Install

```sh
pip install git+https://github.com/SDNick484/mcp-server-onkyo
```

Or, from a clone:

```sh
git clone https://github.com/SDNick484/mcp-server-onkyo
cd mcp-server-onkyo
pip install -e .
```

## Find your receiver

```sh
mcp-server-onkyo --discover
```

```
192.168.1.50     TX-NR7100    00:09:B0:12:34:56  port 60128
```

Discovery relies on UDP broadcast, which some networks silently drop. If it
finds nothing, see [When discovery finds nothing](#when-discovery-finds-nothing).
You don't need discovery: setting `ONKYO_HOST` to the receiver's IP is enough.

## Configuration

All settings are environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONKYO_HOST` | none | Default receiver IP address (tools that take a `receiver` argument can target others). If unset, the server uses the one receiver that answers discovery |
| `ONKYO_PORT` | `60128` | eISCP port |
| `ONKYO_MAX_VOLUME` | `75` | Safety cap on the display scale. Enforced by the server, not left to the model |
| `ONKYO_VOLUME_STEPS` | `2` | Raw volume steps per display unit: `2` for newer models with 0.5 steps (TX-NR6050, TX-NR7100), `1` for older ones |
| `ONKYO_TIMEOUT` | `5` | Seconds to wait for a receiver to reply (power commands get 3×). Some models are slow: a TX-NR7100 takes ~1.5 s to answer a query and ~10 s to confirm standby |
| `ONKYO_DISCOVERY_ADDR` | `255.255.255.255` | Where the discovery query is sent |
| `ONKYO_DEBUG` | off | `1` logs all MCP and eISCP traffic to stderr (same as `--debug`). See [Debugging](#debugging) |

## Use with Claude Code

```sh
claude mcp add onkyo -e ONKYO_HOST=192.168.1.50 -- mcp-server-onkyo
```

Then ask things like *"turn the receiver on and set the volume to 30"*.

## Use with Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "onkyo": {
      "command": "mcp-server-onkyo",
      "env": { "ONKYO_HOST": "192.168.1.50" }
    }
  }
}
```

## Development

No receiver needed: `fake_receiver.py` simulates one on `127.0.0.1:60128`. It
answers discovery, remembers power/volume/mute state, and sends unsolicited
status messages the way real receivers do. Like a TX-NR7100, it ignores
everything but power and queries while in standby.

```sh
python fake_receiver.py &
ONKYO_DISCOVERY_ADDR=127.0.0.1 mcp-server-onkyo --discover
ONKYO_HOST=127.0.0.1 npx @modelcontextprotocol/inspector mcp-server-onkyo
```

Run the tests (no receiver needed; each test gets its own fake receiver on a
free port):

```sh
pip install -e '.[dev]'
pytest
```

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lets you
browse `tools/list`, call tools by hand and watch the JSON-RPC traffic.

### Debugging

Set `ONKYO_DEBUG=1` or pass `--debug` to log both conversations the server
has, to stderr: MCP messages with the client (`MCP <-` / `MCP ->`, tagged with
the JSON-RPC id) and eISCP packets with the receiver (`eISCP ->` / `eISCP <-`):

```
08:11:59 MCP <- [2] tools/call {"name": "set_volume", "arguments": {"level": 30}}
08:11:59 eISCP -> 127.0.0.1 MVL3C
08:11:59 eISCP <- 127.0.0.1 NLSU0-Now Playing (unsolicited, skipped)
08:11:59 eISCP <- 127.0.0.1 MVL3C
08:11:59 MCP -> [2] {"content": [{"text": "Volume is now 30.0", "type": "text"}], ...}
```

It works with `--discover` too, to see which UDP replies arrive. Under Claude
Code, register the server with `-e ONKYO_DEBUG=1`; stderr ends up in Claude
Code's MCP logs (run `claude --debug` to see them). The MCP Inspector shows
stderr in its UI.

`CLAUDE.md` holds project conventions for working on the code with Claude Code.

### How it works

`onkyo_mcp.py` has two layers:

1. **eISCP transport.** Each message is a 16-byte header (`ISCP`, header size,
   data size, version) followed by a command such as `!1MVL3C\r`, which sets
   master volume to raw 0x3C (30.0 on a 0.5-step receiver). Replies are matched by
   their 3-letter command prefix, because receivers also push status updates
   nobody asked for.
2. **MCP tools.** Each `@mcp.tool()` function is published in `tools/list`. Its
   docstring becomes the description and its type hints become the JSON Schema
   the model sees.

## Troubleshooting

### When discovery finds nothing

Discovery broadcasts one UDP query (`!xECNQSTN`) to port 60128, and every
receiver that hears it replies with its model and MAC. A broadcast is the
weakest link here: it only reaches the local subnet, and switches, access
points and mesh systems are often set to filter it. When that happens,
`--discover` and the `discover_receivers` tool return nothing, even though the
receivers are online and fully controllable.

**Narrow it down.** `--debug` shows whether any reply comes back at all:

```sh
mcp-server-onkyo --discover --debug
```

If you know (or suspect) a receiver's IP, send the same query straight to it.
A reply means the receiver and the path back to you are fine, and only the
broadcast is being dropped:

```sh
ONKYO_DISCOVERY_ADDR=192.168.1.50 mcp-server-onkyo --discover
```

**Finding the IP without discovery:**
- Your router's list of connected clients / DHCP leases.
- The receiver's own Setup → Network screen.
- Your computer's ARP table. Onkyo hardware uses the MAC prefix `00:09:B0`.
  This only lists devices your computer has talked to recently:
  `arp -a | findstr /i 00-09-b0` (Windows) or `ip neigh | grep -i 00:09:b0` (Linux).

**Workarounds:**
- Set `ONKYO_HOST` to the receiver's IP and skip discovery. Reach any other
  receivers through the tools' `receiver` argument. Give each receiver a DHCP
  reservation in your router so its IP doesn't change.
- Try the subnet's own broadcast address instead of `255.255.255.255`, e.g.
  `ONKYO_DISCOVERY_ADDR=192.168.1.255` for 192.168.1.0/24. This helps when a
  computer with several network adapters sends the broadcast out of the wrong one.
- Check your network gear for broadcast/multicast filtering, client (AP)
  isolation, or VLANs separating the computer from the receivers.
- **WSL2:** the default NAT networking keeps broadcasts off your LAN. Set
  `networkingMode=mirrored` in `%UserProfile%\.wslconfig` and run
  `wsl --shutdown`. Firewalls must also allow the receivers' UDP replies.

### Other problems

- **Power-on does nothing:** enable Network Standby on the receiver.
- **Volume numbers don't match the front panel:** adjust `ONKYO_VOLUME_STEPS`.

## Roadmap

- [x] Validate on TX-NR7100 / TX-NR6050 hardware
- [x] Multiple receivers from one server (a `receiver` argument on each tool)
- [x] Input selection
- [x] Listening modes
- [x] Zone 2 / Zone 3
- [x] Network services: select a service, now playing
- [x] Network playback: stations by name, play/pause/stop/next/previous
- [ ] Browsing deeper menus (playlists, albums, TuneIn categories)
- [ ] Discovery that works where broadcasts are filtered (query a configured
      list of IPs directly)
- [ ] Typed (structured) tool output
- [ ] Receiver state as MCP resources
- [ ] Persistent connection with push updates
- [ ] Streamable HTTP transport for running on a home server
- [x] Tests

## Acknowledgements

Protocol details and command tables come from
[miracle2k/onkyo-eiscp](https://github.com/miracle2k/onkyo-eiscp).

## License

[MIT](LICENSE)
