# Hardware validation (Onkyo)

Much of eISCP was confirmed on the TX-NR6050 and TX-NR7100 in 2026-10, before the
`hardware-free` branch. Everything that branch added was built away from the receivers and is
**verified against the simulator only** until you run the step below that covers it. This is the
checklist for that session. Each step depends only on the ones before it and names the
assumptions it confirms (ids, claims and sources are in `src/onkyo_mcp/assumptions.py`).

Plan on about 45 minutes for both receivers. `doctor` points at steps 1-4 by number when a check
fails, so keep the numbering if you edit this file.

## What's new and unconfirmed

| Feature (hardware-free branch) | Status | Step |
| --- | --- | --- |
| Several receivers by name (`config.json`, `ONKYO_HOSTS`), never guessing | verified against simulator only | 2 |
| One TCP connection per tool call, one tool call at a time per receiver | verified against simulator only | 2, 11 |
| `doctor`, `doctor --dump` | verified against simulator only | 2-4 |
| `get_status` covering every receiver and zone | verified against simulator only | 4 |
| Per-zone volume caps (`max_volume.zone2`, `ONKYO_MAX_VOLUME_ZONE2`) | verified against simulator only | 6 |
| N/A from a zone in standby explained as "in standby" | verified against simulator only | 7 |
| Shared network player warnings | verified against simulator only | 9 |
| `--dry-run` / `ONKYO_DRY_RUN` | verified against simulator only | 5 |
| MCP resources (`onkyo://...`) and prompts | verified against simulator only | 12 |
| Streamable HTTP + Cloudflare Access (JWT checks tested with local keys) | verified against simulator only | 13 |
| OpenRC service on Alpine (tested in a `python:3.12-alpine` container) | verified against simulator only | 13 |
| Log redaction (addresses, MACs; JWTs, keys) | verified against simulator only | 2 |

## Before you start

- Both receivers on, with **Network Standby** enabled (Setup → Hardware → Power Management)
- Their IP addresses: the TX-NR6050 was at 192.168.1.147; check the TX-NR7100's in the router
- Something playing on neither (steps 9-10 change the network player)

```sh
git clone https://github.com/SDNick484/mcp-server-onkyo.git && cd mcp-server-onkyo
git checkout hardware-free
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # expect: all passed
mkdir -p captures
```

**Tool calls** use `mcp-server-onkyo call <tool> key=value ...`. It runs the tool through the same
MCP layer the model uses and prints its structured result. `call tools` lists the tools.

**Recording a result.** When a step confirms an assumption, set its `status` to
`"hardware-verified"` in `assumptions.py` and put what you saw (model, firmware) in `note`. When
it contradicts one, set `"hardware-contradicted"` and keep the output. Update the README's
verification table to match (`pytest` fails until you do), and change the feature's row above to
"verified on hardware".

**Capturing a contract test.** For any call whose traffic you want pinned forever:

```sh
mcp-server-onkyo call --debug <tool> key=value ... 2> captures/<name>.log
python -m onkyo_mcp.sim.transcript captures/<name>.log > tests/fixtures/eiscp/hw_<name>.json
```

Fill in the fixture's `description`, `source` and `assumptions`. `tests/test_contract.py` replays
it against the server byte for byte.

---

## 1. Discovery

```sh
mcp-server-onkyo discover
```

Expected, one line per receiver (order varies):

```
192.168.1.147    TX-NR6050    00:09:B0:F7:6C:FD  port 60128
192.168.1.x      TX-NR7100    00:09:B0:62:3D:93  port 60128
```

Nothing found: run it again with `--debug` and try a unicast query,
`ONKYO_DISCOVERY_ADDR=192.168.1.147 mcp-server-onkyo discover`. A unicast answer means only the
broadcast is filtered (WSL2 NAT, Wi-Fi client isolation, a VLAN): inconclusive, not a
contradiction. Repeat this step from the LXC in step 13; a Proxmox bridge can filter broadcasts
too.

Confirms: **O-DISCOVERY** (already verified from the laptop; this re-checks the machine you're on).

## 2. Connect: TCP and eISCP, one receiver at a time, then both by name

```sh
mcp-server-onkyo doctor --host 192.168.1.147
mcp-server-onkyo doctor --host <TX-NR7100 ip>
```

Expected for each, ending in `OK`:

```
Receiver 192.168.1.147: TX-NR6050
   ok tcp       connected in <n> ms
   ok eiscp     main zone on; answered in <n> ms
   ok describe  TX-NR6050; zones: main, zone2; 8 network services
   ok zones     main on, zone2 standby
```

Addresses print as `x.x.x.147` (redaction is on by default; `--no-redact` shows them). Then name
them, in `~/.config/mcp-server-onkyo/config.json`:

```json
{
  "receivers": [
    {"host": "192.168.1.147", "name": "Family Room"},
    {"host": "<TX-NR7100 ip>", "name": "Theater"}
  ],
  "max_volume": {"main": 75, "zone2": 50}
}
```

```sh
mcp-server-onkyo doctor
mcp-server-onkyo call set_power state=on
```

Expected: `doctor` checks both by name. The `set_power` without `receiver` must **fail** with
`Several receivers are configured: Family Room (...), Theater (...)` and change nothing.

`doctor` sends PWRQSTN, NRIQSTN and every zone's power query over **one** connection, so a pass
here is the first real evidence for O-MULTI-COMMAND. If `eiscp` passes but `describe` or `zones`
fails with `IncompleteReadError` or a timeout on the second command, **O-MULTI-COMMAND is
contradicted**: capture `doctor --host <ip> --debug 2> captures/doctor-debug.log`.

Confirms: **O-FRAMING**, **O-PUSHES** (re-check), **O-MULTI-COMMAND**.

## 3. Self-description

```sh
mcp-server-onkyo doctor --dump captures/
mcp-server-onkyo call list_net_services receiver=Theater
```

Expected: `captures/Family_Room.json` and `captures/Theater.json`, each with the receiver's NRI
XML. Look at the TX-NR7100's `<zonelist>`: the simulator assumes its Zone 2 (driving height
speakers) appears as `volmax="0"`. If it says `value="1" volmax="100"` instead, record it: the
server will then offer Zone 2 volume on that receiver and get `N/A` (step 7 explains that
case). `list_net_services` should list one entry per `<netservice value="1">` in the XML, with
the names the receiver uses.

Confirms: **O-NRI-ZONES** (the TX-NR7100 part), **O-NRI-SERVICES**.

## 4. Zones and status

```sh
mcp-server-onkyo call get_status
mcp-server-onkyo call get_status receiver=Theater zone=zone3
```

Expected: the first lists both receivers, every zone each has, with power, volume, `volume_cap`
(75 main, 50 zone2 from the config above) and input matching the front panels. The second fails
with `Theater (...), a TX-NR7100, has no Zone 3.` if the NRI says Zone 3 is absent, or a "didn't
answer for Zone 3" message if it lists it but the zone is unused.

Neither receiver has a working Zone 3, so **O-ZONE3-CODES stays unconfirmed**: leave it
simulator-only unless you can try a model with Zone 3.

Confirms: **O-ZONE2-CODES** (re-check).

## 5. Dry run

```sh
mcp-server-onkyo call --dry-run set_volume level=40 receiver="Family Room"
mcp-server-onkyo call --dry-run set_input source=net receiver=Theater
mcp-server-onkyo call get_status receiver="Family Room"
```

Expected: `"outcome": "dry_run"`, `"detail": "DRY RUN, nothing sent: would ..."` and the command
in `sent` (e.g. `MVL50`). The front panels don't change, and `get_status` shows the old volume.

Confirms: no assumption (dry run never reaches the wire); it confirms the feature.

## 6. Volume, caps, and 0.5 steps

```sh
mcp-server-onkyo call set_volume level=30.5 receiver="Family Room"
mcp-server-onkyo call set_power state=on receiver="Family Room" zone=zone2
mcp-server-onkyo call set_volume level=90 receiver="Family Room" zone=zone2
```

Expected: the front panel shows **30.5**. Zone 2 comes on, then `"detail": "Zone 2: Volume is
now 50.0"` with the warning `Requested 90, capped at 50 (the owner's limit for this zone).`, and
Zone 2's volume on the receiver's display (or the Onkyo app) is 50.0, not higher.

Confirms: **O-VOLUME-STEPS** (re-check), per-zone caps.

## 7. Standby

```sh
mcp-server-onkyo call set_power state=off receiver="Family Room" zone=zone2
mcp-server-onkyo call set_volume level=20 receiver="Family Room" zone=zone2
mcp-server-onkyo call set_input source=net receiver="Family Room" zone=zone2
mcp-server-onkyo call set_power state=off receiver=Theater
mcp-server-onkyo call set_volume level=20 receiver=Theater
```

Expected: both `set_volume` calls fail with `... is in standby. Turn it on with set_power ...
first.` The TX-NR6050 gets there through an `N/A` reply, the TX-NR7100 through silence (so its
call takes about the timeout, 5 s). The `set_input` on the TX-NR6050's Zone 2 in standby
**succeeds** (`Zone 2: Input is now net`), which the simulator models from what you saw earlier.

Confirms: **O-STANDBY-SILENT** (both behaviors, now with the new error path).

## 8. Inputs and listening modes

Turn the main zones back on (`set_power state=on receiver=...`), then for each input that has
something connected:

```sh
mcp-server-onkyo call set_input source=<input> receiver="Family Room"
```

Expected: the front panel shows the matching input. The codes come from the onkyo-eiscp tables;
only `bd-dvd` and `net` were seen on hardware. Note any input that lands on the wrong label.

```sh
mcp-server-onkyo call set_listening_mode mode=dolby-surround receiver=Theater
mcp-server-onkyo call set_listening_mode mode=dts-neural-x receiver=Theater
mcp-server-onkyo call set_listening_mode mode=game-action receiver=Theater
mcp-server-onkyo call get_status receiver=Theater zone=main
```

Expected: the display names each mode. A mode can be refused for the current input signal; that
is an `N/A` and a clear error, not a contradiction. A mode that's accepted but shows a different
name contradicts **O-LMD-CODES**: write down the name shown.

Confirms: **O-SOURCE-CODES**, **O-LMD-CODES**.

## 9. The shared network player

```sh
mcp-server-onkyo call set_input source=net receiver="Family Room"
mcp-server-onkyo call set_input source=net receiver="Family Room" zone=zone2   # Zone 2 on first
mcp-server-onkyo call play_station station="<one of your Pandora stations>" receiver="Family Room"
```

Expected: the second `set_input` warns `It now plays the same network audio as Main zone ...`,
and both rooms play the station. Then give Zone 2 a different input
(`set_input source=same-as-main ... zone=zone2` or any other) and confirm the main zone keeps
playing. If two zones on "net" ever play different things, **O-NET-SHARED is contradicted**.

Confirms: **O-NET-SHARED**.

## 10. Music server

```sh
mcp-server-onkyo call list_stations service=music-server receiver="Family Room" interrupt=true
mcp-server-onkyo call list_stations service=music-server folder='["MiniDLNA"]' receiver="Family Room"
```

Expected: the server's folders, as on the receiver's screen. This re-checks **O-MENU-LISTS** and
**O-NSV-CODES** (`00`) after the restructure; both were verified before it.

## 11. Two controllers at once

With the Onkyo Controller app open on your phone (it holds its own connection), run two calls in
parallel:

```sh
mcp-server-onkyo call get_status receiver=Theater & mcp-server-onkyo call get_status receiver=Theater; wait
```

Expected: both succeed. These are two processes, so they open two connections, plus the app's
third. If one fails with `IncompleteReadError` or "can't connect", the receiver limits
connections: record how many it took, for **O-CONNECTIONS**. (Within one server, the lock means
it never opens more than one per receiver; `tests/test_faults.py` covers that.)

Confirms: **O-CONNECTIONS** (a lower bound).

## 12. With Claude: resources and prompts

```sh
claude mcp add onkyo -- mcp-server-onkyo
```

In Claude Code:
1. `@onkyo:onkyo://receivers` attaches both receivers' zones and services. Check the zones match
   step 3.
2. `/mcp__onkyo__play_music zone2 pandora` (prompt arguments are positional: room, service,
   station, volume) plays a Pandora station in Zone 2. "zone2" names no receiver, so the model
   should work out from `get_status` that only Family Room has a usable Zone 2, or ask.
3. `/mcp__onkyo__all_off` puts every zone that is on into standby.

Confirms: the features (resources, prompts); no assumption.

## 13. As a service: Alpine LXC, HTTP, Cloudflare Access

In the LXC (Alpine 3.20+, as root):

```sh
apk add git
git clone -b hardware-free https://github.com/SDNick484/mcp-server-onkyo.git /root/mcp-server-onkyo
sh /root/mcp-server-onkyo/deploy/alpine/install.sh /root/mcp-server-onkyo
install -m 0640 -o root -g mcp-onkyo config.json /etc/mcp-server-onkyo/   # step 2's, copied into the LXC
rc-update add mcp-server-onkyo default && rc-service mcp-server-onkyo start
curl -s http://127.0.0.1:8711/healthz
su -s /bin/sh mcp-onkyo -c 'ONKYO_CONFIG_DIR=/etc/mcp-server-onkyo /opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo doctor'
```

Expected: `install.sh` finishes without compiling anything (every dependency has a musl wheel),
`healthz` answers `{"status": "ok"}`, `ps -o user,args | grep onkyo` shows `mcp-onkyo`, and `doctor` passes as
in step 2. Then step 1 from here.

With cloudflared and an Access application (see README: HTTP and Cloudflare Access), set
`CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` and `MCP_PUBLIC_HOSTS` in `/etc/mcp-server-onkyo/env`,
restart, and check:

```sh
curl -si https://<public host>/onkyo/mcp | head -1       # expect: a redirect to Access sign-in, or 403
curl -si -H 'Host: <public host>' http://127.0.0.1:8711/onkyo/mcp | head -1   # expect: 403 (no JWT)
```

Then add `https://<public host>/onkyo/mcp` as a custom connector in Claude and call `get_status`.
`/var/log/mcp-server-onkyo/server.log` should show the request with addresses redacted and no
JWT.

Confirms: the deployment; no protocol assumption.

---

## Which step confirms what

| Assumption        | Step | Confidence before | Notes                                                     |
| ----------------- | ---- | ----------------- | --------------------------------------------------------- |
| `O-FRAMING`       | 2    | high              | already hardware-verified                                 |
| `O-PUSHES`        | 2    | high              | already hardware-verified                                 |
| `O-STANDBY-SILENT` | 7  | high              | already hardware-verified; re-checks the new error path   |
| `O-MULTI-COMMAND` | 2    | high              | `doctor` uses one connection for every query              |
| `O-CONNECTIONS`   | 11   | low               | a lower bound only                                        |
| `O-DISCOVERY`     | 1    | high              | already hardware-verified; repeat from the LXC            |
| `O-VOLUME-STEPS`  | 6    | high              | already hardware-verified                                 |
| `O-ZONE2-CODES`   | 4, 6 | high              | already hardware-verified                                 |
| `O-ZONE3-CODES`   | none | medium            | needs a receiver with a working Zone 3                    |
| `O-NRI-ZONES`     | 3    | high              | the TX-NR7100's Zone 2 `volmax` is the open part          |
| `O-NRI-SERVICES`  | 3    | medium            | compare `list_net_services` with the dumped XML           |
| `O-NSV-CODES`     | 10   | high              | already hardware-verified                                 |
| `O-NET-SHARED`    | 9    | high              |                                                           |
| `O-PLAY-STATE`    | 9    | high              | already hardware-verified                                 |
| `O-MENU-LISTS`    | 10   | high              | already hardware-verified                                 |
| `O-SOURCE-CODES`  | 8    | medium            | only inputs with something connected                      |
| `O-LMD-CODES`     | 8    | medium            | a refused mode is not a contradiction                     |

## What to send back if something fails

- The full `doctor` output (redacted by default), or `doctor --json`.
- The `captures/` files from `doctor --dump` (NRI XML; addresses and MACs redacted).
- For a failing call: the same call with `--debug 2> captures/<name>.log`. The log is redacted
  too; `--no-redact` shows addresses if they matter. Convert it with
  `python -m onkyo_mcp.sim.transcript` (see above) to turn it into a failing contract test.
- The receiver's model and firmware version (the receiver's setup menu or the Onkyo Controller app shows it).
