# Tasks

## 1. Test fixtures

- [ ] 1.1 Add a `model` argument to `fake_receiver.start()` (default `TX-NR7100`) and use it in the ECN discovery reply; verify the existing `pytest` suite still passes unchanged
- [ ] 1.2 Add a fixture that starts two fakes on the same port, at 127.0.0.1 (TX-NR7100) and 127.0.0.2 (TX-NR6050), skipping the test when 127.0.0.2 can't be bound; verify a test using it is collected and runs on Linux

## 2. Multi-address discovery

- [ ] 2.1 Parse `ONKYO_DISCOVERY_ADDR` as a comma-separated list, trimming spaces and dropping empty entries, and skip entries that aren't IP addresses with a warning on the module logger; verify with unit tests for `" 127.0.0.1 , ,127.0.0.2"` and `"receiver.invalid,127.0.0.1"`
- [ ] 2.2 Send the query to every address from one socket before the collection window, handling an `OSError` from one `sendto` by warning and continuing; verify `discover()` returns both fakes from `"127.0.0.1,127.0.0.2"`, sorted by model, in one window
- [ ] 2.3 Keep de-duplication by IP; verify a receiver reached through two list entries is returned once
- [ ] 2.4 Verify the single-address path is unchanged: the existing discovery tests and `ONKYO_DISCOVERY_ADDR=127.0.0.1 mcp-server-onkyo --discover` against `fake_receiver.py` give the same output as before
- [ ] 2.5 Verify the default-receiver fallback through MCP: with `ONKYO_HOST` unset and both fakes listed, `get_status` fails with the "Several receivers found" error naming both models

## 3. Documentation

- [ ] 3.1 Update README's configuration table entry for `ONKYO_DISCOVERY_ADDR` (list syntax; listing IPs replaces the broadcast unless `255.255.255.255` is listed too); verify the table matches `onkyo_mcp.py`
- [ ] 3.2 Update "When discovery finds nothing" with a list example and the WSL2 case, next to the DHCP reservation advice; verify the commands shown run against `fake_receiver.py`
- [ ] 3.3 Tick "Discovery that works where broadcasts are filtered" in the README roadmap

## 4. Real hardware

- [ ] 4.1 From the WSL2 machine with default NAT networking, run `ONKYO_DISCOVERY_ADDR=<TX-NR7100 ip>,<TX-NR6050 ip> mcp-server-onkyo --discover --debug`; verify both receivers are listed in one run, and record the result in CLAUDE.md's hardware notes

## Workflow follow-up

- Run `/opsx:archive` once all tasks are done, to merge the discovery delta into `openspec/specs/discovery/spec.md`
