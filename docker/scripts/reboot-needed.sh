#!/bin/sh
# Tells Uptime Kuma whether Debian wants a reboot (unattended-upgrades never reboots by itself here).
# Root cron daily at 07:30 (after the 06:00-07:00 security updates) and once at boot.
# Kuma monitor "Home reboot needed": down = reboot needed (Discord alert with the packages), up = nothing pending.
# The first argument is the Kuma push URL (kept in the cron line). REBOOT_FILE overrides the flag file (for testing).
F=${REBOOT_FILE:-/var/run/reboot-required}
if [ -f "$F" ]; then
  pkgs=$(sort -u "$F.pkgs" 2>/dev/null | paste -sd ' ' -)
  status=down
  msg="Reboot needed since $(date -r "$F" '+%a %d %b %H:%M') for: ${pkgs:-unknown packages}. Reboot the server"
else
  status=up
  msg="No reboot needed (up since $(uptime -s))"
fi
curl -fsS -m 10 -G -o /dev/null "$1" --data-urlencode "status=$status" --data-urlencode "msg=$msg"
