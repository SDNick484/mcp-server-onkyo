# mcp-server-onkyo

An MCP server that controls Onkyo AV receivers over eISCP (TCP 60128).
This is a learning project: the owner wants to understand how MCP servers work,
so explain the *why* of MCP concepts as you go and prefer small, readable
changes over clever ones. Show the JSON-RPC traffic when it helps.

## Layout
- `src/onkyo_mcp/eiscp.py` — the protocol: framing, `Connection`, discovery. No MCP here.
- `src/onkyo_mcp/codes.py` — command tables (`Literal` names + code dicts) and volume conversion. Pure data.
- `src/onkyo_mcp/config.py` — `Settings`: receivers (`config.json` / `ONKYO_HOSTS`), names, per-zone
  volume caps, timeouts, dry run. Loading never raises; problems become `Settings.problems`.
- `src/onkyo_mcp/receivers.py` — `Registry` (which receiver a call means) and `Receiver` (its lock,
  its cached NRI layout). `ReceiverError` is the `ToolError` for anything the model can act on.
- `src/onkyo_mcp/server.py` — MCP tools, then resources (`onkyo://...`) and prompts at the end.
  Each tool runs inside `async with call(receiver) as c:`.
- `src/onkyo_mcp/assumptions.py` — every protocol detail relied on, with confidence and whether
  hardware confirmed it. Cite ids in code (`# ASSUMPTION O-...`); `tests/test_assumptions.py`
  keeps code, README and HARDWARE_VALIDATION.md in step with it.
- `src/onkyo_mcp/cli.py` — `serve` (default), `discover`, `doctor` (`doctor.py`), `simulate`, `call`.
- `src/onkyo_mcp/remote.py`, `logsafe.py` — Streamable HTTP behind Cloudflare Access, and log
  redaction. Shared, byte-identical, with mcp-server-shieldtv, -harmony and -sofabaton: change
  them in all four.
- `src/onkyo_mcp/sim/` — `fake_receiver.py` (simulated receivers with TX-NR6050 and TX-NR7100
  profiles, `Faults` for failure injection, `ReplayReceiver` for contract tests) and
  `transcript.py` (a `--debug` log to a contract-test fixture).
- `deploy/alpine/` — OpenRC service, install script, and the container smoke test CI runs.

## Rules for changes
- **Several receivers: never guess.** Without `receiver`, act only when exactly one is configured
  (or, with none configured, exactly one answers discovery). Otherwise raise a `ReceiverError` that
  names them. A guess turns off the wrong room.
- **One exchange at a time per receiver.** Talk to a receiver only inside `call()` (or
  `Receiver.session()`), which holds its lock and one connection for the whole tool call.
- **Don't invent protocol details.** A command code or reply shape not seen on hardware goes in
  `assumptions.py` (status simulator-only), is cited where the code relies on it, gets a test,
  and gets a step in HARDWARE_VALIDATION.md.
- **Setters honor dry run** (`if c.dry_run: return dry_run(...)` before anything is sent);
  `Call.ask` asserts it as a backstop.

## Conventions
- Python 3.11+, official `mcp` SDK v2 (MCPServer, formerly FastMCP), asyncio only (no threads).
- stdio transport: **never print to stdout** in the server, since stdout carries
  JSON-RPC. Log to stderr.
- Tool docstrings and type hints are the model's only documentation. Use
  `Literal[...]` / `Annotated[int, Field(ge=0, le=100)]` for constrained args.
- Safety limits (e.g. max volume) are enforced server-side, never trusted to the model.

## Testing
- `pytest -q` (in `tests/`). Each test gets its own fake receivers on free ports.
  Async tests use anyio's plugin (`pytest.mark.anyio`), not pytest-asyncio: the
  MCP `Client` fixture needs setup and teardown in the same task.
- By hand: `mcp-server-onkyo simulate --write-config /tmp/sim`, then with
  `ONKYO_CONFIG_DIR=/tmp/sim`: `doctor`, `call <tool> key=value`, or the Inspector
  (`npx @modelcontextprotocol/inspector mcp-server-onkyo`).
- Real hardware: `mcp-server-onkyo doctor --host <ip>`, then HARDWARE_VALIDATION.md.
- Keep README.md's tool, config and verification tables and the roadmap in sync with the code.

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
- Discovery broadcasts didn't reach either receiver from the owner's WSL2 machine
  (unicast did). From the owner's native Linux laptop on Wi-Fi, the default
  255.255.255.255 broadcast finds both (2026-10-07), so the LAN itself passes it.
- The owner's TX-NR7100 runs 5.2.4 using the Zone 2 outputs for height channels,
  so it has only the main zone: its zone commands answer queries but don't work
  (Zone 2 volume `N/A` even when "on", Zone 3 ignores power-on). The TX-NR6050 has
  Zone 2 but no Zone 3 (silence, or `N/A` for SL3). Zones in standby accept input
  changes and answer `N/A` (not silence) to volume/mute.
- Network services in the owner's Onkyo app: Pandora, Spotify, Deezer, AirPlay,
  TIDAL, Amazon Music; the receivers also list TuneIn (0e), so it's offered too. Authoritative NSV codes come from the receiver itself:
  `NRIQSTN` returns XML with <netservicelist> (AirPlay is 44, TIDAL 1b, Amazon 1c;
  the onkyo-eiscp tables are wrong or missing for these) and <zonelist> (value=1
  present, volmax=0 no volume control). The server caches the zone list per host.
- Stations: `NLT<code>01` (list UI, service top) gives item count (hex) and layer;
  `NLAL<seq><layer><start><count>` returns all items as XML, reply prefix `NLAX`
  (expect "NLAX", not "NLA", or send()'s setter-echo rule waits forever);
  `NLSI<5-digit position, from 1>` plays one, confirmed by `NSTP`. icontype `M` =
  music, `0` = the item playing now, `G`/`-` = "Create new station", "Account
  Info", "Sign Out" (never select). `NTC` PLAY/PAUSE/STOP confirm via NST P/p/S;
  TRUP has no state change, so watch NTI. `NDN` = station name.
- A signed-out service opens a popup instead of its menu: `NLT<code>3...` (UI type 3),
  title e.g. "TIDAL Login", "Amazon Music Sign In", "Try Deezer Premium+". TuneIn and
  the music server's top menus are folders only (icontype `F`).
- Folders: `NLSI<pos>` on an `F` item opens it; the receiver announces the new menu
  with `NLT<code>02...` whose layer field is one deeper (top 01, then 02, 03, ...),
  but the old menu's NLT keeps arriving too, so match on the layer (send's `until`).
  Opening can take 3-5s on a music server. Read long menus in NLA pages of 100
  (700 albums in one request took 5.3s, 100 take 0.3s).
- Browsing (NSV) a different service stops what's playing; the same service doesn't.
- `NSV` (select network service) has no echo: the confirmation is a pushed
  `NLT<service code>...<name>`. Text fields (titles, stations) are UTF-8.
