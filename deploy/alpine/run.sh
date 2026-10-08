#!/bin/sh
# What the OpenRC service runs (as user mcp-onkyo). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. Run it by hand to see exactly what the service does:
#   su -s /bin/sh mcp-onkyo -c /opt/mcp-server-onkyo/run.sh
set -eu
ENV_FILE="${MCP_ONKYO_ENV:-/etc/mcp-server-onkyo/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export ONKYO_CONFIG_DIR="${ONKYO_CONFIG_DIR:-/etc/mcp-server-onkyo}"
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec /opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo serve --http ${MCP_SERVE_ARGS:-}
