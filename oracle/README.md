# Oracle Cloud VM (Always Free)

A second, public-facing server next to the home server: an Oracle Cloud **Always Free** ARM VM (2 OCPU, 12 GB RAM, 50 GB disk, Ubuntu 24.04 aarch64). It hosts what has to stay up or face the internet (AdGuard DNS-over-TLS for the phone, the public NPM front door) while the home server keeps the data. Compose files live under [`docker/`](docker/), which mirrors `/opt/docker/` on the VM.

## Security layout

```
 Internet ─► ① Oracle security list: TCP 80, 443, 853 + UDP 41641 (Tailscale) only. No SSH.
          ─► ② VM firewall (iptables): SSH only on tailscale0; CrowdSec bans first,
               then 80/443 only from DE and KW (GeoIP)
          ─► ③ Docker: NPM 80/443 public; every admin UI bound to the Tailscale IP only
 Tailscale ─► ④ ACL: the VM is tag:oracle and may reach nothing in the tailnet;
                 member devices (PC, phone, home server) may reach it
```

- **Cost guards (Oracle account):** quota policy `free-tier-only` caps everything at Always Free amounts (A1 2 OCPU / 12 GB, block storage 200 GB in the VM's AD and 0 in the others, no reserved public IPs, no paid services) and a budget `zero-cost-guard` (EUR 1, alerts at 1 % actual and forecast). Oracle halved the free A1 allowance on 2026-06-15 to 2 OCPU / 12 GB; the console's banner still shows the old 4 / 24.
- **Least privilege:** the CLI runs as an API-key-only user `cli-bot` (group `homelab-ops`) that may manage only the `homelab` compartment and read costs. The admin account uses MFA and is kept out of day-to-day work.
- **SSH:** key only, user `ubuntu`, no root, no X11, reachable over Tailscale only. One key per device.

## AdGuard Home (DNS-over-TLS)

[`docker/adguardhome`](docker/adguardhome/) is a copy of the home server's AdGuard, so the phone can use either one with the same Private DNS name. The config was copied from home with three changes: web UI on port 80 inside the container, HTTPS UI off and DNS-over-QUIC off (Oracle only lets TCP 853 in).

- **Bridge network, not host.** Only the published ports exist: DoT `853` public; plain DNS `53` and the web UI `8880` on the Tailscale IP only.
- **Not an open resolver.** `allowed_clients` accepts only the phone's ClientID (the first label of the DoT hostname) plus private and Tailscale ranges, which cannot arrive from the internet. Tested from outside: the phone's ClientID resolves and blocks ads, while no ClientID or a wrong one gets `REFUSED`.
- **Certificate.** The `cert-sync` sidecar copies NPM's wildcard cert (`npm-3` here) every hour; `cert-reload.sh` restarts AdGuard at 02:30 if the served cert differs.
- **Settings follow home.** The `adguardhome-sync` service in the home [AdGuard stack](../docker/adguardhome/) copies home's settings to this AdGuard every hour, except TLS and DHCP. Change settings at home only.
- **Phone failover.** The phone's DoT name (`<ClientID>.yourdomain.dedyn.io`) has its own A record pointing to this VM, with a 60 s TTL. It is set through deSEC's DynDNS interface, because normal records need at least 900 s. Every minute [`scripts/dns-failover.py`](../docker/scripts/dns-failover.py) on the home server checks this AdGuard over DoT with the phone's ClientID and reports to Uptime Kuma. After 3 failures it deletes that record, so the wildcard CNAME sends the phone home. Switching back is manual for now (`dns-failover.py --to-vm`, only if the VM answers), until the automatic way back has been tested. It never switches while home has no internet or the home AdGuard is down.

## Public web protection (CrowdSec + GeoIP)

Both block at the firewall, before a request reaches NPM. Neither touches SSH (Tailscale only), and GeoIP leaves DoT `853` and Tailscale open worldwide.

- **CrowdSec** ([`docker/crowdsec`](docker/crowdsec/)) reads NPM's logs. Proxy hosts log in NPM format, while the default site and fallback log in plain nginx format, so [`acquis.yaml`](docker/crowdsec/acquis.yaml) labels them separately. It detects scans, brute force and known CVE probes, and adds the community blocklist. The host package `crowdsec-firewall-bouncer-iptables` (CrowdSec's apt repository) enforces bans as the first rule in `INPUT` and `DOCKER-USER`. An allowlist `homelab` holds the home public IP and `100.64.0.0/10`, so we can't ban ourselves.
- **GeoIP** ([`host/geoip-allow.sh`](host/geoip-allow.sh) with its systemd units) is an `ipset` of German and Kuwaiti networks from ipdeny.com. In `DOCKER-USER`, ports 80/443 on the public interface accept only those. Certificates use DNS-01, so Let's Encrypt never needs port 80. The script refuses to apply a short or empty list, so a failed download can't block everyone. `geoip-allow.sh off` removes the rule.

## Admin links (Tailscale only)

The admin UIs also have names with the wildcard certificate, like the home apps: `oracle-adguard`, `oracle-uptimekuma` and `oracle-nginx` under `yourdomain.dedyn.io`. Two locks keep them off the internet:

- **DNS:** AdGuard rewrites (set at home, synced) point the names at the VM's Tailscale IP, so only Tailscale devices reach them.
- **NPM access list "Tailscale only":** allows `100.64.0.0/10` and denies everyone else, in case someone sends the name to the public IP. Tested from outside: `403`. Plain HTTP only redirects to HTTPS.

AdGuard and Kuma join `proxy-net` for this; their direct Tailscale ports stay as a fallback. CrowdSec labels NPM's proxy-host logs `nginx-proxy-manager` (its parser filters on that name) and uses `force_inotify` so it also watches log files of hosts created later.

## Uptime Kuma (watching home)

[`docker/uptime-kuma`](docker/uptime-kuma/) watches the home server from outside, so an alert still goes out when the home internet is down. It started as a copy of the home Kuma database, giving the same login and Discord alert, with the home monitors and history removed. It has two monitors:

- **Home internet (DoT 853)**: TCP check of the home public DoT port every 60 s (home internet, router forward, home AdGuard).
- **Home server heartbeat**: a push the home server sends every minute from its crontab, with the token in the cron line. Silence means the server is off or offline.

The UI is on `<TAILSCALE-IP>:3001`, over Tailscale only.

## Dockhand (Hawser agent)

The home Dockhand manages this VM through [Hawser](docker/hawser/) in standard mode: home connects to `<TAILSCALE-IP>:2376` with a token, and the VM never connects to home.

- **Where the compose files live.** Dockhand reads them on the home server, in `/opt/docker/oracle/<app>/` (same files as [`docker/`](docker/) here). On *Save & redeploy*, Hawser writes the compose file to `/opt/docker/<app>/` on the VM and runs it there, so relative paths like `./conf` stay on the VM. Tested with a throwaway stack: existing data and the other stacks were untouched.
- **Keep both copies identical.** Adopting changes nothing; the first redeploy overwrites the VM's compose file with the home copy.
- **Removing a stack** in Dockhand only stops its containers and forgets it; files stay.
- **Hawser itself stays untracked**: redeploying the agent through itself would cut its own connection, and its token would need a copy at home. Update it over SSH.
- **Not behind NPM.** The UI stays on the Tailscale IP. Here NPM is the public front door, so anything behind it would face the internet.

## Scheduled jobs (Oracle VM)

All times Europe/Berlin, read from the VM's timers and crontab. The slots match the home server ([schedule](../README.md#scheduled-jobs-home-server)): checks and cleanups at 01:00, cert reload 02:30, backup 03:15, upload 04:00. OS updates install only after the backup and upload, and a reboot comes last. Unlike the home server, OS updates install themselves, because nobody logs in here every week. Rows marked *planned* are added as services move in.

| When | Job | Runs from |
|---|---|---|
| 00:00 | Log rotation and package-database backup (Ubuntu defaults) | systemd |
| 01:00 | *planned:* Dockhand update check, as on the home server | Dockhand, through its agent |
| Mon 01:00 | Docker cleanup: images and build cache unused for 7 days (same slot as Dockhand's prune at home) | user crontab |
| 02:00 | apt: download package lists | systemd `apt-daily.timer` |
| 02:30 | AdGuard cert reload: restarts AdGuard if NPM renewed the certificate (`adguardhome/cert-reload.sh`), as on the home server | user crontab |
| 03:15 | *planned:* backup, as on the home server | – |
| 04:00 | *planned:* upload of that backup to Google Drive, as on the home server | – |
| 04:15 | Unattended upgrades: Ubuntu security and regular updates; old kernels and unused packages removed | systemd `apt-daily-upgrade.timer` |
| 04:45 | Reboot, only if an update requires it | unattended-upgrades |
| Mon 01:10 | GeoIP: refresh the DE and KW country lists and re-apply the 80/443 rule | systemd `geoip-allow-update.timer` |
| Every 60 s | Uptime Kuma checks the home internet (public DoT port); the home server pushes a heartbeat every minute | Uptime Kuma, home crontab |
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
# AdGuard cert reload, 02:30 like at home: restarts AdGuard if NPM renewed the certificate
30 2 * * * /opt/docker/adguardhome/cert-reload.sh >> /opt/docker/adguardhome/cert-reload.log 2>&1
```

`/etc/cloud/cloud.cfg.d/99-homelab-hostname.cfg`: hostname `oracle-homelab` (same as its Tailscale name and the Oracle instance name), kept across reboots
```yaml
# Homelab: keep our own hostname (cloud-init would reset it to the Oracle name on boot)
preserve_hostname: true
hostname: oracle-homelab
fqdn: oracle-homelab
```

Also: `rpcbind` disabled and masked (it listened on port 111 for nothing), timezone Europe/Berlin, `tailscale set --auto-update=true`.

## Connecting

Tailscale on, then `ssh ubuntu@<TAILSCALE-IP>` with that device's own key. The public IP has no SSH.
