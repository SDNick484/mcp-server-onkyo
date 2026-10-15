# Proposal: Unicast discovery for networks that filter broadcasts

## Why

Discovery only works when a UDP broadcast reaches the receivers. On some setups it
doesn't (WSL2 with NAT networking, mesh Wi-Fi, client isolation), so
`discover_receivers` returns nothing and the default-receiver fallback fails, even
though both receivers answer a query sent straight to their IP. Today
`ONKYO_DISCOVERY_ADDR` can name one address, which covers a single receiver but not
the owner's two (TX-NR7100 and TX-NR6050).

## What Changes

- `ONKYO_DISCOVERY_ADDR` accepts a comma-separated list of addresses. Each can be a
  receiver's IP (unicast) or a broadcast address, and all of them are queried in the
  same discovery window. A single address keeps working exactly as today.
- An address that can't be used (not an IP, unreachable) is skipped with a warning
  on stderr instead of aborting discovery.
- Results are still one entry per receiver IP, so a receiver reached both by
  broadcast and by its own address appears once.
- README: config table, troubleshooting section and roadmap updated.

Not breaking: existing single-address and default configurations behave the same.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `discovery`: the discovery query is sent to every configured address, not just
  one, and a bad address no longer stops discovery.

## Impact

- `onkyo_mcp.py`: `discover()` and the `DISCOVERY_ADDR` setting.
- `fake_receiver.py` / `tests/`: tests with two fake receivers on different
  loopback addresses.
- `README.md`: configuration table, "When discovery finds nothing", roadmap.
- The default-receiver fallback (`resolve_host`) and `--discover` benefit with no
  changes of their own.
