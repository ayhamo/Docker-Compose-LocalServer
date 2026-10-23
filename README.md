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
- **Hardened containers.** Where an image allows it, services run with `no-new-privileges` and `cap_drop: ALL`, adding back only the capabilities they need (each block is commented in its compose). Uptime Kuma has no Docker socket mount.
- **Scheduled jobs.** Two host cron entries run before the nightly Backrest backup (03:15): `adguardhome/cert-reload.sh` at 02:30 and `sure/backup-db.sh` (a `pg_dump` of Sure, 7 days kept) at 02:50. See the tutorial, step 6.
- **Global log rotation.** Logs are rotated for every container via `/etc/docker/daemon.json` (`local` driver, 2 × 2 MB).
- **No secrets in this repo.** Secrets have been replaced with `<REDACTED_…>` or `<CHANGE_ME…>` placeholders, and real `.env` files are git-ignored. Copy `.env.example` → `.env` and fill it in.

## Apps

| App | Folder | What it is | Reached via |
|---|---|---|---|
| [Nginx Proxy Manager](https://nginxproxymanager.com) | `nginx-proxy-manager` | Reverse proxy and SSL, plus [error-pages](https://github.com/tarampampam/error-pages) | host `80`, `443`, `81` |
| [AdGuard Home](https://adguard.com/adguard-home.html) | `adguardhome` | Network-wide DNS ad-blocking; DoT and HTTPS use NPM's cert | host network: DNS `53`, DoT `853`, UI `8880` / `8443` |
| [Dockhand](https://hub.docker.com/r/fnsys/dockhand) | `dockhand` | Web UI for managing the compose stacks | `dockhand:3000` |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | `uptime-kuma` | Uptime monitoring | `uptime-kuma:3001` |
| [Backrest](https://github.com/garethgeorge/backrest) | `backrest` | Restic backup UI, backs up `/opt/docker` read-only | `backrest:9898` |
| [Nextcloud AIO](https://github.com/nextcloud/all-in-one) | `nextcloud-aio` | Files, photos and office suite | AIO admin on host `8080`; Apache on host `11000` |
| [Immich](https://immich.app) | `immich` | Photo library with ML (OpenVINO), plus [immich-drop](https://github.com/nasogaa/immich-drop) for guest uploads | `immich_server:2283`, `immich_drop:8080` |
| [Navidrome](https://www.navidrome.org) | `navidrome` | Music streaming, plus [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) and `radio-relay` (re-streams HLS-only radio stations as MP3 for Navidrome's radio list, built locally) | `navidrome:4533`, `audiomuse-flask:8000`, `radio-relay:8080` |
| [Sure](https://github.com/we-promise/sure) | `sure` | Personal finance, behind the [Pipelock](https://github.com/luckyPipewrench/pipelock) egress proxy; `overrides/` holds a small Yahoo Finance fix mounted as an initializer | `sure-web:3000` |
| [SparkyFitness](https://github.com/CodeWithCJ/SparkyFitness) | `sparky-fitness` | Fitness and nutrition tracker | `sparkyfitness-frontend:80` |
| [Floppy](https://github.com/dannyvfilms/floppy) | `floppy` | Media tracker; custom AniList/SIMKL importers are mounted in | `floppy:8000` |
| [Dynacat](https://github.com/panonim/dynacat) | `dynacat` | Dashboard / start page | `dynacat:8080` |
| [Last Glance](https://github.com/krelltunez/lastglance) | `lastglance` | "When did I last…" tracker | `lastglance:80` |
| [BentoPDF](https://github.com/alam00000/bentopdf) | `bentopdf` | Client-side PDF toolkit | `bentopdf:8080` |
| [SnapOtter](https://hub.docker.com/r/snapotter/snapotter) | `snapotter` | AI image editing (iGPU) | `snapotter:1349` |
| [GPTWOL](https://github.com/Misterbabou/gptwol) | `gptwol` | Wake-on-LAN web UI | host `5000` |
| Smart Plug | `smart-plug` | Local web UI for a Tuya (LSC) smart plug: switching, schedules, energy stats; built locally | `smart-plug:8080` |
| EVE.js | `evejs` | Private game server, deployed with `manage.sh` from a release zip | host `26000`–`26003`, `5222`, `40110`–`40111` |
| W101 server | `w101server` | Private game server ([Imlight](https://github.com/Revive101/Imlight)), built with `setup.sh` | host `12000`, `12333`, `12500` |

## Oracle Cloud VM

A second, public-facing server runs on Oracle Cloud's Always Free tier (ARM, 2 OCPU, 12 GB RAM). It hosts what must stay reachable from the internet, starting with NPM as the public front door and AdGuard DNS-over-TLS for the phone, while the data stays at home. SSH and every admin UI are reachable over Tailscale only, and a Tailscale ACL stops the VM from reaching anything at home. Setup, security layers and host configuration are in **[oracle/README.md](oracle/README.md)**.

## Scheduled jobs (home server)

All times Europe/Berlin, checked against the jobs' real run times. Apps that schedule in UTC run one hour earlier on the clock in winter (shown in brackets). Every app dump finishes before Backrest at 03:15, so Backrest always picks up last night's copies. The Oracle VM has its own schedule in [oracle/README.md](oracle/README.md).

| When | Job | Runs from |
|---|---|---|
| Every 30 min (:13, :43) | deSEC DynDNS update of the home IP, with an Uptime Kuma heartbeat | root crontab |
| 00:00 Mon–Fri (23:00 Sun–Thu) | Sure: stock price import | Sure (UTC) |
| 00:00 on the 1st | Backrest prune and check | Backrest |
| 01:00 | Dockhand: check all containers for updates (no auto-install) and cleanups; Mondays also prune unused images | Dockhand |
| 01:10 (00:10) | SparkyFitness full backup | SparkyFitness (UTC) |
| 01:30 | Floppy: AniList and SIMKL import | Floppy |
| 02:30 | AdGuard cert reload: restarts AdGuard if NPM renewed the certificate (`adguardhome/cert-reload.sh`) | user crontab |
| 02:37 | Floppy database snapshot, 7 kept (`floppy/backups/database/`) | Floppy |
| 02:40 (01:40) | Immich database dump, 7 kept (`immich/library/backups/`) | Immich (UTC) |
| 02:50 | Sure database dump, 7 days kept (`sure/backup-db.sh`) | user crontab |
| 03:15 | Backrest backup of `/opt/docker` (7 daily, 4 weekly, 6 monthly) | Backrest |
| 04:00 | rclone: Backrest repository → Google Drive | root crontab |
| 04:20 (03:20) | Nextcloud AIO backup to `/mnt/hdd/nextcloud-backup` | Nextcloud AIO (UTC) |
| 05:00 | rclone: Nextcloud AIO backup → Google Drive | root crontab |
| 05:30 | Backup check: Immich, SparkyFitness and Floppy dumps and the Backrest snapshot must be under 26 h old; each reports to its own Uptime Kuma push monitor (`scripts/backup-check.sh`) | user crontab |
| Weekly | `apt upgrade` | by hand |

## Things you must adapt

- **Domain.** Use your own instead of `yourdomain.dedyn.io`.
- **Server IP.** Defaults to `192.168.0.214`. It appears in `/etc/docker/daemon.json`, in `w101server/setup.sh` and in the NPM hosts for the host-network apps.
- **Storage paths.** Bulk storage is mounted at `/mnt/hdd/...` (Immich ML cache, Nextcloud data, music, SnapOtter models). Point these at your own disks.
- **Intel iGPU.** Immich, Nextcloud and SnapOtter mount `/dev/dri`. Remove those lines if your server has no iGPU.
- **AdGuard cert ID.** The AdGuard `cert-sync` reads NPM cert folder `npm-5`. On a new NPM install the number will differ (see the tutorial).
