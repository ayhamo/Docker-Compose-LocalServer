#!/bin/bash
# Nightly Sure database dump (pg_dump), picked up by Backrest at 03:15.
# Keeps the last 7 days. Run from ayham crontab at 02:50.
set -euo pipefail
dir=/opt/docker/sure/backups
file="$dir/sure-$(date +%F).dump"
mkdir -p "$dir"
docker exec sure-db pg_dump -U sure_user -Fc sure_production > "$file.tmp"
# A valid custom-format dump lists its contents; refuse to keep a broken one.
docker exec -i sure-db pg_restore --list < "$file.tmp" > /dev/null
mv "$file.tmp" "$file"
find "$dir" -name "sure-*.dump" -mtime +7 -delete
if [ -s "$dir/.kuma-push-url" ]; then
  curl -fsS -m 10 "$(cat "$dir/.kuma-push-url")?status=up&msg=OK" > /dev/null || true
fi
