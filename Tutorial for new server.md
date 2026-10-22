# Setting up a new server

This guide takes a fresh Debian/Ubuntu machine (x86_64 or a Raspberry Pi) to the full stack in this repo. The steps are in dependency order, so follow them top to bottom:

1. Docker
2. `proxy-net`
3. Nginx Proxy Manager and the wildcard certificate
4. AdGuard Home, which reuses that certificate
5. Everything else

Placeholders used below:

| Placeholder | Meaning | Example |
|---|---|---|
| `<SERVER-IP>` | Server's static LAN IP | `192.168.0.214` |
| `yourdomain.dedyn.io` | Your deSEC domain | `homelab.dedyn.io` |
| `<CERT-ID>` | Folder NPM creates for the wildcard cert | `npm-1` |

---

## 0. Port map (why nothing conflicts)

Only these ports are published on the host. Everything else stays internal on `proxy-net` and is reached through NPM.

| Host port | Owner | Note |
|---|---|---|
| 80, 443 | Nginx Proxy Manager | Must stay free for NPM |
| 81 | NPM admin UI | |
| 53 tcp/udp, 853 tcp/udp | AdGuard Home (host network) | Port 53 must not be taken by `systemd-resolved` (step 1.3) |
| 3000 | AdGuard **setup wizard only** | Free again once setup is done |
| **8880** | AdGuard web UI (HTTP) | **Not** 80 (NPM), **not** 8080 (Nextcloud AIO) |
| **8443** | AdGuard web UI (HTTPS) | Required: the AdGuard healthcheck probes `https://127.0.0.1:8443` |
| 5000 | GPTWOL (host network) | |
| 8080 | Nextcloud AIO mastercontainer admin | Fixed by AIO |
| 11000 | Nextcloud AIO Apache | NPM forwards to this |
| 26000–26003, 5222, 40110–40111 | EVE.js | |
| 12000, 12333, 12500 | W101 server | |

---

## 1. Install Docker and prepare the host

### 1.1 Docker

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker          # or log out and back in
docker compose version
```

### 1.2 Global Docker settings: log rotation and DNS

Every container gets small rotated logs. Containers also resolve DNS through AdGuard, with `1.1.1.1` as fallback while AdGuard is down.

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "2m", "max-file": "2" },
  "dns": ["<SERVER-IP>", "1.1.1.1"]
}
EOF
sudo systemctl restart docker
```

### 1.3 Free port 53 (Ubuntu only)

Debian doesn't run `systemd-resolved`, so skip this there. On Ubuntu, AdGuard can't bind port 53 until the stub listener is turned off:

```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNSStubListener=no\n' | sudo tee /etc/systemd/resolved.conf.d/adguard.conf
sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
sudo systemctl restart systemd-resolved
```

### 1.4 Folder layout and shared network

```bash
sudo mkdir -p /opt/docker
sudo chown -R $USER:$USER /opt/docker

# Copy this repo's docker/ folder to /opt/docker on the server
git clone https://github.com/ayhamo/Docker-Compose-LocalServer.git ~/Docker-Compose-LocalServer
cp -a ~/Docker-Compose-LocalServer/docker/. /opt/docker/

# The one external network every web app joins
docker network create proxy-net
```

> Before starting any app, search its compose for `/mnt/hdd`, `/dev/dri` and `<REDACTED_…>` and adapt them to your machine (see the README).

---

## 2. Nginx Proxy Manager (first, because it owns 80/443 and the certificate)

```bash
cd /opt/docker/nginx-proxy-manager
docker compose up -d
```

Open `http://<SERVER-IP>:81` and complete the first-run screen to create your admin account.

### 2.1 Issue the wildcard certificate with deSEC

1. In deSEC, create an API token for your domain.
2. In NPM, go to **Certificates** → **Add Certificate** → **Let's Encrypt**.
3. **Domain names:** `yourdomain.dedyn.io` and `*.yourdomain.dedyn.io`.
4. Enable **Use a DNS Challenge**, pick the provider **deSEC**, and replace the credentials box with:
   ```ini
   dns_desec_token = YOUR_DESEC_TOKEN
   ```
5. Set **Propagation seconds** to `120` (deSEC can be slow), agree to the terms and save.

NPM now owns and auto-renews the certificate. Note the folder it was saved in, because AdGuard needs it in the next step:

```bash
sudo ls /opt/docker/nginx-proxy-manager/letsencrypt/live/
# e.g.  npm-1   ← this is <CERT-ID>
```

> **Fix from the old guide:** there is **no certbot container** and nothing is uploaded into NPM as a custom certificate. NPM creates the cert itself, and AdGuard copies it *from* NPM.

---

## 3. AdGuard Home and cert-sync

### 3.1 Point cert-sync at your certificate

The compose file has the cert ID of the original server (`npm-5`) hard-coded. Replace it with yours:

```bash
cd /opt/docker/adguardhome
sed -i 's#/live/npm-5/#/live/<CERT-ID>/#g' docker-compose.yml
grep -n "/live/" docker-compose.yml     # check the result
mkdir -p conf work certs
```

Also change `hostname: ayham` to your own hostname.

### 3.2 Start it and run the wizard

```bash
docker compose up -d
docker logs adguard-cert-sync      # expect: "SUCCESS: SSL certificates synchronized from NPM."
ls certs/                          # fullchain.pem  privkey.pem  .last_sync
```

Open `http://<SERVER-IP>:3000` for the setup wizard:

- **Admin web interface:** all interfaces, port **`8880`**.
- **DNS server:** all interfaces, port `53`.

### 3.3 Enable encryption (DoT and HTTPS UI)

Go to **Settings** → **Encryption settings**:

| Field | Value |
|---|---|
| Enable encryption | ✔ |
| Server name | `adguard.yourdomain.dedyn.io` |
| Redirect to HTTPS automatically | ✘ (NPM handles HTTPS) |
| HTTPS port | `8443` |
| DNS-over-TLS port | `853` |
| DNS-over-QUIC port | `784` (optional) |
| Certificate | *Set a certificates file path* → `/opt/adguardhome/certs/fullchain.pem` |
| Private key | *Set a private key file* → `/opt/adguardhome/certs/privkey.pem` |

The container healthcheck turns **healthy** only after this is saved, because it probes `https://127.0.0.1:8443`. cert-sync copies the renewed cert every hour, but AdGuard only loads it on restart. `cert-reload.sh` handles that: it compares the synced cert with the one AdGuard serves on 853 and restarts AdGuard when they differ. Schedule it in step 6. If your domain isn't `yourdomain.dedyn.io`, fix the `-servername` in the script.

### 3.4 Local DNS for your domain

1. Go to **Filters** → **DNS rewrites** → **Add**.
2. Set `*.yourdomain.dedyn.io` → `<SERVER-IP>` and `yourdomain.dedyn.io` → `<SERVER-IP>`.
3. Then point your router's DHCP DNS (or each device) at `<SERVER-IP>`. Android "Private DNS" can use `adguard.yourdomain.dedyn.io` over DoT on port 853.

---

## 4. Proxy hosts in NPM

For each service, create a proxy host in NPM: go to **Hosts** → **Proxy Hosts** → **Add**.

- **Scheme:** `http`.
- Enable **Block Common Exploits** and **Websockets Support**.
- **SSL tab:** select the wildcard cert and enable **Force SSL** and **HTTP/2**.

Containers on `proxy-net` are reached by **container name**. Host-network apps are reached by **`<SERVER-IP>`**, never `127.0.0.1`, which would point at NPM's own container.

| Subdomain | Forward host | Port |
|---|---|---|
| npm | `nginx-proxy-manager` | 81 |
| adguard | `<SERVER-IP>` | 8880 |
| gptwol | `<SERVER-IP>` | 5000 |
| nextcloud | `<SERVER-IP>` | 11000 (+ custom config below) |
| dockhand | `dockhand` | 3000 |
| uptime | `uptime-kuma` | 3001 |
| backrest | `backrest` | 9898 |
| immich | `immich_server` | 2283 |
| drop | `immich_drop` | 8080 |
| music | `navidrome` | 4533 |
| audiomuse | `audiomuse-flask` | 8000 |
| sure | `sure-web` | 3000 |
| fitness | `sparkyfitness-frontend` | 80 |
| floppy | `floppy` | 8000 |
| dash | `dynacat` | 8080 |
| lastglance | `lastglance` | 80 |
| pdf | `bentopdf` | 8080 |
| snapotter | `snapotter` | 1349 |
| smart-plug | `smart-plug` | 8080 |
| radio | `radio-relay` | 8080 |

Radio streams are then `https://radio.yourdomain.dedyn.io/<station>.mp3` (station names are in `navidrome/radio-relay/relay.py`); add them in Navidrome under **Radios**.

For **Nextcloud**, paste the contents of [`docker/nextcloud-aio/nextcloud custom nginx config.txt`](docker/nextcloud-aio/nextcloud%20custom%20nginx%20config.txt) into the proxy host's **Advanced** → *Custom Nginx Configuration*.

The `error-pages` container is used for NPM's 404/5xx pages. It starts together with NPM.

---

## 5. Start the rest

Every app is started with `cd /opt/docker/<app> && docker compose up -d`. Order doesn't matter from here on, but these apps need preparation first:

| App | Before `up -d` |
|---|---|
| **dockhand, uptime-kuma, backrest, bentopdf, lastglance, dynacat** | Nothing; just start them. Dynacat mounts `/mnt/hdd` read-only, so adjust that path if needed. |
| **floppy** | Set `SECRET=` to a long random string (`openssl rand -hex 32`). In `anilist.py`, fill in `MANGAUPDATES_USERNAME`/`MANGAUPDATES_PASSWORD`, or leave them empty if you don't import from MangaUpdates. |
| **immich** | `cp .env.example .env` and set `DB_PASSWORD` (letters and digits only) and `UPLOAD_LOCATION`. Fix the Nextcloud photos mount path and `/mnt/hdd/docker/immich-ai-models`. Start it, create the admin user, then create an **API key** in Immich and put it into `IMMICH_API_KEY` (immich-drop). Set `ADMIN_PASSWORD`, then `docker compose up -d immich-drop`. |
| **navidrome** | Replace `<REDACTED_AUDIOMUSE_DB_PASSWORD>` in all **three** places with the same value. Run `mkdir -p navidrome-data && sudo chown 1000:1000 navidrome-data` (the container runs as UID 1000). Point `/mnt/hdd/music` at your library. `cp .env.example .env` and fill in a [Last.fm API account](https://www.last.fm/api/account/create) (`LASTFM_APIKEY`, `LASTFM_SECRET`). `radio-relay` is built locally on the first `up -d`. |
| **sparky-fitness** | `cp .env.example .env` and fill every `<CHANGE_ME…>` (`openssl rand -hex 32` for the two keys). Set `SPARKY_FITNESS_FRONTEND_URL` to your real `https://fitness.yourdomain.dedyn.io`. |
| **sure** | Create a `.env` with `SECRET_KEY_BASE=$(openssl rand -hex 64)` and `POSTGRES_PASSWORD=…`, or replace the `<REDACTED_…>` defaults in the compose. `pipelock.example.yaml` must sit next to the compose (it does), and so must `overrides/`. For the nightly dump, schedule `backup-db.sh` (step 6); to report it to Uptime Kuma, put a push monitor URL in `backups/.kuma-push-url`. |
| **snapotter** | Create `/mnt/hdd/docker/snapotter/{models,ai,workspace}`, or change the paths. Needs `/dev/dri`. |
| **nextcloud-aio** | Fix `NEXTCLOUD_DATADIR`. Don't rename the container or volume, because AIO depends on those names. Start it, open `https://<SERVER-IP>:8080`, save the passphrase **somewhere safe outside the repo**, and enter `nextcloud.yourdomain.dedyn.io` as the domain. Remove `NEXTCLOUD_ADDITIONAL_APKS`/`devices` if there is no Intel iGPU. |
| **gptwol** | Nothing (host network, port 5000). Enable `ENABLE_LOGIN` if it's reachable from outside your LAN. |
| **smart-plug** | Built locally (`docker compose up -d --build`). Before the first start, get the plug's Tuya local key and create `data/config.json`, as described in `smart-plug/README.md`. `data/` holds that key, so never commit it. |
| **evejs** | Not a plain `up -d`. Put the EVE.js release `.zip` in the folder and run `./manage.sh`, choosing **f** for a fresh install. The script patches `compose.yaml` and builds the image. In its final message, replace `192.168.0.214` with `<SERVER-IP>`. |
| **w101server** | Not a plain `up -d`. Edit `SERVER_IP=` in `setup.sh`, place your wiztype `.json` type dump next to it, then run `./setup.sh`. It clones and builds Imlight and writes the compose. |

---

## 6. Scheduled jobs

Both scripts run as your user (who is in the `docker` group) and finish before Backrest's nightly backup at 03:15, so the backup always holds a fresh Sure dump. Add them with `crontab -e`:

```cron
# AdGuard: load the renewed cert (if NPM renewed it)
30 2 * * * /opt/docker/adguardhome/cert-reload.sh >> /opt/docker/adguardhome/cert-reload.log 2>&1
# Sure database dump before Backrest (03:15)
50 2 * * * /opt/docker/sure/backup-db.sh >> /opt/docker/sure/backups/backup-db.log 2>&1
```

```bash
chmod +x /opt/docker/adguardhome/cert-reload.sh /opt/docker/sure/backup-db.sh
mkdir -p /opt/docker/sure/backups
```

---

## 7. Check everything

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}' | sort
```

All containers should show `Up … (healthy)` or `Up`. If something isn't right:

- **AdGuard `unhealthy`:** encryption isn't enabled on port 8443 yet (step 3.3).
- **`adguard-cert-sync` logs "Files missing":** the `<CERT-ID>` in the compose is wrong (step 3.1).
- **AdGuard fails with `bind: address already in use` on :53:** `systemd-resolved` is still running (step 1.3).
- **NPM shows 502:** the target container isn't on `proxy-net`, or the port in the proxy host is wrong.

---

## What was wrong in the previous version of this guide

- It described a **certbot container inside the AdGuard stack** writing to `adguardhome/certs`, with the cert then uploaded into NPM as a custom certificate. The real flow is the opposite: **NPM issues the wildcard cert via deSEC**, and AdGuard's `cert-sync` copies it out of `nginx-proxy-manager/letsencrypt/live/<CERT-ID>/`.
- It suggested AdGuard's web UI on **8080**, which collides with **Nextcloud AIO** (host 8080). It also left it on 3000. The UI now uses **8880** (HTTP) and **8443** (HTTPS), and 8443 is required by the healthcheck.
- It never mentioned that the `npm-5` cert ID is specific to the original server.
- It used the old `nginx/` folder name. The folder is now `nginx-proxy-manager/`.
- It covered only NPM, AdGuard and Dockhand. It skipped `daemon.json`, freeing port 53, `.env` files and the per-app prep above.
