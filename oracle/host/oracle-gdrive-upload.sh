#!/bin/sh
# Root cron 04:00: Backrest repo -> Google Drive, only once tonight's DB copies (03:00) and Backrest
# snapshot (03:15) exist. Pushes "up" to Kuma ("Oracle Backup → GDrive") on success; otherwise logs
# why and pushes "down" with that reason, so a skipped night alerts at once instead of after 25 h.
# $1 = the Kuma push URL (the token stays in the crontab line).
PUSH="$1"
LOG=/var/log/cron-logs/rclone-oracle-backup.log

fail() {
    echo "$(date '+%Y/%m/%d %H:%M:%S') SKIP: $1" >> "$LOG"
    curl -s -G "$PUSH" --data-urlencode "status=down" --data-urlencode "msg=$1" > /dev/null
    exit 1
}

n=$(find /opt/docker -name '*.snapshot' -mmin -120 | wc -l)
[ "$n" -ge 6 ] || fail "only $n of 6 DB copies from 03:00 (see oracle-db-snapshots.log)"
[ -n "$(find /opt/docker/backrest/data/oracle-backup/snapshots -type f -mmin -120)" ] || fail "no Backrest snapshot from 03:15"
rclone sync /opt/docker/backrest/data/oracle-backup "oracle-gdrive:" --log-file "$LOG" --log-level INFO \
    || fail "rclone sync failed (exit $?)"
curl -s "$PUSH?status=up&msg=OK" > /dev/null
