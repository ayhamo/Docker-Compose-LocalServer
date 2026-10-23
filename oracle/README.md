# Oracle Cloud VM (Always Free)

A second, public-facing server next to the home server: an Oracle Cloud **Always Free** ARM VM (2 OCPU, 12 GB RAM, 50 GB disk, Ubuntu 24.04 aarch64). It hosts what has to stay up or face the internet (AdGuard DNS-over-TLS for the phone, the public NPM front door) while the home server keeps the data. Compose files live under [`docker/`](docker/), which mirrors `/opt/docker/` on the VM.

## Security layout

```
 Internet ─► ① Oracle security list: TCP 80, 443, 853 + UDP 41641 (Tailscale) only. No SSH.
          ─► ② VM firewall (iptables): SSH only on tailscale0
          ─► ③ Docker: NPM 80/443 public; every admin UI bound to the Tailscale IP only
 Tailscale ─► ④ ACL: the VM is tag:oracle and may reach nothing in the tailnet;
                 member devices (PC, phone, home server) may reach it
```

- **Cost guards (Oracle account):** quota policy `free-tier-only` caps everything at Always Free amounts (A1 2 OCPU / 12 GB, block storage 200 GB in the VM's AD and 0 in the others, no reserved public IPs, no paid services) and a budget `zero-cost-guard` (EUR 1, alerts at 1 % actual and forecast). Oracle halved the free A1 allowance on 2026-06-15 to 2 OCPU / 12 GB; the console's banner still shows the old 4 / 24.
- **Least privilege:** the CLI runs as an API-key-only user `cli-bot` (group `homelab-ops`) that may manage only the `homelab` compartment and read costs. The admin account uses MFA and is kept out of day-to-day work.
- **SSH:** key only, user `ubuntu`, no root, no X11, reachable over Tailscale only. One key per device.

## Scheduled jobs (Oracle VM)

All times Europe/Berlin, read from the VM's timers and crontab. The slots match the home server ([schedule](../README.md#scheduled-jobs-home-server)): checks and cleanups at 01:00, cert reload 02:30, backup 03:15, upload 04:00. OS updates install only after the backup and upload, and a reboot comes last. Unlike the home server, OS updates install themselves, because nobody logs in here every week. Rows marked *planned* are added as services move in.

| When | Job | Runs from |
|---|---|---|
| 00:00 | Log rotation and package-database backup (Ubuntu defaults) | systemd |
| 01:00 | *planned:* Dockhand update check, as on the home server | Dockhand, through its agent |
| Mon 01:00 | Docker cleanup: images and build cache unused for 7 days (same slot as Dockhand's prune at home) | user crontab |
| 02:00 | apt: download package lists | systemd `apt-daily.timer` |
| 02:30 | *planned:* AdGuard cert reload, as on the home server | user crontab |
| 03:15 | *planned:* backup, as on the home server | – |
| 04:00 | *planned:* upload of that backup to Google Drive, as on the home server | – |
| 04:15 | Unattended upgrades: Ubuntu security and regular updates; old kernels and unused packages removed | systemd `apt-daily-upgrade.timer` |
| 04:45 | Reboot, only if an update requires it | unattended-upgrades |
| Weekly (Mon, random time) | SSD trim | systemd `fstrim.timer` |
| Continuous | Tailscale updates itself; NPM renews its certificates | Tailscale, NPM |

## Host configuration

`/etc/ssh/sshd_config.d/10-hardening.conf`
```
# Homelab hardening: key-only login for ubuntu, no root, no X11
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
AllowUsers ubuntu
```

`/etc/apt/apt.conf.d/52homelab-unattended`
```
// Homelab: also install normal Ubuntu updates automatically (not only security),
// remove unused kernels/deps, and reboot at 04:45 when an update needs it.
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-updates";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:45";
```

`/etc/systemd/system/apt-daily.timer.d/override.conf` (and the same for `apt-daily-upgrade.timer` with `04:15`)
```
[Timer]
# Homelab: fixed nightly schedule
OnCalendar=
OnCalendar=*-*-* 02:00
RandomizedDelaySec=0
```

`/etc/systemd/journald.conf.d/size.conf`
```
[Journal]
# Homelab: keep system logs small
SystemMaxUse=200M
```

`/etc/sysctl.d/60-tailscale-bind.conf`: lets Docker bind admin UIs to the Tailscale IP even if Docker starts before Tailscale at boot.
```
net.ipv4.ip_nonlocal_bind = 1
```

`/etc/docker/daemon.json`
```json
{
  "log-driver": "local",
  "log-opts": { "max-size": "2m", "max-file": "2" },
  "no-new-privileges": true,
  "live-restore": true
}
```

`/etc/iptables/rules.v4`: changes to Oracle's image defaults
```diff
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
+A INPUT -i tailscale0 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
+A INPUT -p udp -m udp --dport 41641 -j ACCEPT
-A FORWARD -j REJECT --reject-with icmp-host-prohibited   # breaks Docker published ports
```

Crontab of `ubuntu`
```
# Weekly Docker cleanup (docker-cleanup), Monday 01:00 like Dockhand prune at home: images and build cache unused for 7 days
0 1 * * 1 docker image prune -af --filter until=168h >/dev/null && docker builder prune -af --filter until=168h >/dev/null
```

Also: `rpcbind` disabled and masked (it listened on port 111 for nothing), timezone Europe/Berlin, `tailscale set --auto-update=true`.

## Connecting

Tailscale on, then `ssh ubuntu@<TAILSCALE-IP>` with that device's own key. The public IP has no SSH.
