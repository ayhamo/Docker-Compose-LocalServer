# Docker-Compose-LocalServer

The Docker Compose files behind my self-hosted home server, shared so others can reuse them. Each app lives in its own folder under [`docker/`](docker/), which mirrors `/opt/docker/` on the server.

Setting up a fresh machine? Follow **[Tutorial for new server.md](Tutorial%20for%20new%20server.md)**. It installs everything in the right order without port conflicts.

## How it fits together

```
            LAN / Tailscale clients
                     │  DNS: *.yourdomain.dedyn.io → server IP   (AdGuard Home rewrite)
                     ▼
     ┌──────── Nginx Proxy Manager (80 / 443, admin 81) ────────┐
     │   one wildcard Let's Encrypt cert via deSEC DNS-01       │
     │   proxies to  <container_name>:<internal port>           │
     └───────────────────────┬──────────────────────────────────┘
                             │  external docker network: proxy-net
     immich · navidrome · sure · floppy · uptime-kuma · dockhand · …
```

- **No published ports for web apps.** Every app with a web UI has its `ports:` section commented out and joins the external `proxy-net` network. Nginx Proxy Manager (NPM) reaches it as `http://<container_name>:<internal port>`, then adds your domain and SSL on top.
- **A single wildcard certificate.** NPM requests `*.yourdomain.dedyn.io` from Let's Encrypt using a deSEC DNS challenge. AdGuard Home reuses that same certificate for DNS-over-TLS and its HTTPS UI. A small `cert-sync` sidecar copies it from NPM's `letsencrypt/` folder into `adguardhome/certs/` every hour, and `cert-reload.sh` (daily cron) restarts AdGuard when the cert it serves differs from the synced one.
- **Host-network apps.** AdGuard Home and GPTWOL use `network_mode: host` because they need raw DNS/ARP/WoL access. NPM reaches them by the server's LAN IP rather than a container name.
- **Tailscale clients keep their real IP in NPM.** Tailscale masquerades everything it forwards, and since its 28 Oct 2026 update its rules run before Docker's, so NPM saw every Tailscale client (the Oracle VM too) as the `proxy-net` gateway and the VM's per-IP limit in `http_top.conf` stopped matching. [`scripts/tailscale-docker-realip.service`](docker/scripts/tailscale-docker-realip.service) (in `/etc/systemd/system`, enabled) clears Tailscale's mark on packets going into Docker bridges at every boot, so those skip the masquerade whatever the rule order; exit-node and LAN-route traffic keep it.
- **Hardened containers.** Where an image allows it, services run with `no-new-privileges` and `cap_drop: ALL`, adding back only the capabilities they need (each block is commented in its compose). Uptime Kuma has no Docker socket mount.
- **Scheduled jobs.** Three host cron entries run before the nightly Backrest backup (03:15): `adguardhome/cert-reload.sh` at 02:30, `sure/backup-db.sh` (a `pg_dump` of Sure, 7 days kept) at 02:50 and `wardrowbe/backup-db.sh` (the same for Wardrowbe; its Uptime Kuma push URL is passed in the cron line) at 02:55. See the tutorial, step 6.
- **Root SSD on btrfs with zstd compression** (since 17 Nov 2026, converted from ext4). The SSD has one subvolume per area: `@` (/), `@home`, `@opt-docker`, `@containerd`, `@docker`, `@log` and `@swap`, all mounted with `compress=zstd:1,noatime`.
  - **Snapshots:** each area can be snapshotted and restored on its own, so one broken app can be rolled back without touching the rest. The image stores (`@containerd`, `@docker`) are left out of snapshots, because images can be pulled again.
  - **Space:** the data went from 60 GB to 40 GB.
  - **Swap:** an 8 GB swapfile in `@swap` (`/swap/swapfile`).
  - **Setup:** see the tutorial, step 1.0.
  - **Snapper:** automatic snapshots of `/` (7 daily, 4 weekly, plus one before and after every apt run and one at each boot) and `/opt/docker` (6 hourly, 7 daily, 4 weekly). Backrest excludes `/opt/docker/.snapshots`.
  - **Boot:** the Acer firmware starts the fallback loader `EFI\BOOT\grubx64.efi`, not `EFI\debian`. Debconf `grub2/force_efi_extra_removable` is set, so every GRUB update refreshes both copies.
- **Backups off the SSD.** The Backrest (restic) repository lives on the HDD at `/mnt/hdd/backrest-backup`, mounted into the container at `/data/docker-backup` (moved 17 Nov 2026). An SSD failure no longer takes the data and its backup together; Google Drive keeps the off-site copy.
- **Host tuning** (tutorial step 1.2a), tested 2026-11-17 including a reboot:
  - **zswap with zstd** in front of the swapfile ([`scripts/zswap.conf`](docker/scripts/zswap.conf) in `/etc/tmpfiles.d`).
  - **UDP GRO forwarding for Tailscale** ([`scripts/udp-gro-forwarding.service`](docker/scripts/udp-gro-forwarding.service), enabled), because home is an exit node and subnet router.
  - **No Docker `live-restore`** (tried on 2026-11-17, removed the same day): with it, containers survive a Docker update, but every container that mounts the Docker socket (Dockhand, Nextcloud AIO mastercontainer and HaRP) keeps the old, dead socket and needs a restart. Without it, a Docker update simply restarts all containers.
  - **TCP BBR** (`/etc/sysctl.d/90-bbr.conf`, `tcp_bbr` in `/etc/modules-load.d/bbr.conf`). Benchmarked 2026-11-17 in containers with a 100 ms round trip and a 50 Mbit/s limit, like the upload to Kuwait: the same speed as CUBIC on a clean path, 47 against 9 Mbit/s with 0.1% packet loss and 46 against 1.6 Mbit/s with 1%.
  - **`tcp_slow_start_after_idle = 0` and `tcp_notsent_lowat = 16384`** (`/etc/sysctl.d/90-tcp-tuning.conf`): connections keep their speed after a pause, and HTTP/2 can send urgent parts first. A container gets its own copy of these settings, so NPM, which sends every page to remote clients, sets them again under `sysctls:` in its compose file. BBR does carry into containers.
  - **AdGuard `upstream_mode: parallel`:** asks Cloudflare and Quad9 at once and uses the first answer (uncached lookups 21 ms on average instead of 25, worst case 35 ms instead of 67).
- **SSD writes, about 21 GB a day** (measured 2026-11-17). The biggest writer was Smart Plug, at 7–70 GB a day, until its SQLite fix (see [its folder](docker/smart-plug/)).
- **Updates and health alerts.** `unattended-upgrades` installs Debian security updates only and never reboots. [`scripts/reboot-needed.sh`](docker/scripts/reboot-needed.sh) tells the Kuma monitor "Home reboot needed" when a reboot is pending, and [`scripts/disk-health.sh`](docker/scripts/disk-health.sh) reports SMART health of both disks, NVMe wear and btrfs error counters to "Home disk health". Both are in `/usr/local/sbin`.
- **Global log rotation.** Logs are rotated for every container via `/etc/docker/daemon.json` (`local` driver, 2 × 2 MB).
- **No secrets in this repo.** Compose files only reference `${VAR}`; the values live in each stack's `.env` (mode 600, git-ignored), and Immich Drop's in its own `immich-drop.env` (Immich's `.env` is loaded into every Immich container). Copy `.env.example` → `.env` and fill it in. A few config files keep `<REDACTED_…>` placeholders.
- **Home Navidrome is a backup, home and Tailscale only.** The Oracle VM's Navidrome is the main one. Home Navidrome is not on the Oracle gateway (removed 2026-10-29), so it needs no proxy trust, header login or log shipping.

## Apps

| App | Folder | What it is | Reached via |
|---|---|---|---|
| [Nginx Proxy Manager](https://nginxproxymanager.com) | `nginx-proxy-manager` | Reverse proxy and SSL, plus [error-pages](https://github.com/tarampampam/error-pages) | host `80`, `443`, `81` |
| [AdGuard Home](https://adguard.com/adguard-home.html) | `adguardhome` | Network-wide DNS ad-blocking; DoT and HTTPS use NPM's cert. Includes [adguardhome-sync](https://github.com/bakito/adguardhome-sync) for the Oracle VM's AdGuard, paused behind the compose profile `oracle-replica` while that AdGuard is off | host network: DNS `53`, DoT `853`, UI `8880` / `8443` |
| [Dockhand](https://hub.docker.com/r/fnsys/dockhand) | `dockhand` | Web UI for managing the compose stacks | `dockhand:3000` |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | `uptime-kuma` | Uptime monitoring | `uptime-kuma:3001` |
| [Backrest](https://github.com/garethgeorge/backrest) | `backrest` | Restic backup UI, backs up `/opt/docker` read-only. The repository is on the HDD | `backrest:9898` |
| [Nextcloud AIO](https://github.com/nextcloud/all-in-one) | `nextcloud-aio` | Files, photos and office suite | AIO admin on host `8080`; Apache on host `11000` |
| [Immich](https://immich.app) | `immich` | Photo library with ML (OpenVINO), plus [immich-drop](https://github.com/nasogaa/immich-drop) for guest uploads | `immich_server:2283`, `immich_drop:8080` |
| [Navidrome](https://www.navidrome.org) | `navidrome` | Music streaming, plus [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) and `radio-relay` (re-streams HLS-only radio stations as MP3 for Navidrome's radio list, built locally) | `navidrome:4533`, `audiomuse-flask:8000`, `radio-relay:8080` |
| [Sure](https://github.com/we-promise/sure) | `sure` | Personal finance, behind the [Pipelock](https://github.com/luckyPipewrench/pipelock) egress proxy; `overrides/` holds a small Yahoo Finance fix mounted as an initializer | `sure-web:3000` |
| [SparkyFitness](https://github.com/CodeWithCJ/SparkyFitness) | `sparky-fitness` | Fitness and nutrition tracker | `sparkyfitness-frontend:80` |
| [Floppy](https://github.com/dannyvfilms/floppy) | `floppy` | Media tracker; custom AniList/SIMKL importers are mounted in | `floppy:8000` |
| [Wardrowbe](https://github.com/Anyesh/wardrowbe) | `wardrowbe` | Wardrobe photos with AI tagging (OpenRouter) and daily outfit suggestions; LAN/Tailscale only, NPM signs you in directly (forward-auth headers from `nginx-proxy-manager/data/nginx/custom/wardrowbe-auth.conf`) | `wardrobe-frontend:3000` |
| [Dynacat](https://github.com/panonim/dynacat) | `dynacat` | Dashboard / start page | `dynacat:8080` |
| [Last Glance](https://github.com/krelltunez/lastglance) | `lastglance` | "When did I last…" tracker | `lastglance:80` |
| [BentoPDF](https://github.com/alam00000/bentopdf) | `bentopdf` | Client-side PDF toolkit | `bentopdf:8080` |
| [SnapOtter](https://hub.docker.com/r/snapotter/snapotter) | `snapotter` | AI image editing (iGPU) | `snapotter:1349` |
| [GPTWOL](https://github.com/Misterbabou/gptwol) | `gptwol` | Wake-on-LAN web UI | host `5000` |
| Smart Plug | `smart-plug` | Local web UI for Tuya (LSC) smart plugs: switching, schedules, energy stats. Image built from [ayhamo/LSC-Smart-Plug](https://github.com/ayhamo/LSC-Smart-Plug) | `smart-plug:8080` |
| EVE.js | `evejs` | Private game server, deployed with `manage.sh` from a release zip | host `26000`–`26003`, `5222`, `40110`–`40111` |
| W101 server | `w101server` | Private game server ([Imlight](https://github.com/Revive101/Imlight)), built with `setup.sh` | host `12000`, `12333`, `12500` |

## Oracle Cloud VM

A second, public-facing server runs on Oracle Cloud's Always Free tier (ARM, 2 OCPU, 12 GB RAM). It hosts what must stay reachable from the internet: Pangolin (Traefik with Pocket ID passkey login) as the public gateway, which also publishes chosen home apps, while the data stays at home. Public DNS sends `*.yourdomain.dedyn.io` to the VM; the home AdGuard keeps sending home devices straight to the home server. SSH and every admin UI are reachable over Tailscale only. A Tailscale ACL lets the VM reach only NPM's port 443 at home, and NPM answers the VM with `403` for every app not on its published list ([`http_top.conf`](docker/nginx-proxy-manager/data/nginx/custom/http_top.conf)). Setup, security layers and host configuration are in **[oracle/README.md](oracle/README.md)**.

## Scheduled jobs (home server)

All times Europe/Berlin, checked against the jobs' real run times. Apps that schedule in UTC run one hour earlier on the clock in winter (shown in brackets). Every app dump finishes before Backrest at 03:15, so Backrest always picks up last night's copies. The Oracle VM has its own schedule in [oracle/README.md](oracle/README.md).

| When | Job | Runs from |
|---|---|---|
| Every 30 min (:13, :43) | deSEC DynDNS update of the home IP, with an Uptime Kuma heartbeat | root crontab |
| Every minute | Oracle gateway check: opens Pangolin's dashboard on the VM and pushes to the home Kuma monitor "Oracle gateway (Pangolin)" (replaced the phone DNS failover, `scripts/dns-failover.py`, kept on disk) | user crontab |
| Every minute | Heartbeat to the Uptime Kuma on the Oracle VM (silence there = home down) | user crontab |
| 00:00 Mon–Fri (23:00 Sun–Thu) | Sure: stock price import | Sure (UTC) |
| 00:00 on the 1st | Backrest prune and check | Backrest |
| 01:00 | Dockhand: check all containers for updates (no auto-install) and cleanups; Mondays also prune unused images | Dockhand |
| 01:10 (00:10) | SparkyFitness full backup | SparkyFitness (UTC) |
| 01:30 | Floppy: AniList and SIMKL import | Floppy |
| 02:30 | AdGuard cert reload: restarts AdGuard if NPM renewed the certificate (`adguardhome/cert-reload.sh`) | user crontab |
| 02:37 | Floppy database snapshot, 7 kept (`floppy/backups/database/`) | Floppy |
| 02:40 (01:40) | Immich database dump, 7 kept (`immich/library/backups/`) | Immich (UTC) |
| 02:50 | Sure database dump, 7 days kept (`sure/backup-db.sh`) | user crontab |
| 02:55 | Wardrowbe database dump, 7 days kept (`wardrowbe/backup-db.sh`) | user crontab |
| 03:05 | Host config copy: all of `/etc` (no password hashes), every crontab, `/usr/local` scripts, public SSH keys, package lists and a system summary into `/opt/docker/host-config` (root only), so Backrest backs up the host's setup too; pushes to the Kuma monitor "Host config copy" ([`scripts/host-config-backup.sh`](docker/scripts/host-config-backup.sh), installed in `/usr/local/sbin`) | root crontab |
| 03:15 | Backrest backup of `/opt/docker` (7 daily, 4 weekly, 6 monthly) | Backrest |
| 04:00 | rclone: Backrest repository (`/mnt/hdd/backrest-backup`) → Google Drive | root crontab |
| 04:20 (03:20) | Nextcloud AIO backup to `/mnt/hdd/nextcloud-backup` | Nextcloud AIO (UTC) |
| 05:00 | rclone: Nextcloud AIO backup → Google Drive | root crontab |
| 05:10 | Disk health: SMART, NVMe wear and btrfs errors → Kuma monitor "Home disk health" ([`scripts/disk-health.sh`](docker/scripts/disk-health.sh)) | root crontab |
| Mon 05:15 | radio-relay: tests the relay (health, every station, Navidrome → relay); on the first Monday of the month it first rebuilds it with `--pull` (newest Alpine, ffmpeg, Python) and rolls back if the test fails. Pushes to the Kuma monitor "Radio relay check" (8-day interval) ([`navidrome/radio-relay-rebuild.sh`](docker/navidrome/radio-relay-rebuild.sh)) | user crontab |
| 05:30 | Backup check: Immich, SparkyFitness and Floppy dumps, the Backrest snapshot and the Nextcloud AIO backup must be under 26 h old; each reports to its own Uptime Kuma push monitor (`scripts/backup-check.sh`) | user crontab |
| 06:00–07:00 | Debian security updates (`unattended-upgrades`, no reboot, with a Snapper snapshot before and after) | apt timer |
| 07:30 and at boot | Reboot check → Kuma monitor "Home reboot needed" ([`scripts/reboot-needed.sh`](docker/scripts/reboot-needed.sh)) | root crontab |
| Weekly | `apt upgrade` for everything else | by hand |

## Things you must adapt

- **Domain.** Use your own instead of `yourdomain.dedyn.io`.
- **Server IP.** Defaults to `192.168.0.214`. It appears in `/etc/docker/daemon.json`, in `w101server/setup.sh` and in the NPM hosts for the host-network apps.
- **Storage paths.** Bulk storage is mounted at `/mnt/hdd/...` (Immich ML cache, Nextcloud data, music, SnapOtter models). Point these at your own disks.
- **Intel iGPU.** Immich, Nextcloud and SnapOtter mount `/dev/dri`. Remove those lines if your server has no iGPU.
- **AdGuard cert ID.** The AdGuard `cert-sync` reads NPM cert folder `npm-5`. On a new NPM install the number will differ (see the tutorial).
