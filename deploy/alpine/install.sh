#!/bin/sh
# Install mcp-server-onkyo as an OpenRC service on Alpine (e.g. a Proxmox LXC).
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# Layout (each part owned so the service user can read config, write state, and nothing else):
#   /opt/mcp-server-onkyo/venv     the code              root:root        0755
#   /opt/mcp-server-onkyo/run.sh   the service command   root:root        0755
#   /etc/mcp-server-onkyo/         config.json, env      root:mcp-onkyo   0750 / files 0640
#   /var/lib/mcp-server-onkyo/     state (none yet)      mcp-onkyo        0750
#   /var/log/mcp-server-onkyo/     server.log            mcp-onkyo        0750
#   /etc/init.d/mcp-server-onkyo   the OpenRC script
# Re-running upgrades the code and keeps config and state.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-onkyo}"
USER_NAME=mcp-onkyo
PREFIX=/opt/mcp-server-onkyo
ETC=/etc/mcp-server-onkyo
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; every compiled dependency ships a musl wheel,
# so no compiler is needed (see README: Alpine).
apk add --no-cache python3 py3-pip openrc >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h /var/lib/mcp-server-onkyo -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
[ -e "$ETC/config.json" ] || {
	printf '{\n  "receivers": [],\n  "max_volume": {"main": 75, "zone2": 60}\n}\n' >"$ETC/config.json"
	chown root:"$USER_NAME" "$ETC/config.json"
	chmod 0640 "$ETC/config.json"
}
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/lib/mcp-server-onkyo /var/log/mcp-server-onkyo
install -m 0755 "$HERE/mcp-server-onkyo.initd" /etc/init.d/mcp-server-onkyo

echo "Installed $("$PREFIX/venv/bin/mcp-server-onkyo" --version)."
echo "Next: add receivers to $ETC/config.json, review $ETC/env, then:"
echo "  su -s /bin/sh $USER_NAME -c 'ONKYO_CONFIG_DIR=$ETC $PREFIX/venv/bin/mcp-server-onkyo doctor'"
echo "  rc-update add mcp-server-onkyo default && rc-service mcp-server-onkyo start"
