# LSC Smart Plug – local controller

A small Python program that controls the **LSC Smart Connect power plug with
energy meter** (sold by Action; a Tuya device, protocol 3.5) directly over your
Wi-Fi. No Home Assistant, no Tuya cloud at runtime. One Python file, one HTML
page, a SQLite log. Runs on Windows, Linux, a Raspberry Pi or in Docker
(~120 MB image, ~40 MB RAM).

It exposes every native feature of the plug and adds the energy side the Tuya
app does badly: a local kWh log you own, cost, charts, per-device totals,
budget alerts and a monthly report.

## Features

**On the plug** (keep working when this program is off):

- On / off
- Countdown (switch after N minutes)
- Auto-off after switching on ("inching")
- Cycle timer (on X min / off Y min inside a time window)
- Random timer ("away mode")
- Relay status after a power cut: off / on / last state
- LED mode: on with relay, on when relay is off, always off, always on
  (this controls the red relay LED only; the blue Wi-Fi LED cannot be turned off)
- Child lock
- Overcharge protection (plug switches off when the load stays below ~3 W for
  40 minutes; leave it off for devices with standby, like an AC)
- Fault flags (overcurrent, overvoltage, overpower, …)

**In this program** (needs it running):

- Live W / V / A, refreshed every 5 s
- Energy log: one row per poll in SQLite, kWh integrated from the readings
- Cost per hour, per 24 h, today, this month, total, plus a month projection
- Charts like the Tuya app: Day (per hour), Week (per weekday), Month (per day),
  with ‹ › navigation and "Compare with previous" grey bars
- Runtime today / 7 days / 30 days
- "Plugged in now" label: every reading is stored with the name of the device
  in the plug, so usage stays separate when you move the plug (AC in summer,
  heater in winter). Filter totals, chart and export per device. "Fix a stretch"
  relabels readings between two times if you forgot to change the name.
- Weekly on/off schedule at fixed times (the Tuya app runs these from the cloud;
  here the program runs them)
- Alerts on the page and to Discord (webhook) or ntfy: power above a limit, plug
  fault, plug unreachable, back online, monthly budget exceeded
- Monthly report on the 1st (kWh, cost, hours on, busiest day, per-device split)
- CSV export of all readings (optionally one device)
- "Delete all measured data" button (type RESET)
- Finds the plug again by itself if its IP changes
- Demo mode with a simulated plug, so you can try the page without hardware

## Screens

The page is one column on a phone and two on a desktop:

| Left | Right |
|---|---|
| Live (switch, W/V/A, cost at this draw, fault) | Countdown |
| Energy & cost (totals, device label, chart, price, export, danger zone) | Auto-off |
| Alerts (limit, budget, monthly report, notify URL) | Cycle timer, Random timer |
| | Weekly schedule |
| | Plug settings (relay status, LED, child lock, overcharge) |

## Quick start

### Try it without a plug

```bash
pip install -r requirements.txt
python plug_server.py --demo
```

Open http://localhost:8081. Demo mode simulates a 1.4 kW load and writes to
separate `demo_*` files, so it never touches real data.

### One-time setup: get the plug's local key

Tuya encrypts local traffic with a per-device **local key**. You fetch it once
from Tuya's cloud; after that the cloud is not used.

1. Install the **Smart Life** app, create an account, pair the plug
   (2.4 GHz Wi-Fi only).
2. Create a free developer account at https://iot.tuya.com:
   - Cloud → Create Cloud Project. Pick the data center of your region
     (Central Europe for the EU).
   - Devices → Link Tuya App Account → scan the QR code with Smart Life.
     The plug appears in the project.
   - Note the project's **Access ID** and **Access Secret**.
3. Run the wizard on your PC:

   ```bash
   pip install tinytuya
   python -m tinytuya wizard
   ```

   Enter Access ID, Access Secret, any device ID from the app's device info
   page and your region (`eu`). Say yes to the network scan. It writes
   `devices.json` and `snapshot.json` with the plug's `id`, `key`, `ip` and
   `version`.
4. Copy `config.example.json` to `config.json` and fill in `device_id`,
   `local_key`, `ip` and `version` (3.5 for this plug).
5. Give the plug a **DHCP reservation** in your router so its IP stays fixed.
   (The program can re-find it on a changed IP, but a reservation avoids gaps.)
6. Delete the wizard files afterwards (`tinytuya.json`, `devices.json`,
   `tuya-raw.json`, `snapshot.json` in your home folder). They contain the API
   secret and the key. You can also delete the cloud project; that does not
   reset the plug.

### Run it

```bash
python plug_server.py
```

Open http://localhost:8080, or `http://<this machine's IP>:8080` from a phone on
the same Wi-Fi. On Windows, `start_server.bat` does the same and installs the
packages on first run.

## Docker

`docker/` has a `Dockerfile` (python:3.13-alpine, runs as uid 1000) and a
`docker-compose.yml`. Layout on the host:

```
smart-plug/
  docker-compose.yml
  Dockerfile
  .dockerignore
  plug_server.py  index.html  requirements.txt
  data/            # volume: config.json (key!), state.json, energy.db
```

```bash
mkdir -p data && cp config.json data/
docker compose up -d --build
```

Notes:

- `DATA_DIR=/data` is where config, state and the database live. Mount it.
- Set `TZ` to your time zone; schedules, charts and the monthly report use it.
- The compose file attaches the container to an external `proxy-net` network for
  a reverse proxy (Nginx Proxy Manager, Traefik, …) and publishes no host port.
  Without a proxy, remove the network block and uncomment `ports`.
- The plug is found by a TCP sweep of its /24 on port 6668, so the container
  does not need host networking.
- Only events are logged (switches, alerts, IP changes), not every request:
  `docker logs smart-plug`.
- After a code change: copy `plug_server.py` / `index.html` and run
  `docker compose up -d --build`.

**Stop any other copy of the program first.** Two programs polling the plug at
once make it drop connections.

### Reverse proxy

Point a host at `http://smart-plug:8080` (container name) with websockets on.
The page has **no login**. Keep it reachable only from your LAN or VPN
(Tailscale, WireGuard). If you ever expose it to the internet, put an access
list or auth in front of it.

## How the energy numbers work

- **Readings.** Every 5 s the program asks the plug for W, V and A and stores
  them. Energy (Wh) is the power integrated over the time since the previous
  reading. Gaps longer than 3 polls (program off, plug unreachable) count as
  zero, so a gap never inflates the total.
- **Price.** Set your per-kWh price (brutto working price) on the page. The
  fixed monthly base fee is left out on purpose: you pay it whether the device
  runs or not.
- **"Logged X % of today"** shows how much of today was actually measured.
  Energy is only counted while the program runs.
- **Month projection** takes the average draw over the measured time this month
  and scales it to the whole month. It needs at least one hour of data; trust
  it after about a week.
- **Accuracy.** The plug's meter is roughly ±1–2 %. The plug's own draw
  (~0.5 W) is not measured. Wiring losses are well under 1 %. For a household
  bill comparison the number is as good as the meter.
- **Size.** One row every 5 s is about 17 000 rows a day, roughly 300 MB per
  year. Export per device or per period if you want to analyse it elsewhere.

## Plugged in now

Set the name of the device that is in the plug before you move it. Every reading
is stored with that label. "Show" filters totals and the chart to one device,
the per-device table lists runtime, kWh and cost for each, and the CSV export
has a `device` column. "Fix a stretch" relabels all readings between two times.

## Alerts, budget and report

In the Alerts box:

- **Power above N W** → alert when the live draw exceeds it.
- **Monthly budget** → one alert per month when this month's cost (all devices)
  reaches the limit. Checked every 5 minutes.
- **Monthly report** → on the first run in a new month, last month's kWh, cost,
  hours on, busiest day and per-device split. "Send report now" sends it
  immediately.
- **Notify URL** → a Discord webhook URL or an ntfy topic URL. "Send test"
  checks it. Also used for fault / unreachable / back-online alerts.

## HTTP API

Everything the page does goes through these endpoints, so you can script it
(curl, Home Assistant REST, a shortcut on your phone).

| Method | Path | What |
|---|---|---|
| GET | `/api/status?device=` | live state, energy summary, schedules, alert settings, recent alerts |
| GET | `/api/info` | plug name, model, ID, IP, protocol |
| GET | `/api/chart?range=day\|week\|month&date=YYYY-MM-DD&device=` | chart buckets, totals, previous period |
| GET | `/api/export/readings.csv?device=` | all readings as CSV |
| POST | `/api/switch` `{"on": true}` | switch |
| POST | `/api/device` `{"setting": "countdown\|power_on\|led\|child_lock\|overcurrent\|inching\|cycle\|random", "value": …}` | on-plug setting |
| POST | `/api/schedules` `{"time":"07:00","action":"on","days":["mon",…]}` / `{"delete": i}` / `{"toggle": i}` | weekly schedule |
| POST | `/api/settings` `{"price_per_kwh", "device", "power_above_w", "budget_eur", "monthly_report", "notify_url"}` | any subset |
| POST | `/api/relabel` `{"from":"YYYY-MM-DDTHH:MM","to":…,"device":"name"}` | relabel readings |
| POST | `/api/test-notify`, `/api/test-report` | send a test message / last month's report |
| POST | `/api/reset` `{"confirm":"RESET"}` | delete all readings |

## Files

| File | What it is |
|---|---|
| `plug_server.py` | the program: Flask server, poll loop, schedules, alerts |
| `index.html` | the web page (no build step, no framework) |
| `config.example.json` | template; copy to `config.json` |
| `config.json` | plug ID, IP, **local key – never share or commit** |
| `state.json` | schedules, price, device label, alert settings (created automatically) |
| `energy.db` | SQLite log of readings (created automatically) |
| `demo_state.json`, `demo_energy.db` | demo-mode counterparts |
| `start_server.bat` | Windows one-click start |
| `docker/` | Dockerfile, docker-compose.yml, .dockerignore |

`config.json` fields: `device_id`, `local_key`, `ip`, `version`, `poll_seconds`
(5), `port` (8080), `dps` (data point numbers: switch 1, current 18, power 19,
voltage 20), `price_per_kwh`. If the wizard's `snapshot.json` shows different
DP numbers for your plug, edit `dps`.

## Things not to do

- **Don't remove the plug from the Smart Life app and don't delete that
  account** while the plug can reach the internet. Removing it sends a factory
  reset, which generates a new local key. You would have to pair again and rerun
  the wizard. Keeping the app is harmless: it, Google Home and this program can
  all control the plug at the same time.
- **Don't long-press the plug's button** (5 s+). Same reset.
- If your router can block a device's internet access, you *can* cut the cloud:
  confirm this program works, block the plug, and only then remove it from the
  app. The reset command never arrives and the key stays valid.

## Changing the router

The plug remembers the Wi-Fi name and password, not the router.

- Same name, same password, 2.4 GHz on: the plug reconnects by itself. It may
  get a new IP; the program finds it within about 30 s and updates
  `config.json`. Re-add the DHCP reservation.
- Different name or password: pair again in Smart Life, which creates a new
  local key. Rerun the wizard and update `config.json`.

## Limitations

- Fixed-time weekly schedules live in this program, not on the plug (same as
  the Tuya app, which runs them from the cloud). If the program is off, they
  don't fire. The on-plug timers (countdown, cycle, random, inching) do.
- Energy is only logged while the program runs. There is no import from the
  Tuya cloud to fill gaps.
- One plug per instance. For a second plug, run a second container with its own
  `data/` folder and port.
- No authentication on the page.

## Running permanently without Docker

Windows: Task Scheduler, "at logon", run `pythonw plug_server.py` in this folder.

Linux / Raspberry Pi, a systemd unit:

```ini
[Unit]
Description=Smart plug controller
After=network.target
[Service]
WorkingDirectory=/home/pi/smart-plug
ExecStart=/usr/bin/python3 plug_server.py
Restart=always
[Install]
WantedBy=multi-user.target
```

## Thanks

Built on [tinytuya](https://github.com/jasonacox/tinytuya) and Flask.
