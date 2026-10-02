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
- Unit (not written yet): `pytest` against `fake_receiver.py`.
- Protocol: `npx @modelcontextprotocol/inspector mcp-server-onkyo`
- Real hardware: `ONKYO_HOST=<ip> mcp-server-onkyo`
- Keep README.md's tool table, config table and roadmap in sync with the code.

## eISCP reference
Command tables: https://github.com/miracle2k/onkyo-eiscp (eiscp-commands.yaml).
Target hardware: TX-NR7100 and TX-NR6050 (2021+). Volume (MVL) is hex in
0.5-dB-style steps: raw 0x00-0xC8 = display 0.0-100.0 (`ONKYO_VOLUME_STEPS=2`).
Receivers need Network Standby enabled to power on over the network.
Receivers push unsolicited status messages; match replies by 3-char command prefix.
