# Design

## Context

`discover()` opens one UDP socket on an ephemeral port, sends `!xECNQSTN` to
`DISCOVERY_ADDR`, then sleeps for the window while `datagram_received` collects
replies keyed by source IP. Unicast already works through the same code path (the
README's troubleshooting section uses `ONKYO_DISCOVERY_ADDR=<receiver ip>`), so the
change is about sending to several destinations from that one socket.

## Goals / Non-Goals

**Goals:**
- Query several addresses in one window, so discovery takes no longer with a list.
- Keep a single-address setting byte-for-byte compatible.

**Non-Goals:**
- Hostnames (mDNS or DNS) in the list: resolving them could block or stall the
  window. IP addresses only.
- Scanning a subnet (e.g. querying every address in a /24).
- A `discover_receivers` argument for addresses chosen by the model. Configuration
  stays with the user; this can be a later change.

## Decisions

**Extend `ONKYO_DISCOVERY_ADDR` rather than add a new variable.**
Alternatives: a new `ONKYO_DISCOVERY_HOSTS` that is queried in addition to the
broadcast, or reusing `ONKYO_HOST`. Extending the existing variable keeps one place
to say "where discovery looks", and a single value already means unicast today. The
cost is that listing receivers replaces the broadcast unless the user also lists
`255.255.255.255`; the README will show both forms.

**One socket, one window, many `sendto` calls.**
Replies come back to the same ephemeral port and are already de-duplicated by IP, so
no new bookkeeping is needed. Sending sequentially before the sleep keeps the total
time at one window regardless of list length.

**Validate entries with `ipaddress.ip_address`; skip bad ones with a warning.**
A typo in one entry shouldn't hide the receivers the other entries would find. The
warning goes through the module logger, which writes to stderr, keeping stdout clean
for JSON-RPC. `sendto` errors (`OSError`) for an otherwise valid address are handled
the same way.

**Testing with two fakes on 127.0.0.1 and 127.0.0.2.**
Both must listen on the same port, because discovery sends to `ONKYO_PORT` on every
address. Linux routes all of 127.0.0.0/8 to loopback, so this works in CI
(ubuntu-latest). `fake_receiver.start()` gains a `model` argument so the two fakes
report different models and the sort order can be checked.

## Risks / Trade-offs

- [Listing IPs drops the broadcast] → document `255.255.255.255,<ip>,...` in the
  README config table and troubleshooting section.
- [127.0.0.2 doesn't exist on macOS by default] → mark the two-fake test to skip
  when binding 127.0.0.2 fails, rather than failing.
- [DHCP changes a receiver's IP] → README already recommends DHCP reservations;
  repeat it next to the list example.
