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
| `get_status` | Power state, master volume (0–100 display scale), mute state, selected input and listening mode |
| `set_power` | Turn the main zone on, or put it into standby |
| `set_volume` | Set master volume (0.5 steps on newer models); clamped to a configurable safety cap |
| `set_mute` | Mute or unmute the main zone |
| `set_input` | Select the main zone input (`bd-dvd`, `game`, `cbl-sat`, `strm-box`, `pc`, `aux`, `tv`, `phono`, `cd`, `fm`, `am`, `net`, `bluetooth`) |
| `set_listening_mode` | Set the listening mode (`stereo`, `direct`, `pure-audio`, `all-ch-stereo`, `full-mono`, `theater-dimensional`, `dolby-surround`, `dts-neural-x`, `game-rpg`, `game-action`, `game-rock`, `game-sports`) |

Every tool except `discover_receivers` takes an optional `receiver` argument
(an IP address from `discover_receivers`) for networks with several receivers.
Without it, tools talk to `ONKYO_HOST`.

Each tool also declares MCP tool annotations. `discover_receivers` and
`get_status` are read-only. The `set_*` tools are marked non-destructive and
idempotent, so clients can tell they're safe to retry.

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
192.168.1.50     TX-NR7100    00:09:B0:62:3D:93  port 60128
```

## Configuration

All settings are environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONKYO_HOST` | `192.168.1.50` | Default receiver IP address (tools that take a `receiver` argument can target others) |
| `ONKYO_PORT` | `60128` | eISCP port |
| `ONKYO_MAX_VOLUME` | `50` | Safety cap on the display scale. Enforced by the server, not left to the model |
| `ONKYO_VOLUME_STEPS` | `2` | Raw volume steps per display unit: `2` for newer models with 0.5 steps (TX-NR6050, TX-NR7100), `1` for older ones |
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
status messages the way real receivers do.

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

- **`--discover` finds nothing:** discovery is a UDP broadcast, so it only
  reaches receivers on the same subnet. On **WSL2**, the default NAT networking
  keeps broadcasts off your LAN. Set `networkingMode=mirrored` in
  `%UserProfile%\.wslconfig` and run `wsl --shutdown`, or run discovery from
  Windows Python. Firewalls must allow the receivers' UDP replies.
- **Power-on does nothing:** enable Network Standby on the receiver.
- **Volume numbers don't match the front panel:** adjust `ONKYO_VOLUME_STEPS`.

## Roadmap

- [ ] Validate on TX-NR7100 / TX-NR6050 hardware
- [x] Multiple receivers from one server (a `receiver` argument on each tool)
- [x] Input selection
- [x] Listening modes
- [ ] Zone 2 / Zone 3
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
