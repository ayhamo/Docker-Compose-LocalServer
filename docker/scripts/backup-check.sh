#!/bin/bash
# Morning check of the backups the apps make themselves (05:30, after the night's jobs).
# Each backup reports to its own Uptime Kuma push monitor, like the Sure dump does:
# up when a new file appeared in the last 26 hours, down otherwise.
# Push URLs live in .kuma-push-urls next to this script (not in git).
set -uo pipefail
cd "$(dirname "$0")"
max_age=$((26 * 3600))
now=$(date +%s)

check() {  # <monitor name> <glob of the backup files>
  local newest url status msg
  newest=$(ls -td $2 2>/dev/null | head -1)
  if [ -n "$newest" ] && [ $((now - $(stat -c %Y "$newest"))) -le $max_age ]; then
    status=up; msg="OK: $(basename "$newest")"
  else
    status=down; msg="No new backup in 26 h"
  fi
  url=$(grep -F "$1=" .kuma-push-urls 2>/dev/null | head -1 | cut -d= -f2-)
  echo "[$(date)] $1: $status ($msg)"
  if [ -n "$url" ]; then
    curl -fsS -m 10 -G "$url" --data-urlencode "status=$status" --data-urlencode "msg=$msg" >/dev/null || echo "[$(date)] $1: push failed"
  fi
}

check "Immich DB Dump"       "/opt/docker/immich/library/backups/immich-db-backup-*.sql.gz"
check "SparkyFitness Backup" "/opt/docker/sparky-fitness/backup/sparkyfitness_full_backup_*.tar.gz"
check "Floppy DB Snapshot"   "/opt/docker/floppy/backups/database/db-*.sqlite3"
check "Backrest Backup"      "/opt/docker/backrest/data/docker-backup/snapshots/*"
