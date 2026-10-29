# Oracle Cloud VM (Always Free)

A second, public-facing server next to the home server: an Oracle Cloud **Always Free** ARM VM (2 OCPU, 12 GB RAM, 100 GB disk, Ubuntu 24.04 aarch64). It hosts what has to stay up or face the internet (the public Pangolin gateway with Pocket ID login, which also publishes chosen home apps; Uptime Kuma watching home) while the home server keeps the data. Compose files live under [`docker/`](docker/), which mirrors `/opt/docker/` on the VM.

## Security layout

```
 Internet ─► ① Oracle security list: TCP 80, 443 + UDP 41641 (Tailscale) only. No SSH.
          ─► ② VM firewall (iptables): SSH only on tailscale0; CrowdSec bans first,
               then 80/443 only from DE and KW (GeoIP)
          ─► ③ Docker: Traefik (Pangolin) 80/443 public; admin UIs on the Tailscale IP,
               their links Tailscale-only + Pocket ID login
 Tailscale ─► ④ ACL: the VM is tag:oracle and may reach only the home server's NPM
                 (TCP 443); member devices (PC, phone, home server) may reach it
 Home NPM  ─► ⑤ requests from the VM get 403 unless the app is on the published list
```

Port 853 (DoT) was removed from the security list on 2026-10-29 while the VM's AdGuard is off; add it back when AdGuard returns.

- **Cost guards (Oracle account):** quota policy `free-tier-only` caps everything at Always Free amounts (A1 2 OCPU / 12 GB, block storage 200 GB in the VM's AD and 0 in the others, no reserved public IPs, no paid services) and a budget `zero-cost-guard` (EUR 1, alerts at 1 % actual and forecast). Oracle halved the free A1 allowance on 2026-06-15 to 2 OCPU / 12 GB; the console's banner still shows the old 4 / 24.
- **Least privilege:** the CLI runs as an API-key-only user `cli-bot` (group `homelab-ops`) that may manage only the `homelab` compartment and read costs. The admin account uses MFA and is kept out of day-to-day work.
- **SSH:** key only, user `ubuntu`, no root, no X11, reachable over Tailscale only. One key per device.

## AdGuard Home (stopped)

**Switched off on 2026-10-27; the files stay for later.** The home AdGuard is the only DNS: the phone's DoT name is a CNAME to the home apex, the VM's container is down (`docker compose down`), the home `adguardhome-sync` sits behind the compose profile `oracle-replica`, and the 02:30 cert reload below is commented out. The home server's old failover script (`scripts/dns-failover.py`) is replaced by a gateway heartbeat. To bring it back: `docker compose up -d` here, `docker compose --profile oracle-replica up -d` in the home AdGuard stack, re-enable the cron line, add TCP 853 back to the Oracle security list, and point the phone's name at the VM again. The notes below describe it as it ran.

[`docker/adguardhome`](docker/adguardhome/) is a copy of the home server's AdGuard, so the phone can use either one with the same Private DNS name. The config was copied from home with three changes: web UI on port 80 inside the container, HTTPS UI off and DNS-over-QUIC off (Oracle only lets TCP 853 in).

- **Bridge network, not host.** Only the published ports exist: DoT `853` public; plain DNS `53` and the web UI `8880` on the Tailscale IP only.
- **Not an open resolver.** `allowed_clients` accepts only the phone's ClientID (the first label of the DoT hostname) plus private and Tailscale ranges, which cannot arrive from the internet. Tested from outside: the phone's ClientID resolves and blocks ads, while no ClientID or a wrong one gets `REFUSED`.
- **Certificate.** The `cert-sync` sidecar ([traefik-certs-dumper](https://github.com/ldez/traefik-certs-dumper)) exports the wildcard cert from Traefik's `acme.json` as PEM every hour, read-only and through memory only. AdGuard picks up new files by itself; `cert-reload.sh` at 02:30 restarts it if the served cert still differs.
- **Real client IPs.** `trusted_proxies` includes `proxy-net` (`172.18.0.0/16`), so logins through the link are counted per device, not all as Traefik's IP. A lockout then only hits the device that failed. The sync can't overwrite this setting: it is not part of AdGuard's API.
- **Settings follow home.** The `adguardhome-sync` service in the home [AdGuard stack](../docker/adguardhome/) copies home's settings to this AdGuard every hour, except TLS and DHCP. Change settings at home only.
- **Phone failover.** The phone's DoT name (`<ClientID>.yourdomain.dedyn.io`) has its own A record pointing to this VM, with a 60 s TTL. It is set through deSEC's DynDNS interface, because normal records need at least 900 s. Every minute [`scripts/dns-failover.py`](../docker/scripts/dns-failover.py) on the home server checks this AdGuard over DoT with the phone's ClientID and reports to Uptime Kuma. After 3 failures it deletes that record, so the wildcard CNAME sends the phone home. After 10 good checks in a row it points the record back to the VM (tested both ways; by hand: `dns-failover.py --to-vm`). It never switches while home has no internet or the home AdGuard is down. The check verifies the certificate, so an expired or wrong cert also alerts.

## Public web protection (CrowdSec + GeoIP)

Both block at the firewall, before a request reaches Traefik. Neither touches SSH (Tailscale only), and GeoIP leaves Tailscale open worldwide.

- **CrowdSec** ([`docker/crowdsec`](docker/crowdsec/)) reads Traefik's access log, which has every public request with the real client IP ([`acquis.yaml`](docker/crowdsec/acquis.yaml), collection `crowdsecurity/traefik`; [`host/logrotate-pangolin-traefik`](host/logrotate-pangolin-traefik) keeps 7 days). Pangolin has no CrowdSec of its own, so this stays. Its dashboard is [CrowdSec Web UI](https://github.com/TheDuffman85/crowdsec-web-ui) in the same compose: alerts, bans and metrics, its own login, no Docker socket. It logs in to CrowdSec as the machine `crowdsec-web-ui` and is Tailscale-only: `oracle-crowdsec` (Pangolin rules: Tailscale → bypass, everything else blocked) or `<TAILSCALE-IP>:3002`. It detects scans, brute force and known CVE probes, and adds the community blocklist. The host package `crowdsec-firewall-bouncer-iptables` (CrowdSec's apt repository) enforces bans as the first rule in `INPUT` and `DOCKER-USER`. An allowlist `homelab` holds the home public IP and `100.64.0.0/10`, so we can't ban ourselves.
- **GeoIP** ([`host/geoip-allow.sh`](host/geoip-allow.sh) with its systemd units) is an `ipset` of German and Kuwaiti networks from ipdeny.com. In `DOCKER-USER`, ports 80/443 on the public interface accept only those. Certificates use DNS-01, so Let's Encrypt never needs port 80. The script refuses to apply a short or empty list, so a failed download can't block everyone. `geoip-allow.sh off` removes the rule.

## Gateway: Pangolin + Pocket ID

[Pangolin](https://github.com/fosrl/pangolin) (Community Edition) replaced Nginx Proxy Manager on 2026-10-27. It is Traefik plus a dashboard that decides who may open which app. [Pocket ID](https://pocket-id.org) is the login: one account per person, with a passkey.

- **[`docker/pangolin`](docker/pangolin/)**: `pangolin` (dashboard, API, rules) and `traefik` (ports 80/443; 80 only redirects to HTTPS). Traefik joins `proxy-net` and reaches apps by container name. `config/config.yml` holds Pangolin's `server.secret` (it can't come from `.env`), so it is mode 600, like its database `config/db/db.sqlite` (users, sessions, the encrypted OIDC secret); Pangolin runs as root inside its container and still reads both.
- **Secrets:** every stack here keeps passwords and keys in its own `.env` (600, git-ignored; `.env.example` shows the names) and the compose files only reference `${VAR}`. Moved on 2026-10-29 with the resolved `docker compose config` checked identical before and after, so no container changed.
- **No tunnels.** Pangolin's own tunnel parts (Gerbil, Newt) are not installed. Home is reached over Tailscale, and Pangolin's VPN uses the same `100.64.0.0/10` range as Tailscale, which clashes on Linux. The `gerbil:` block in `config.yml` only satisfies Pangolin's config check.
- **Certificate.** Traefik gets `*.yourdomain.dedyn.io` from Let's Encrypt by DNS-01 through deSEC (token in `.env`) and renews it about 30 days before expiry. The deSEC token `oracle-traefik-acme` may only write the TXT records `_acme-challenge` and `_acme-challenge.oracle-pangolin` (the dashboard has its own cert), may not create or delete domains, and only works from the VM's public IP; everything else is read-only (tested: other records `403`, other IPs `401`). `LEGO_DISABLE_CNAME_SUPPORT=true` stays set: it was needed while the wildcard was a CNAME, which would have sent the `_acme-challenge` record to the apex. The Badger plugin is pinned to `v1.7.0`, because `v1.8.0` failed Traefik's plugin integrity check.
- **[`docker/pocket-id`](docker/pocket-id/)**: no published port. Traefik routes it from [`dynamic_config.yml`](docker/pangolin/config/traefik/dynamic_config.yml), not as a Pangolin resource, so the login never depends on the gate it feeds. Back up its `ENCRYPTION_KEY`: without it Pocket ID can't read its data. Traefik carries a Docker network alias with Pocket ID's name, so Pangolin reaches Pocket ID directly inside Docker when it checks a login, whatever public DNS says.
- **Pocket ID → Pangolin.** OIDC client "Pangolin" (callback `https://oracle-pangolin.yourdomain.dedyn.io/auth/idp/*/oidc/callback`, access *All Users*; the default "selected groups" with no group denies everyone). In Pangolin, identity provider "Pocket ID" with identifier `preferred_username` and auto-provisioning: your own username becomes Admin, everyone else Member (`preferred_username == '<you>' && 'Admin' || 'Member'`). A Member sees only the apps given to the Member role.

### Admin links: Tailscale = app login, internet = Pocket ID first

`oracle-pangolin`, `oracle-pocketid`, `oracle-navidrome`, `oracle-uptimekuma`, `oracle-crowdsec` and `oracle-backrest` under `yourdomain.dedyn.io` all use the wildcard certificate. Public DNS sends them to the VM through the wildcard record, at home too (home AdGuard only exempts them from its `*.yourdomain` rewrite).

- **Trusted = Tailscale or home:** Tailscale proves who you are, and so does home's internet IP (`<HOME-IP>/32`, kept current by [`host/home-ip-trust.py`](host/home-ip-trust.py)). From either, the apps open straight to their own login. Each is a Pangolin resource (site type *Local*, targets by container name) with the rules: from `100.64.0.0/10`, then from `<HOME-IP>/32` → bypass the gateway login.
- **Admin-only (trusted only):** Kuma, CrowdSec Web UI and Backrest add a last rule, `DROP /*`, so from anywhere else they get no login page at all; a Traefik router in front (`!ClientIP(100.64.0.0/10) && !ClientIP(<HOME-IP>/32)`) shows the styled `403` page instead of Pangolin's plain `401`. Pocket ID's admin pages and admin API are locked the same way (router `pocket-id-admin-public`).
- **From the internet:** first the host firewall (GeoIP DE/KW, CrowdSec), then the Pocket ID passkey (*skip to identity provider*: no Pangolin login page in between), then the app's own login. Pocket ID is public so this login can happen. Of Pangolin's dashboard, the internet reaches only the resource login (`/auth/resource/`, the Pocket ID callback `/auth/idp/`, page files `/_next/`, `favicon.ico`). Everything else, including the dashboard's own login page (where cancelling Pocket ID would otherwise land) and its email + password login (the server admin account, kept as a break-glass login), answers only over Tailscale (`ipAllowList`, internet: `403`). Sign-ups are closed in both apps. The home gateway check fetches `favicon.ico` for that reason. Blocked dashboard pages and names without a resource get styled pages (`error-pages` in the Pangolin compose, the same image as home NPM's) instead of Traefik's plain `403` / `404` text.
- **Navidrome apps:** `/rest/*` (the Subsonic API, with Navidrome's own login) passes without Pocket ID, so music apps work; Traefik limits it to 10 requests per second per IP (burst 50, then `429`). A failed app login still answers HTTP 200, so CrowdSec reads Navidrome's own log instead: Navidrome writes it to `navidrome-data/logs/navidrome.log` (`ND_LOGFILE`, rotated by [`host/logrotate-navidrome`](host/logrotate-navidrome)) with the real client IP (it trusts `X-Forwarded-For` from `proxy-net`). The local parser and scenario in [`docker/crowdsec/config`](docker/crowdsec/config/) ban an IP after 5 failed logins (web or app) within about 100 s. Trusting `proxy-net` also enables Navidrome's header login, so its header name is random and Traefik strips it (and `Remote-User`) from every incoming request (`strip-proxy-auth` on the `websecure` entrypoint; tested: the header works from inside Docker, fails from the internet).
- **Background and cover clips (Psysonic / BitChord forks):** `GET /rest/x-clip/<folder>/<song name>.background.mp4` (or `.cover.mp4`, the square cover clip) serves the clip that sits next to the song. Traefik first asks Navidrome whether the app's `X-ND-Authorization` token is valid (`forwardAuth` to `/api/keepalive/keepalive`, `401` without), then `navidrome-clips` (nginx, read-only music mount, no capabilities) serves only `*.background.mp4` and `*.cover.mp4` one folder deep; anything else is `404`. Every answer, a `404` too, carries `Cache-Control`, which the apps need to remember "no clip". Same rate limit as `/rest`. Failed tokens log as a plain `HTTP 401` in Navidrome, which the brute-force scenario does not count.
- **Radio relay:** Navidrome's HLS-only radios (Kuwait FM) stream from `radio.yourdomain.dedyn.io`. On the VM that name goes to the VM's own `radio-relay` (same code as home, [`../docker/navidrome/radio-relay`](../docker/navidrome/radio-relay/)); home devices reach the home relay through home NPM. Public (GeoIP + CrowdSec apply), at most 3 streams per IP at once (`inFlightReq`), because each listener starts an ffmpeg. A listener that disappears without closing is dropped after 30 s, so no ffmpeg is left running.
- **Ban length:** 4 h for a first ban, then 4 h more for each ban the IP already had (8 h, 12 h, …), set by `duration_expr` in [`profiles.yaml`](docker/crowdsec/config/profiles.yaml). Exception, a **permanent ban (10 years)**, for failed logins at the home apps behind the gateway, where only one person logs in (profile `homelab_login_permanent`, first in the file):
  - [`homelab/gateway-login-fail`](docker/crowdsec/config/scenarios/homelab-gateway-login-fail.yaml): 5 wrong passwords at any pace (one leaks away every 30 days), counted from Traefik's log when the **app itself** answered `401` to a login: Immich `POST /api/auth/login`, SparkyFitness `POST /api/auth/sign-in/email`, Navidrome web `POST /auth/login`. Other `401`s (expired tokens, WebDAV clients connecting before they send the password) and the gateway's own `401` ("log in with Pocket ID first", server URL `-`) never count. Nextcloud is left to its own brute-force protection (real client IPs via `trusted_proxies`). Oracle's Navidrome (friends) keeps the 4 h rule.
  - [`homelab/home-navidrome-bf`](docker/crowdsec/config/scenarios/homelab-home-navidrome-bf.yaml): home Navidrome answers `200` to a wrong app password, so its own log is the only signal. It arrives by syslog over Tailscale ([`acquis.d/home-navidrome.yaml`](docker/crowdsec/config/acquis.d/home-navidrome.yaml), UDP 5514 on the VM's Tailscale IP only) with the visitor's real IP (home NPM trusts `X-Forwarded-For` from the VM only). Same parser as the VM's Navidrome.
  - Tested 2026-10-29: 5 slow wrong Immich logins → ban `87599h…`; 5 Pocket-ID-gate `401`s → nothing; wrong home Navidrome app logins → ban. Home's IP and Tailscale stay allowlisted. CrowdSec keeps counts in memory, so a restart starts them at 0. Unban: `docker exec crowdsec cscli decisions delete --ip <ip>`.
- **Login lifetime:** a Pangolin session lasts 30 days and extends while used; Pocket ID's own sign-in lasts about an hour (default).
- **Trusted from anywhere on Tailscale:** the VM advertises its own public IP as a Tailscale route (`<PUBLIC-IP>/32`, approved in the admin console, allowed in the ACL) and holds that IP on `lo` ([`host/public-ip-loopback.service`](host/public-ip-loopback.service)). A device with Tailscale on then reaches the public address through Tailscale, arrives with its `100.x` address and counts as trusted, at home or away, with normal public DNS.
- **Direct Tailscale ports:** only for machines: `<TAILSCALE-IP>:3001` (Kuma: the home heartbeat and the backup push), `:2376` (Hawser, for home Dockhand), `:27973` (glance-agent). CrowdSec Web UI and Backrest have no published port; they open only through Pangolin.

### Home apps through the gateway

Home apps are published here one by one; everything not published stays reachable only at home or over Tailscale. Started 2026-10-28 with BentoPDF as the test app.

Published (2026-10-29), all **Admin role only** except BentoPDF (also Member). The listed paths skip Pocket ID so the mobile and sync apps work; the app's own login protects them:

| App | Paths without Pocket ID |
|---|---|
| BentoPDF, Floppy, Sure, SnapOtter | none (SnapOtter has no login of its own, so Pocket ID is its only lock) |
| SparkyFitness | `/api/*` (its installed web app on the phone serves its pages from cache, so only its API calls reach the gateway; also its mobile app) |
| Navidrome (home) | `/rest/*`, rate-limited like the VM's (router `home-navidrome-rest` in `dynamic_config.yml`, straight to home NPM with NPM's certificate checked); failed logins ban permanently (see *Ban length*) |
| Immich | `/api/*`, `/.well-known/immich` |
| Nextcloud | `/remote.php/*`, `/ocs/*`, `/status.php`, `/index.php/login/v2/poll`, `/login/v2/poll`, `/.well-known/*`. Known limit through the gateway (not at home or on Tailscale): the Nextcloud app's thumbnails, avatar and office editing (`/index.php/core/preview`, `/index.php/avatar`, `/apps/files/directEditing/…` in its built-in browser) stop at Pocket ID; files, sync and the web UI work. Kept this way on purpose |
| Immich Drop | resource exists but is **disabled** (its NPM line is commented out) |

- **Never published:** Dockhand (Docker socket), NPM admin, Backrest, Kuma, AdGuard, GPTWOL, Dynacat, lastGLANCE, Pinchflat, the Nextcloud AIO admin. The Oracle Kuma monitor "Home NPM blocks the VM" checks Dockhand for that reason.
- **Real client IPs in Nextcloud:** `trusted_proxies` also lists the VM's Tailscale IP, so its brute-force protection counts each visitor, not the VM (Traefik overwrites any `X-Forwarded-For` a client sends). AIO may reset `config.php` on updates: check `occ config:system:get trusted_proxies` after one.
- **Home AdGuard:** each has the self-rewrite and the home-only rule below, so at home and on Tailscale these names go straight to home NPM, and the phone on mobile data (which still asks home AdGuard over DoT) gets the gateway.

```
 phone / friend (no Tailscale) ─► public DNS: *.yourdomain.dedyn.io → VM
   ─► GeoIP + CrowdSec ─► Traefik ─► Pocket ID ─► Pangolin resource
   ─► Tailscale (ACL: TCP 443 to the home server only) ─► home NPM ─► app
```

- **DNS (deSEC).** `*` is an A record to the VM's public IP. The apex keeps the home IP (DDNS), and the phone's DoT name is a CNAME to the apex, so DNS-over-TLS still goes home. No per-app records.
- **Pangolin resource.** Same name as at home (e.g. `bentopdf`), target `https://<HOME-TAILSCALE-IP>:443`, *TLS server name* and *Host header* set to the app's name so home NPM picks the right host (Pangolin's Traefik does not verify backend certificates, `insecureSkipVerify`; the hop runs inside Tailscale's encryption). Rule: from `100.64.0.0/10` → bypass; skip to identity provider Pocket ID. Who may open it is set per resource (roles or users).
- **Home NPM.** [`http_top.conf`](../docker/nginx-proxy-manager/data/nginx/custom/http_top.conf) maps requests from the VM's Tailscale IP to `403` unless the host is listed, and [`server_proxy.conf`](../docker/nginx-proxy-manager/data/nginx/custom/server_proxy.conf) (included by every proxy host) enforces it. A broken-into VM can reach port 443 but none of the unpublished apps. This needs NPM to see the VM's real IP: [`tailscale-docker-realip.service`](../docker/scripts/tailscale-docker-realip.service) at home keeps Tailscale from rewriting it, and the Oracle Kuma monitor "Home NPM blocks the VM" checks it.
- **Home AdGuard.** The phone asks home AdGuard over DoT everywhere, so without this it would get the home LAN address on mobile data too. A published app gets a self-rewrite (it leaves the `*.yourdomain` wildcard rewrite), then one rule that gives the home server only to questions from home, LAN or Tailscale: `||app^$dnsrewrite=<HOME-LAN-IP>,client=192.168.0.0/16|172.16.0.0/12|10.0.0.0/8|100.64.0.0/10|fd7a:115c:a1e0::/48|fe80::/10|fc00::/7|<HOME-IPV6-/64>|127.0.0.1|::1`. The Docker ranges are there because home containers also ask AdGuard (`daemon.json`): without them Collabora, Nextcloud's ExApps and Kuma reached Nextcloud through the VM. Everyone else (the phone on mobile data, a public address) gets public DNS and goes through the gateway; at home the phone still goes straight to home NPM. If home's IPv6 prefix changes, the phone at home just takes the gateway for these names until the rule is updated. Tested 2026-10-29: LAN → home, a DoT query with the phone's ClientID from outside → the VM, unpublished names → home only.
- **The home server itself** resolves through the router, which now answers with the VM. `/etc/hosts` pins `uptimekuma.yourdomain.dedyn.io` to `127.0.0.1` / `::1`, so its cron pushes still reach the home Kuma.

To publish another app: Pangolin resource (as above), a line in `http_top.conf` plus `nginx -s reload` in the NPM container, the AdGuard self-rewrite and the home-only rule. To unpublish, undo the three.

## Navidrome (music, moving here from home)

[`docker/navidrome`](docker/navidrome/) is the home stack with two changes: `user: "1001:1001"` (the VM's `ubuntu`) and the library in `./music` on the VM. It runs everything home runs: Navidrome with its plugins, AudioMuse-AI (Flask, worker, Postgres; the image has ARM builds) and `radio-relay` (built here from [`../docker/navidrome/radio-relay`](../docker/navidrome/radio-relay/)).

- **Copied from home on 2026-10-28:** Navidrome's database (users, playlists, play counts, radios, plugin settings) through SQLite's online backup, the plugins, the radio images, `.env` (Last.fm keys), and AudioMuse's database through `pg_dump` / `pg_restore` (Postgres data files are not portable between x86 and ARM). Not copied: music, `artwork/hashed` and `cache` (rebuilt on demand).
- **Music not there yet.** A full scan marks every song *missing* instead of deleting it, so playlists, play counts and AudioMuse's matches come back when the files arrive at the same paths.
- **Public access:** see *Admin links* above (`/rest` past Pocket ID with a rate limit).
- **AudioMuse UI:** `oracle-audiomuse.yourdomain.dedyn.io`, a plain Traefik router (not a Pangolin resource) with `tailscale-only`: Tailscale or home only, everyone else gets the styled 403. AudioMuse has no login of its own. Home AdGuard has a self-rewrite for the name, like the other `oracle-*` links.

## Uptime Kuma (watching home)

[`docker/uptime-kuma`](docker/uptime-kuma/) watches the home server from outside, so an alert still goes out when the home internet is down. It started as a copy of the home Kuma database, giving the same login and Discord alert, with the home monitors and history removed. It has four monitors:

- **Home internet (DoT 853)**: TCP check of the home public DoT port every 60 s (home internet, router forward, home AdGuard).
- **Home server heartbeat**: a push the home server sends every minute from its crontab, with the token in the cron line. Silence means the server is off or offline.
- **Oracle Backup → GDrive**: pushed by the 04:00 upload (see the schedule): up when it ran, down with the reason when it skipped or failed.
- **Home NPM blocks the VM**: every 10 min Kuma opens an unpublished home app (`immich`, pinned to home's Tailscale IP with `extra_hosts`, since on the VM the name resolves to the VM itself) and only `403` counts as up. A `200` means home NPM no longer sees the VM's real IP (see [the home README](../README.md#how-it-fits-together)).

The UI is on `<TAILSCALE-IP>:3001` or `oracle-uptimekuma`, over Tailscale only. In the other direction, the home server opens Pangolin's dashboard every minute and pushes to the home Kuma ("Oracle gateway (Pangolin)").

## Dockhand (Hawser agent)

The home Dockhand manages this VM through [Hawser](docker/hawser/) in standard mode: home connects to `<TAILSCALE-IP>:2376` with a token, and the VM never connects to home.

- **Where the compose files live.** Dockhand reads them on the home server, in `/opt/docker/oracle/<app>/` (only the compose files, the same as in [`docker/`](docker/) here; configs, scripts and data live on the VM next to them). On *Save & redeploy*, Hawser writes the compose file to `/opt/docker/<app>/` on the VM and runs it there, so relative paths like `./conf` stay on the VM. Tested with a throwaway stack: existing data and the other stacks were untouched.
- **Keep both copies identical.** Adopting changes nothing; the first redeploy overwrites the VM's compose file with the home copy.
- **Removing a stack** in Dockhand only stops its containers and forgets it; files stay.
- **Adopted:** `adguardhome`, `pangolin`, `pocket-id`, `uptime-kuma`, `crowdsec`. The home copies of `pangolin` and `pocket-id` include their `.env` (mode 600), so a redeploy can't write an empty one to the VM.
- **Hawser itself stays untracked**: redeploying the agent through itself would cut its own connection, and its token would need a copy at home. Update it over SSH.
- **Not behind the gateway.** Hawser stays on the Tailscale IP only.

## Scheduled jobs (Oracle VM)

All times Europe/Berlin, read from the VM's timers and crontab. The slots match the home server ([schedule](../README.md#scheduled-jobs-home-server)): checks and cleanups at 01:00, database copies 03:00, backup 03:15, upload 04:00. OS updates install only after the backup and upload, and a reboot comes last. Unlike the home server, OS updates install themselves, because nobody logs in here every week. Rows marked *planned* are added as services move in.

| When | Job | Runs from |
|---|---|---|
| 00:00 | Log rotation (incl. Traefik's access log, 7 days kept) and package-database backup (Ubuntu defaults) | systemd |
| 01:00 | Dockhand update check, as on the home server | Dockhand, through its agent |
| Mon 01:00 | Docker cleanup: images and build cache unused for 7 days (same slot as Dockhand's prune at home) | user crontab |
| 02:00 | apt: download package lists | systemd `apt-daily.timer` |
| 03:00 | Database copies: consistent `.snapshot` copies of the 6 SQLite databases (Pocket ID, Pangolin, Kuma, Navidrome, CrowdSec, CrowdSec Web UI `state.db`) through SQLite's online backup, no container stop ([`host/oracle-db-snapshots.py`](host/oracle-db-snapshots.py)) | root crontab |
| 03:15 | Backrest: backup of `/opt/docker` (music and caches excluded); keeps 7 daily, 4 weekly, 6 monthly; prune and check on the 1st at 00:00 ([`docker/backrest`](docker/backrest/)) | Backrest |
| 04:00 | Upload of the Backrest repo to Google Drive (`System & Backups/Local Server Backup/oracle-backup (Backrest)`), only if tonight's database copies and backup exist ([`host/oracle-gdrive-upload.sh`](host/oracle-gdrive-upload.sh)); pushes up to Oracle's Kuma monitor "Oracle Backup → GDrive", or down with the reason (also logged) when it skips | root crontab |
| 04:15 | Unattended upgrades: Ubuntu security and regular updates; old kernels and unused packages removed | systemd `apt-daily-upgrade.timer` |
| 04:45 | Reboot, only if an update requires it | unattended-upgrades |
| Mon 01:10 | GeoIP: refresh the DE and KW country lists and re-apply the 80/443 rule | systemd `geoip-allow-update.timer` |
| Every 5 min | Home IP trust: home's internet IP (`yourdomain.dedyn.io`, no subdomain) counts like Tailscale, so admin pages open from home and Pangolin lets home through without its login. When the IP changes, the old `/32` is swapped for the new one in Traefik's rules, Pangolin's resource rules and CrowdSec's `homelab` allowlist ([`host/home-ip-trust.py`](host/home-ip-trust.py)) | root crontab |
| Every 60 s | Uptime Kuma checks the home internet (public DoT port); the home server pushes a heartbeat every minute, and checks Oracle's gateway for home Kuma | Uptime Kuma, home crontab |
| Weekly (Mon, random time) | SSD trim | systemd `fstrim.timer` |
| Daily | Traefik checks its certificates and renews them about 30 days before expiry | Traefik (Pangolin) |
| Continuous | Tailscale updates itself | Tailscale |

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
# AdGuard cert reload, 02:30 like at home: restarts AdGuard if Traefik (Pangolin) renewed the certificate
# PAUSED 2026-10-27: AdGuard on the VM is switched off
#30 2 * * * /opt/docker/adguardhome/cert-reload.sh >> /opt/docker/adguardhome/cert-reload.log 2>&1
```

`glance-agent` container ([compose](docker/glance-agent/docker-compose.yml)): [Glance agent](https://github.com/glanceapp/agent) that gives the home Dynacat "Oracle Server" widget the VM's CPU, RAM and disk usage. Tailscale only (`<TAILSCALE-IP>:27973`), every request needs the `TOKEN` from `.env`, read-only, no capabilities. The empty `disk/` folder is mounted so the agent can report the boot volume.

`oracle-keepalive` container ([compose](docker/oracle-keepalive/docker-compose.yml), [script](docker/oracle-keepalive/keepalive.py)): temporary guard against Oracle's idle reclaim, which stops an Always Free VM when CPU, network and memory all stay under 20% for 7 days. It holds 2 GB of RAM (about 35% of the VM in use) and runs a light CPU burst for 12 minutes each hour at the lowest CPU priority, with no network and a read-only filesystem. It runs as a container so Dockhand shows it. Remove it once real services keep the VM busy: `docker compose down` in `/opt/docker/oracle-keepalive`, then delete the folder.

`/etc/logrotate.d/pangolin-traefik`: see [`host/logrotate-pangolin-traefik`](host/logrotate-pangolin-traefik) (daily, 7 kept, `copytruncate` because Traefik keeps the file open).

`/etc/cloud/cloud.cfg.d/99-homelab-hostname.cfg`: hostname `oracle-homelab` (same as its Tailscale name and the Oracle instance name), kept across reboots
```yaml
# Homelab: keep our own hostname (cloud-init would reset it to the Oracle name on boot)
preserve_hostname: true
hostname: oracle-homelab
fqdn: oracle-homelab
```

Also: `rpcbind` disabled and masked (it listened on port 111 for nothing), timezone Europe/Berlin, `tailscale set --auto-update=true`, and `tailscale set --snat-subnet-routes=false`. Without that last one, Tailscale rewrites the source address of Tailscale connections to Docker's gateway (172.23.0.1) whenever its firewall rules land in front of Docker's (for example after an auto-update), and every "Tailscale only" rule answers 403. The VM routes no subnets, so nothing else changes.

## Connecting

Tailscale on, then `ssh ubuntu@<TAILSCALE-IP>` with that device's own key. The public IP has no SSH.
