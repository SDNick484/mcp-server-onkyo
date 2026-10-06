# mcp-server-onkyo

An MCP server that controls Onkyo AV receivers over eISCP (TCP 60128).
This is a learning project: the owner wants to understand how MCP servers work,
so explain the *why* of MCP concepts as you go and prefer small, readable
changes over clever ones. Show the JSON-RPC traffic when it helps.

## Layout
- `onkyo_mcp.py` — the server. Two layers: eISCP transport (plain protocol code)
  and MCP tools (`@mcp.tool()` functions). Keep MCP concerns out of the transport layer.
- `fake_receiver.py` — simulated receiver on 127.0.0.1:60128 for hardware-free dev.

## Conventions
- Python 3.11+, official `mcp` SDK v2 (MCPServer, formerly FastMCP), asyncio only (no threads).
- stdio transport: **never print to stdout** in the server, since stdout carries
  JSON-RPC. Log to stderr.
- Tool docstrings and type hints are the model's only documentation. Use
  `Literal[...]` / `Annotated[int, Field(ge=0, le=100)]` for constrained args.
- Safety limits (e.g. max volume) are enforced server-side, never trusted to the model.

## Testing
- Unit: `pytest` (in `tests/`). Each test gets its own `fake_receiver` on a free port.
  Async tests use anyio's plugin (`pytest.mark.anyio`), not pytest-asyncio: the
  MCP `Client` fixture needs setup and teardown in the same task.
- Protocol: `npx @modelcontextprotocol/inspector mcp-server-onkyo`
- Real hardware: `ONKYO_HOST=<ip> mcp-server-onkyo`
- Keep README.md's tool table, config table and roadmap in sync with the code.

## eISCP reference
Command tables: https://github.com/miracle2k/onkyo-eiscp (eiscp-commands.yaml).
Target hardware: TX-NR7100 and TX-NR6050 (2021+). Volume (MVL) is hex in
0.5-dB-style steps: raw 0x00-0xC8 = display 0.0-100.0 (`ONKYO_VOLUME_STEPS=2`).
Receivers need Network Standby enabled to power on over the network.
Receivers push unsolicited status messages; match replies by 3-char command prefix
(and, for setters, by the echoed value: a same-prefix push is not the reply).

Observed on real hardware (TX-NR7100 is the awkward one; the TX-NR6050 answers in ~0.1s):
- Slow: ~1.5s for a query, ~4s to confirm power-on, up to ~10s to confirm standby.
- In standby it answers queries but silently ignores setters (no `N/A`).
- After confirming `PWR01` it pushes a status burst and ignores setters for ~15s.
- Discovery broadcasts don't reach either receiver on the owner's LAN; unicast does.
- Zones 2/3 behave unlike the main zone: in standby they accept input changes and
  answer `N/A` (not silence) to volume/mute. 7100 Zone 2 volume is `N/A` even when on
  (probably fixed-level output); 7100 Zone 3 answers queries but ignores power-on.
  The 6050 has no Zone 3 (silence, or `N/A` for SL3).
- `NSV` (select network service) has no echo: the confirmation is a pushed
  `NLT<service code>...<name>`. Text fields (titles, stations) are UTF-8.
