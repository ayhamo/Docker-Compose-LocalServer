#!/bin/bash
# Nightly Wardrowbe database dump (pg_dump), picked up by Backrest at 03:15.
# Keeps the last 7 days. Run from ayham crontab at 02:55; the Uptime Kuma push URL is the first argument (in the cron line).
set -euo pipefail
dir=/opt/docker/wardrowbe/backups
file="$dir/wardrowbe-$(date +%F).dump"
mkdir -p "$dir"
docker exec wardrobe-db pg_dump -U wardrobe -Fc wardrobe > "$file.tmp"
# A valid custom-format dump lists its contents; refuse to keep a broken one.
docker exec -i wardrobe-db pg_restore --list < "$file.tmp" > /dev/null
mv "$file.tmp" "$file"
find "$dir" -name "wardrowbe-*.dump" -mtime +7 -delete
if [ -n "${1:-}" ]; then
  curl -fsS -m 10 "$1?status=up&msg=OK" > /dev/null || true
fi
