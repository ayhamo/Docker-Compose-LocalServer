#!/bin/sh
# Nightly copy of the host's own setup into /opt/docker/host-config, so the 03:15 Backrest backup
# (and its 04:00 upload to Google Drive) also holds what lives outside /opt/docker:
#   etc.tar.gz        all of /etc (password hashes left out: shadow, gshadow and their backups)
#   crontabs/         every user's crontab, root's included
#   usr-local.tar.gz  /usr/local/bin and /usr/local/sbin (host scripts)
#   home-ssh/         per user: authorized_keys, known_hosts, config and public keys (no private keys)
#   packages/         manually installed packages, all package selections with versions, snaps
#   system/           OS, kernel, disks, mounts, network, enabled units and timers, Docker and Tailscale versions
# Same script on the home server and the Oracle VM. Runs as root from cron at 03:05.
# The folder is overwritten every night (root only, mode 700); Backrest keeps the history.
# The first argument is the Uptime Kuma push URL (kept in the cron line): up with a summary, or down with the reason.
set -u
umask 077
DEST=/opt/docker/host-config
TMP=$DEST.tmp
PUSH=${1:-}

push() { [ -n "$PUSH" ] && curl -fsS -m 10 -G -o /dev/null "$PUSH" --data-urlencode "status=$1" --data-urlencode "msg=$2" || true; }
fail() { echo "$(date '+%F %T') FAILED: $1"; rm -rf "$TMP"; push down "host config copy failed: $1"; exit 1; }

[ "$(id -u)" = 0 ] || fail "must run as root"
rm -rf "$TMP" && mkdir -p "$TMP/crontabs" "$TMP/home-ssh" "$TMP/packages" "$TMP/system" || fail "cannot create $TMP"

tar -C / -czf "$TMP/etc.tar.gz" --exclude='etc/shadow*' --exclude='etc/gshadow*' etc 2>"$TMP/etc.tar.err" \
  || [ $? = 1 ] || fail "tar of /etc: $(head -c 200 "$TMP/etc.tar.err")"   # 1 = a file changed while reading: fine
rm -f "$TMP/etc.tar.err"
tar -tzf "$TMP/etc.tar.gz" etc/fstab >/dev/null 2>&1 || fail "etc.tar.gz is unreadable"

for f in /var/spool/cron/crontabs/*; do [ -f "$f" ] && cp -p "$f" "$TMP/crontabs/"; done
[ -n "$(ls "$TMP/crontabs")" ] || fail "no crontabs found"

tar -C / -czf "$TMP/usr-local.tar.gz" usr/local/bin usr/local/sbin 2>/dev/null || fail "tar of /usr/local"

for h in /root /home/*; do
  [ -d "$h/.ssh" ] || continue
  u=$(basename "$h"); mkdir -p "$TMP/home-ssh/$u"
  for f in authorized_keys known_hosts config "$h"/.ssh/*.pub; do
    f=$(basename "$f"); [ -f "$h/.ssh/$f" ] && cp -p "$h/.ssh/$f" "$TMP/home-ssh/$u/"
  done
done

apt-mark showmanual > "$TMP/packages/apt-manual.txt" || fail "apt-mark"
dpkg --get-selections > "$TMP/packages/dpkg-selections.txt" || fail "dpkg --get-selections"
dpkg-query -W -f='${Package}\t${Version}\n' > "$TMP/packages/dpkg-versions.txt"
command -v snap >/dev/null && snap list > "$TMP/packages/snaps.txt" 2>&1

S=$TMP/system
{ date '+%F %T %Z'; hostname; uname -a; cat /etc/os-release; } > "$S/os.txt"
lsblk -f > "$S/disks.txt" 2>&1; df -hT -x tmpfs -x overlay > "$S/df.txt" 2>&1; { findmnt -n -l --real 2>/dev/null || findmnt -n -l | grep -v overlay; } > "$S/mounts.txt"
ip -br addr > "$S/network.txt" 2>&1; ip route >> "$S/network.txt" 2>&1
systemctl list-unit-files --state=enabled --no-pager > "$S/enabled-units.txt" 2>&1
systemctl list-timers --all --no-pager > "$S/timers.txt" 2>&1
{ docker version --format 'Docker {{.Server.Version}}'; docker compose version; docker info --format 'Root dir {{.DockerRootDir}}, storage {{.Driver}}, cgroup {{.CgroupVersion}}'; docker network ls; } > "$S/docker.txt" 2>&1
command -v tailscale >/dev/null && tailscale version > "$S/tailscale.txt" 2>&1

cat > "$TMP/README.txt" <<'EOF'
Host configuration copied by host-config-backup.sh (root cron 03:05) for the 03:15 Backrest backup.
Restore on a fresh install of the same OS:
  1. Install the packages in packages/apt-manual.txt (apt install $(cat apt-manual.txt)), plus Docker and Tailscale.
  2. Unpack etc.tar.gz somewhere (tar -xzf etc.tar.gz -C /tmp/old) and copy back what is needed:
     ssh hardening, apt/unattended config, systemd units and timer overrides, logrotate, journald, docker daemon.json, fstab (check disk UUIDs in system/disks.txt).
  3. Put the crontabs back: crontab -u <user> crontabs/<user>.
  4. Unpack usr-local.tar.gz into / (host scripts), and home-ssh/<user>/ into ~/.ssh (chmod 700/600).
  5. Restore /opt/docker from Backrest, then systemctl daemon-reload and enable the units listed in system/enabled-units.txt.
EOF

etc_kb=$(du -k "$TMP/etc.tar.gz" | cut -f1); ncron=$(ls "$TMP/crontabs" | wc -l); npkg=$(wc -l < "$TMP/packages/apt-manual.txt")
rm -rf "$DEST.old"; [ -d "$DEST" ] && mv "$DEST" "$DEST.old"
mv "$TMP" "$DEST" || { [ -d "$DEST.old" ] && mv "$DEST.old" "$DEST"; fail "cannot move $TMP into place"; }
rm -rf "$DEST.old"; chmod 700 "$DEST"

msg="etc ${etc_kb} KB, $ncron crontabs, $npkg manual packages"
echo "$(date '+%F %T') OK: $msg"
push up "$msg"
