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
- **A single wildcard certificate.** NPM requests `*.yourdomain.dedyn.io` from Let's Encrypt using a deSEC DNS challenge. AdGuard Home reuses that same certificate for DNS-over-TLS and its HTTPS UI. A small `cert-sync` sidecar copies it from NPM's `letsencrypt/` folder into `adguardhome/certs/` every hour.
- **Host-network apps.** AdGuard Home and GPTWOL use `network_mode: host` because they need raw DNS/ARP/WoL access. NPM reaches them by the server's LAN IP rather than a container name.
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
| [Navidrome](https://www.navidrome.org) | `navidrome` | Music streaming, plus [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) | `navidrome:4533`, `audiomuse-flask:8000` |
| [Sure](https://github.com/we-promise/sure) | `sure` | Personal finance, behind the [Pipelock](https://github.com/luckyPipewrench/pipelock) egress proxy | `sure-web:3000` |
| [SparkyFitness](https://github.com/CodeWithCJ/SparkyFitness) | `sparky-fitness` | Fitness and nutrition tracker | `sparkyfitness-frontend:80` |
| [Floppy](https://github.com/dannyvfilms/floppy) | `floppy` | Media tracker; custom AniList/SIMKL importers are mounted in | `floppy:8000` |
| [Dynacat](https://github.com/panonim/dynacat) | `dynacat` | Dashboard / start page | `dynacat:8080` |
| [Last Glance](https://github.com/krelltunez/lastglance) | `lastglance` | "When did I last…" tracker | `lastglance:80` |
| [BentoPDF](https://github.com/alam00000/bentopdf) | `bentopdf` | Client-side PDF toolkit | `bentopdf:8080` |
| [Omni-Tools](https://github.com/iib0011/omni-tools) | `omni-tools` | Collection of web utilities | `omni-tools:80` |
| [SnapOtter](https://hub.docker.com/r/snapotter/snapotter) | `snapotter` | AI image editing (iGPU) | `snapotter:1349` |
| [GPTWOL](https://github.com/Misterbabou/gptwol) | `gptwol` | Wake-on-LAN web UI | host `5000` |
| vdesktop | `vdesktop` | [Webtop](https://docs.linuxserver.io/images/docker-webtop/) XFCE desktop with Sober, built locally | `vdesktop:3000` |
| EVE.js | `evejs` | Private game server, deployed with `manage.sh` from a release zip | host `26000`–`26003`, `5222`, `40110`–`40111` |
| W101 server | `w101server` | Private game server ([Imlight](https://github.com/Revive101/Imlight)), built with `setup.sh` | host `12000`, `12333`, `12500` |

## Things you must adapt

- **Domain.** Use your own instead of `yourdomain.dedyn.io`.
- **Server IP.** Defaults to `192.168.0.214`. It appears in `/etc/docker/daemon.json`, in `w101server/setup.sh` and in the NPM hosts for the host-network apps.
- **Storage paths.** Bulk storage is mounted at `/mnt/hdd/...` (Immich ML cache, Nextcloud data, music, SnapOtter models). Point these at your own disks.
- **Intel iGPU.** Immich, Nextcloud, SnapOtter and vdesktop mount `/dev/dri`. Remove those lines if your server has no iGPU.
- **AdGuard cert ID.** The AdGuard `cert-sync` reads NPM cert folder `npm-5`. On a new NPM install the number will differ (see the tutorial).
