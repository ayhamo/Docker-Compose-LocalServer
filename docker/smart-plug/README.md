# Smart Plug

Local web UI for LSC (Tuya) smart plugs: switching, schedules, energy log, cost and alerts.

The code lives in its own repo, [ayhamo/LSC-Smart-Plug](https://github.com/ayhamo/LSC-Smart-Plug). Its GitHub Actions run the tests and build `ghcr.io/ayhamo/lsc-smart-plug:latest` on every push and on the 1st of each month. This folder only holds the compose file that runs that image.

- **Data:** `./data` holds `config.json` (plug IDs, IPs, local keys), `state.json` and `energy.db`.
- **Update:** `docker compose pull && docker compose up -d`.
- **SSD writes:** since 17 Nov 2026 the readings go through one open SQLite connection and SQLite's temp tables stay in RAM. Before, it wrote 7–70 GB a day to the server's SSD (a sync on every 5-second reading, plus ~25 MB temp files on every page refresh). Now it writes about 0.4 GB.
