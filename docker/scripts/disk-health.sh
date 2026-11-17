#!/bin/sh
# Tells Uptime Kuma whether the disks look healthy. Root cron, daily at 05:10.
# Kuma monitor "Home disk health": down = a problem (Discord alert with the details), up = all fine.
# Checks: SMART overall health and the early-failure counters of every disk, NVMe wear,
# and the btrfs error counters of the root filesystem (read/write/checksum errors).
# The first argument is the Kuma push URL (kept in the cron line).
problems=""
summary=""
add() { problems="${problems:+$problems; }$1"; }

for dev in $(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}'); do
  name=${dev#/dev/}
  out=$(smartctl -H -A "$dev" 2>&1)
  echo "$out" | grep -qE "PASSED|SMART Health Status: OK" || add "$name: SMART health not PASSED"
  case $name in
    nvme*)
      crit=$(echo "$out" | awk -F: '/Critical Warning/{gsub(/ /,"",$2); print $2}')
      media=$(echo "$out" | awk -F: '/Media and Data Integrity Errors/{gsub(/[ ,]/,"",$2); print $2}')
      used=$(echo "$out" | awk -F: '/Percentage Used/{gsub(/[ %]/,"",$2); print $2}')
      [ "${crit:-0x00}" != "0x00" ] && add "$name: critical warning $crit"
      [ "${media:-0}" -gt 0 ] && add "$name: $media media errors"
      [ "${used:-0}" -ge 80 ] && add "$name: ${used}% worn"
      summary="$summary $name ${used}% worn,"
      ;;
    *)
      for attr in Reallocated_Sector_Ct Current_Pending_Sector Offline_Uncorrectable; do
        v=$(echo "$out" | awk -v a=$attr '$2==a{print $10}')
        [ "${v:-0}" -gt 0 ] && add "$name: $attr=$v"
      done
      summary="$summary $name ok,"
      ;;
  esac
done

errs=$(btrfs device stats / | awk '$2!=0' | paste -sd ' ' -)
[ -n "$errs" ] && add "btrfs errors: $errs"

if [ -n "$problems" ]; then
  status=down; msg="$problems"
else
  status=up; msg="Disks fine:${summary%,}, btrfs 0 errors"
fi
echo "$(date '+%F %T') $status $msg"
curl -fsS -m 10 -G -o /dev/null "$1" --data-urlencode "status=$status" --data-urlencode "msg=$msg"
