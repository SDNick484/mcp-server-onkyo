#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against the simulator. Proves the dependencies install on musl, the
# install layout and permissions, and that OpenRC starts and stops it.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

# The full test suite on musl, from the installed venv
/opt/mcp-server-onkyo/venv/bin/pip install --quiet "/src[dev]"
cd /src && /opt/mcp-server-onkyo/venv/bin/pytest -q -p no:cacheprovider && cd /

# A fake receiver, and the service pointed at it
/opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo simulate --model TX-NR6050 --port 60128 --write-config /tmp/sim &
for _ in $(seq 50); do [ -e /tmp/sim/config.json ] && break; sleep 0.1; done
cp /tmp/sim/config.json /etc/mcp-server-onkyo/config.json
chown root:mcp-onkyo /etc/mcp-server-onkyo/config.json

# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-onkyo start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8711/healthz && break; sleep 0.2; done
rc-service mcp-server-onkyo status

# The process runs as the service user, not root (busybox has no pgrep -u with -f)
# shellcheck disable=SC2009
ps -o user,args | grep '[m]cp-server-onkyo serve' | grep -q '^mcp-onkyo'
# The config is readable by the service and not by others
[ "$(stat -c %a /etc/mcp-server-onkyo/config.json)" = 640 ]
# doctor as the service user, against the fake
su -s /bin/sh mcp-onkyo -c 'ONKYO_CONFIG_DIR=/etc/mcp-server-onkyo /opt/mcp-server-onkyo/venv/bin/mcp-server-onkyo doctor --timeout 2'

rc-service mcp-server-onkyo stop
cat /var/log/mcp-server-onkyo/server.log
echo "Alpine smoke test passed"
