#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against the simulator. Proves the dependencies install on musl, the
# install layout and permissions, and that OpenRC starts and stops it.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation (the job log isn't always at
# hand; annotations are), with the end of the service log if there is one.
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/var/log/mcp-server-onkyo/server.log}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='pytest on musl'
# The full test suite on musl, from the installed venv
/opt/mcp-server-onkyo/venv/bin/pip install --quiet "/src[dev]"
STEP_LOG=/tmp/pytest.log
cd /src
/opt/mcp-server-onkyo/venv/bin/pytest -q -p no:cacheprovider -rf >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

STEP='simulator'
# A fake receiver, and the service pointed at it
/opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo simulate --model TX-NR6050 --port 60128 --write-config /tmp/sim &
for _ in $(seq 50); do [ -e /tmp/sim/config.json ] && break; sleep 0.1; done
# install, not cp: busybox cp replaces the file with the source's mode (0644)
install -m 0640 -o root -g mcp-onkyo /tmp/sim/config.json /etc/mcp-server-onkyo/config.json

STEP='service start'
# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-onkyo start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8711/healthz && break; sleep 0.2; done
rc-service mcp-server-onkyo status

STEP='service user'
# The process runs as the service user, not root. (busybox ps truncates user
# names to 8 characters, so ask /proc for the owner of the server's process.)
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-onkyo serve' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-onkyo ]
STEP='file modes'
# The config is readable by the service and not by others
[ "$(stat -c %a /etc/mcp-server-onkyo/config.json)" = 640 ]
STEP='doctor'
# doctor as the service user, against the fake
su -s /bin/sh mcp-onkyo -c 'ONKYO_CONFIG_DIR=/etc/mcp-server-onkyo /opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo doctor --timeout 2'

STEP='service stop'
rc-service mcp-server-onkyo stop
cat /var/log/mcp-server-onkyo/server.log
echo "Alpine smoke test passed"
