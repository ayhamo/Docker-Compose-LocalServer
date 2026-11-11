#!/bin/sh
# radio-relay upkeep, run weekly from cron (Monday 05:15). The relay is built here (radio-relay:local),
# so nothing else updates it (Dockhand skips it via dockhand.update=false).
# - First Monday of the month: rebuild with --pull (newest Alpine packages: ffmpeg, Python), test, roll back on failure.
# - Other Mondays: only test the running relay.
# Test = health page, 8 s of every station listed in relay.py, and Navidrome reaching the relay by name.
# The first argument is the Uptime Kuma push URL (kept in the cron line, not here): up with versions, or down with the reason.
# Kuma push monitors can't wait a month (their interval must stay under ~24.8 days), hence the weekly run.
# Manual: RELAY_REBUILD=1 forces a rebuild; RELAY_TEST_FAIL=1 makes the test fail (to try the rollback).
set -u
cd "$(dirname "$0")" || exit 1                # the navidrome stack folder
PUSH=${1:-}
MODE=check
[ "$(date +%d)" -le 7 ] && MODE=rebuild
[ "${RELAY_REBUILD:-}" = 1 ] && MODE=rebuild

log() { echo "$(date '+%F %T') $*"; }
push() { [ -n "$PUSH" ] && curl -fsS -m 10 -G -o /dev/null "$PUSH" --data-urlencode "status=$1" --data-urlencode "msg=$2" || true; }
versions() { docker exec radio-relay sh -c 'echo "alpine $(cat /etc/alpine-release), ffmpeg $(ffmpeg -version | head -1 | cut -d" " -f3), $(python3 --version)"' 2>/dev/null; }

# Prints the problems and returns 1 if the relay doesn't work.
test_relay() {
  [ "${RELAY_TEST_FAIL:-}" = 1 ] && { echo "forced test failure (RELAY_TEST_FAIL=1)"; return 1; }
  [ "$(docker exec radio-relay wget -qO- -T 5 http://127.0.0.1:8080/health 2>/dev/null)" = ok ] || { echo "health page not ok"; return 1; }
  stations=$(docker exec radio-relay python3 -c 'import re; b = re.search(r"STATIONS = \{(.*?)\n\}", open("/relay.py").read(), re.S).group(1); print(" ".join(re.findall(r"^\s*\"([^\"]+)\"\s*:", b, re.M)))')
  [ -n "$stations" ] || { echo "no stations found in relay.py"; return 1; }
  bad=""
  for s in $stations; do
    bytes=$(docker exec radio-relay sh -c "timeout 8 wget -qO- http://127.0.0.1:8080/$s.mp3 2>/dev/null | wc -c" 2>/dev/null)
    [ "${bytes:-0}" -ge 64000 ] || bad="$bad $s(${bytes:-0} B)"   # 128 kbit/s for 8 s is ~128 KB
  done
  [ -z "$bad" ] || { echo "stations not streaming:$bad"; return 1; }
  [ "$(docker exec navidrome wget -qO- -T 5 http://radio-relay:8080/health 2>/dev/null)" = ok ] || { echo "Navidrome can't reach radio-relay"; return 1; }
  return 0
}

if [ "$MODE" = check ]; then
  if why=$(test_relay); then
    log "check OK ($(versions))"; push up "check OK: $(versions)"
  else
    log "check FAILED: $why"; push down "check failed: $why"; exit 1
  fi
  exit 0
fi

# Rebuild: keep the current image as radio-relay:rollback until the new one passes.
OLD=$(docker image inspect radio-relay:local --format '{{.Id}}')
BEFORE=$(versions)
docker tag radio-relay:local radio-relay:rollback
# --force-recreate: with the same tag, compose would otherwise keep the container on the old image
if docker compose build -q --pull radio-relay >/dev/null 2>&1 && docker compose up -d --no-build --force-recreate radio-relay >/dev/null 2>&1; then
  sleep 8
  NEW=$(docker image inspect radio-relay:local --format '{{.Id}}')
  if [ "$(docker inspect radio-relay --format '{{.Image}}')" != "$NEW" ]; then
    why="container did not switch to the new image"; ok=0
  else
    why=$(test_relay) && ok=1 || ok=0
  fi
else
  why="build or start failed"; ok=0
  NEW=$(docker image inspect radio-relay:local --format '{{.Id}}')
fi

if [ $ok = 1 ]; then
  docker rmi radio-relay:rollback >/dev/null
  [ "$NEW" != "$OLD" ] && docker rmi "$OLD" >/dev/null 2>&1   # the old image, now unused
  log "rebuilt OK: $BEFORE -> $(versions)"; push up "rebuilt: $(versions)"
else
  docker tag radio-relay:rollback radio-relay:local && docker rmi radio-relay:rollback >/dev/null
  docker compose up -d --no-build --force-recreate radio-relay >/dev/null 2>&1
  sleep 8
  [ "$NEW" != "$OLD" ] && docker rmi "$NEW" >/dev/null 2>&1   # the failed new build
  if after=$(test_relay); then state="old image works again"; else state="old image ALSO fails: $after"; fi
  log "rebuild FAILED ($why), rolled back; $state"; push down "rebuild failed ($why), rolled back; $state"; exit 1
fi
