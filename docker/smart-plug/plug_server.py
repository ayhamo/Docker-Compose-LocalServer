"""
Local controller for the LSC Smart Connect plug with power meter (Tuya, protocol 3.5).

Runs entirely on your LAN: no Tuya cloud, no Home Assistant.

On-plug features (keep working when this program is off):
    countdown, inching (auto-off), cycle timer, random timer,
    power-on behaviour, LED mode, child lock, overcurrent protection
Program features (need this program running):
    energy log + cost, weekly on/off schedules, alerts, auto-rediscovery

Usage:
    python plug_server.py            # real plug, reads config.json
    python plug_server.py --demo     # simulated plug
"""

import base64
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from calendar import monthrange
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)      # config/state/db live here (a volume in Docker)
CONFIG_PATH = os.path.join(DATA, "config.json")
STATE_PATH = os.path.join(DATA, "state.json")
DB_PATH = os.path.join(DATA, "energy.db")
DEMO = "--demo" in sys.argv

# Silence the per-request access log ("GET /api/status 200"); keep warnings/errors
import logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

# Data points of "LSC Power Plug EU incl. Power meter" (verified on the device)
DP_SWITCH, DP_COUNTDOWN = "1", "9"
DP_CURRENT, DP_POWER, DP_VOLTAGE = "18", "19", "20"   # mA, W*10, V*10
DP_FAULT, DP_POWER_ON, DP_OVERCURRENT = "26", "38", "39"
DP_LED, DP_CHILD_LOCK = "40", "41"
DP_CYCLE, DP_RANDOM, DP_INCHING = "42", "43", "44"
ENERGY_DPS = [int(DP_CURRENT), int(DP_POWER), int(DP_VOLTAGE)]

POWER_ON_VALUES = ["off", "on", "memory"]
LED_VALUES = ["relay", "pos", "none", "on"]   # verified on the device
FAULT_BITS = ["overcurrent", "overvoltage", "overpower",
              "low current", "low voltage", "low power"]
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]   # bit 0 .. bit 6

# --------------------------------------------------------------------------
# Binary formats of the on-plug timers (base64 strings)
# --------------------------------------------------------------------------

def _days_to_mask(days):
    return sum(1 << DAYS.index(d) for d in days)


def _mask_to_days(mask):
    return [d for i, d in enumerate(DAYS) if mask & (1 << i)]


def _hm_to_min(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _min_to_hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _b64(raw):
    return base64.b64encode(raw).decode() if raw else ""


def _unb64(s):
    try:
        return base64.b64decode(s) if s else b""
    except Exception:
        return b""


def encode_cycle(tasks):
    """10 bytes/task: enable, weekdays, start, end, on-min, off-min (big endian)."""
    raw = b""
    for t in tasks[:10]:
        raw += bytes([1 if t.get("enabled", True) else 0, _days_to_mask(t["days"])])
        raw += _hm_to_min(t["start"]).to_bytes(2, "big")
        raw += _hm_to_min(t["end"]).to_bytes(2, "big")
        raw += int(t["on_min"]).to_bytes(2, "big")
        raw += int(t["off_min"]).to_bytes(2, "big")
    return _b64(raw)


def decode_cycle(s):
    raw, out = _unb64(s), []
    for i in range(0, len(raw) - 9, 10):
        b = raw[i:i + 10]
        out.append({"enabled": bool(b[0] & 1), "days": _mask_to_days(b[1]),
                    "start": _min_to_hm(int.from_bytes(b[2:4], "big")),
                    "end": _min_to_hm(int.from_bytes(b[4:6], "big")),
                    "on_min": int.from_bytes(b[6:8], "big"),
                    "off_min": int.from_bytes(b[8:10], "big")})
    return out


def encode_random(tasks):
    """6 bytes/task: enable, weekdays, start, end (big endian minutes)."""
    raw = b""
    for t in tasks[:16]:
        raw += bytes([1 if t.get("enabled", True) else 0, _days_to_mask(t["days"])])
        raw += _hm_to_min(t["start"]).to_bytes(2, "big")
        raw += _hm_to_min(t["end"]).to_bytes(2, "big")
    return _b64(raw)


def decode_random(s):
    raw, out = _unb64(s), []
    for i in range(0, len(raw) - 5, 6):
        b = raw[i:i + 6]
        out.append({"enabled": bool(b[0] & 1), "days": _mask_to_days(b[1]),
                    "start": _min_to_hm(int.from_bytes(b[2:4], "big")),
                    "end": _min_to_hm(int.from_bytes(b[4:6], "big"))})
    return out


def encode_inching(enabled, seconds):
    return _b64(bytes([1 if enabled else 0]) + int(seconds).to_bytes(2, "big"))


def decode_inching(s):
    raw = _unb64(s)
    if len(raw) < 3:
        return {"enabled": False, "seconds": 5}
    return {"enabled": bool(raw[0] & 1), "seconds": int.from_bytes(raw[1:3], "big")}


# --------------------------------------------------------------------------
# Plug backends: get_dps() -> dict, set_dp(dp, value)
# --------------------------------------------------------------------------

class DemoPlug:
    """Simulated plug that behaves like the real one, for trying the UI."""
    def __init__(self):
        self.d = {DP_SWITCH: False, DP_COUNTDOWN: 0, DP_FAULT: 0,
                  DP_POWER_ON: "memory", DP_OVERCURRENT: False, DP_LED: "relay",
                  DP_CHILD_LOCK: False, DP_CYCLE: "", DP_RANDOM: "", DP_INCHING: ""}
        self.t0 = time.time()
        self.cd_end = None
        self.inch_end = None

    def _tick(self):
        now = time.time()
        if self.cd_end and now >= self.cd_end:
            self.d[DP_SWITCH] = not self.d[DP_SWITCH]
            self.cd_end = None
        if self.inch_end and now >= self.inch_end:
            self.d[DP_SWITCH] = False
            self.inch_end = None
        self.d[DP_COUNTDOWN] = int(self.cd_end - now) if self.cd_end else 0

    def get_dps(self):
        self._tick()
        w = (1400 + 120 * math.sin((time.time() - self.t0) / 9)) if self.d[DP_SWITCH] else 0
        self.d[DP_POWER] = int(w * 10)
        self.d[DP_VOLTAGE] = 2318
        self.d[DP_CURRENT] = int(w / 231.8 * 1000)
        return dict(self.d)

    def set_dp(self, dp, value):
        dp = str(dp)
        self.d[dp] = value
        if dp == DP_COUNTDOWN:
            self.cd_end = time.time() + int(value) if int(value) else None
        if dp == DP_SWITCH and value:
            inch = decode_inching(self.d[DP_INCHING])
            if inch["enabled"]:
                self.inch_end = time.time() + inch["seconds"]


class TuyaPlug:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._connect()

    def _connect(self):
        import tinytuya
        self.dev = tinytuya.OutletDevice(
            dev_id=self.cfg["device_id"], address=self.cfg["ip"],
            local_key=self.cfg["local_key"], version=float(self.cfg.get("version", 3.5)))
        # A fresh short connection per request. A long-lived connection collected
        # unread replies from the plug, so readings fell further and further behind.
        self.dev.set_socketPersistent(False)
        self.dev.set_socketRetryLimit(2)
        self.dev.set_socketTimeout(5)

    def _sweep(self):
        """Find the plug without UDP broadcasts (these don't reach a Docker bridge):
        try every host in the plug's /24 on Tuya's TCP port 6668, then confirm
        it answers with our device ID and key."""
        import socket
        import tinytuya
        from concurrent.futures import ThreadPoolExecutor
        prefix = self.cfg["ip"].rsplit(".", 1)[0]

        def port_open(ip):
            try:
                with socket.create_connection((ip, 6668), timeout=0.4):
                    return ip
            except OSError:
                return None

        with ThreadPoolExecutor(64) as ex:
            candidates = [ip for ip in ex.map(port_open, (f"{prefix}.{i}" for i in range(1, 255))) if ip]
        for ip in candidates:
            d = tinytuya.OutletDevice(self.cfg["device_id"], ip, self.cfg["local_key"],
                                      version=float(self.cfg.get("version", 3.5)))
            d.set_socketTimeout(3)
            d.set_socketRetryLimit(1)
            r = d.status()
            d.close()
            if r and "dps" in r:
                return ip
        return None

    def rediscover(self):
        """Find the plug by device ID if its IP changed. Returns new IP or None."""
        import tinytuya
        ip = None
        try:
            found = tinytuya.find_device(dev_id=self.cfg["device_id"])
            ip = found.get("ip") if found else None
        except Exception:
            pass
        if not ip:
            ip = self._sweep()
        if ip and ip != self.cfg["ip"]:
            with self.lock:
                self.cfg["ip"] = ip
                try:
                    self.dev.close()
                except Exception:
                    pass
                self._connect()
            # Persist so the next start uses the new IP
            with open(CONFIG_PATH) as f:
                disk = json.load(f)
            disk["ip"] = ip
            with open(CONFIG_PATH, "w") as f:
                json.dump(disk, f, indent=2)
            return ip
        return None

    def get_dps(self):
        with self.lock:
            try:
                # Ask for fresh power readings. This plug never replies to the
                # request itself, so don't wait for one (waiting cost 5 s).
                self.dev.updatedps(ENERGY_DPS, nowait=True)
            except Exception:
                pass
            raw = self.dev.status()
        if not raw or "Error" in raw or "dps" not in raw:
            raise RuntimeError((raw or {}).get("Error", "no response"))
        return raw["dps"]

    def set_dp(self, dp, value):
        with self.lock:
            r = self.dev.set_value(int(dp), value)
        if r and "Error" in r:
            raise RuntimeError(r["Error"])
        if r and isinstance(r.get("dps"), dict):
            last["dps"].update(r["dps"])   # the plug confirms the new value right away


# --------------------------------------------------------------------------
# State (server-side schedules, settings, alerts)
# --------------------------------------------------------------------------

state_lock = threading.Lock()
state = {
    "schedules": [],
    "price_per_kwh": 0.3205,
    "device": "AC",          # label stamped on every reading: what is plugged in right now
    "alerts": {"power_above_w": 0, "notify_url": "", "budget_eur": 0, "monthly_report": False},
    "last_report": "",       # "YYYY-MM" of the month the last report was sent in
}
last = {"dps": {}, "error": None, "ts": 0, "fail_count": 0}
alerts_log = []          # recent alert messages shown on the page
_alert_flags = {}        # alert key -> currently active


def load_state(cfg):
    if "price_per_kwh" in cfg:
        state["price_per_kwh"] = cfg["price_per_kwh"]
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            saved = json.load(f)
        saved.pop("timer", None)      # old server-side timer, replaced by on-plug countdown
        state.update(saved)


def save_state():
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def raise_alert(key, active, message):
    """Record an alert once when it becomes active."""
    was = _alert_flags.get(key, False)
    _alert_flags[key] = active
    if active and not was:
        alerts_log.insert(0, {"ts": time.time(), "msg": message})
        del alerts_log[50:]
        log(f"ALERT: {message}")
        threading.Thread(target=notify, args=(message,), daemon=True).start()


def notify(message):
    """Send a message to the configured URL. Returns None on success, else the error.
    Discord webhook URLs get Discord's JSON format; anything else (e.g. ntfy)
    gets the message as plain text."""
    url = (state["alerts"].get("notify_url") or "").strip()
    if not url:
        return "no notification URL set"
    name = cfg.get("name", "Smart Plug")
    headers = {"User-Agent": "SmartPlug/1.0"}   # Discord rejects Python's default agent
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        body = json.dumps({"username": name, "content": f"**{name}:** {message}"}).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = message.encode()
        headers["Title"] = name
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers,
                                                      method="POST"), timeout=8)
        return None
    except Exception as e:
        log(f"notify failed: {e}")
        return str(e)


# --------------------------------------------------------------------------
# Energy database
# --------------------------------------------------------------------------

_db_ready = False


def db():
    global _db_ready
    conn = sqlite3.connect(DB_PATH)
    if not _db_ready:
        conn.execute("""CREATE TABLE IF NOT EXISTS readings (
            ts INTEGER PRIMARY KEY, on_state INTEGER, power_w REAL,
            voltage_v REAL, current_a REAL, wh REAL, device TEXT NOT NULL DEFAULT 'AC')""")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(readings)")]
        if "device" not in cols:   # database from before the device label existed
            conn.execute("ALTER TABLE readings ADD COLUMN device TEXT NOT NULL DEFAULT 'AC'")
            conn.commit()
        _db_ready = True
    return conn


def log_reading(st, elapsed_s):
    p = st.get("power_w") or 0.0
    conn = db()
    conn.execute("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                 (int(time.time()), int(st["on"]), p, st.get("voltage_v"),
                  st.get("current_a"), p * elapsed_s / 3600.0, state["device"]))
    conn.commit()
    conn.close()


def dev_where(device):
    """SQL fragment + params that limit a query to one device label ('' = all)."""
    return (" AND device = ?", (device,)) if device else ("", ())


def period_stats(conn, since, poll, price, device=""):
    """Runtime (relay on), energy and cost since a timestamp, plus per-day averages.
    Runtime adds the gap to the previous reading whenever the plug was on
    (capped, so time when the program was off is not counted)."""
    w, p = dev_where(device)
    runtime, wh, days = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL
                                 THEN MIN(ts - prev, ?) ELSE 0 END), 0),
               COALESCE(SUM(wh), 0),
               COUNT(DISTINCT date(ts, 'unixepoch', 'localtime'))
        FROM (SELECT ts, on_state, wh, LAG(ts) OVER (ORDER BY ts) AS prev
              FROM readings WHERE ts >= ?{w})""", (poll * 3, since) + p).fetchone()
    d = max(days, 1)
    return {"runtime_s": int(runtime), "kwh": round(wh / 1000, 3),
            "eur": round(wh / 1000 * price, 2), "days_with_data": days,
            "avg_runtime_s": int(runtime / d), "avg_kwh": round(wh / 1000 / d, 3),
            "avg_eur": round(wh / 1000 * price / d, 2)}


def measured_seconds(conn, since, poll, device=""):
    """Seconds covered by readings since a timestamp (gaps > 3 polls don't count)."""
    w, p = dev_where(device)
    return conn.execute(f"""
        SELECT COALESCE(SUM(MIN(ts - prev, ?)), 0)
        FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev FROM readings WHERE ts >= ?{w})
        WHERE prev IS NOT NULL""", (poll * 3, since) + p).fetchone()[0]


def device_totals(conn, poll, price):
    """One row per device label ever logged: runtime, kWh, cost, first and last day."""
    rows = conn.execute("""
        SELECT device,
               SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL THEN MIN(ts - prev, ?) ELSE 0 END),
               SUM(wh), MIN(ts), MAX(ts)
        FROM (SELECT device, ts, on_state, wh,
                     LAG(ts) OVER (PARTITION BY device ORDER BY ts) AS prev FROM readings)
        GROUP BY device ORDER BY MAX(ts) DESC""", (poll * 3,)).fetchall()
    return [{"device": r[0], "runtime_s": int(r[1] or 0), "kwh": round((r[2] or 0) / 1000, 3),
             "eur": round((r[2] or 0) / 1000 * price, 2),
             "first": datetime.fromtimestamp(r[3]).strftime("%d.%m.%Y"),
             "last": datetime.fromtimestamp(r[4]).strftime("%d.%m.%Y")} for r in rows]


def chart_data(rng, date, price, device="", compare=True):
    """kWh per bucket like the Tuya app: day = 24 hours, week = Mon–Sun, month = 1..31.
    With compare, the previous day/week/month is included as `prev`."""
    d = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    d = datetime(d.year, d.month, d.day)
    if compare:
        if rng == "day":
            pd = d - timedelta(days=1)
        elif rng == "week":
            pd = d - timedelta(days=7)
        else:
            pd = datetime(d.year, d.month, 1) - timedelta(days=1)
        prev = chart_data(rng, pd.strftime("%Y-%m-%d"), price, device, compare=False)
        prev = {k: prev[k] for k in ("kwh", "title", "total_kwh", "total_eur")}
    if rng == "day":
        start, n = d, 24
        end = d + timedelta(days=1)
        key, labels = "CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER)", list(range(24))
        title = f"{d:%a %d.%m.%Y}"
    elif rng == "week":
        start = d - timedelta(days=d.weekday())
        end, n = start + timedelta(days=7), 7
        key = "CAST(julianday(date(ts, 'unixepoch', 'localtime')) - julianday(?) AS INTEGER)"
        labels = [(start + timedelta(days=i)).strftime("%a %d.%m") for i in range(7)]
        title = f"{start:%d.%m.} – {end - timedelta(days=1):%d.%m.%Y}"
    else:
        start = datetime(d.year, d.month, 1)
        n = monthrange(d.year, d.month)[1]
        end = start + timedelta(days=n)
        key = "CAST(strftime('%d', ts, 'unixepoch', 'localtime') AS INTEGER) - 1"
        labels = list(range(1, n + 1))
        title = f"{start:%B %Y}"
    w, p = dev_where(device)
    args = ((start.strftime("%Y-%m-%d"),) if rng == "week" else ()) + \
           (int(start.timestamp()), int(end.timestamp())) + p
    conn = db()
    rows = conn.execute(f"SELECT {key} k, SUM(wh) FROM readings "
                        f"WHERE ts >= ? AND ts < ?{w} GROUP BY k", args).fetchall()
    conn.close()
    wh = [0.0] * n
    for k, v in rows:
        if 0 <= k < n:
            wh[k] += v
    total = sum(wh)
    return {"range": rng, "date": d.strftime("%Y-%m-%d"), "title": title,
            "labels": labels, "kwh": [round(x / 1000, 4) for x in wh],
            "total_kwh": round(total / 1000, 3), "total_eur": round(total / 1000 * price, 2),
            "has_data": [x > 0 for x in wh], "prev": prev if compare else None}


def month_report(year, month, price, poll):
    """Text summary of one calendar month for the Discord report."""
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    conn = db()
    rows = conn.execute("""
        SELECT device, SUM(wh), SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL
                                         THEN MIN(ts - prev, ?) ELSE 0 END)
        FROM (SELECT device, ts, on_state, wh,
                     LAG(ts) OVER (PARTITION BY device ORDER BY ts) AS prev
              FROM readings WHERE ts >= ? AND ts < ?)
        GROUP BY device ORDER BY SUM(wh) DESC""",
        (poll * 3, int(start.timestamp()), int(end.timestamp()))).fetchall()
    busiest = conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') d, SUM(wh) FROM readings
        WHERE ts >= ? AND ts < ? GROUP BY d ORDER BY SUM(wh) DESC LIMIT 1""",
        (int(start.timestamp()), int(end.timestamp()))).fetchone()
    conn.close()
    if not rows:
        return None
    wh = sum(r[1] for r in rows)
    runtime = sum(r[2] for r in rows)
    lines = [f"Report for {start:%B %Y}: {wh / 1000:.2f} kWh = {wh / 1000 * price:.2f} €, "
             f"switched on for {runtime / 3600:.1f} h."]
    if busiest:
        bd = datetime.strptime(busiest[0], "%Y-%m-%d")
        lines.append(f"Busiest day: {bd:%a %d.%m.} with {busiest[1] / 1000:.2f} kWh "
                     f"({busiest[1] / 1000 * price:.2f} €).")
    if len(rows) > 1:
        lines.append("Per device: " + ", ".join(
            f"{r[0]} {r[1] / 1000:.2f} kWh ({r[1] / 1000 * price:.2f} €)" for r in rows))
    return "\n".join(lines)


def monthly_checks():
    """Budget alert and the monthly report. Called from the poll loop every few minutes."""
    n = datetime.now()
    price = state["price_per_kwh"]
    poll = cfg.get("poll_seconds", 10)
    budget = state["alerts"].get("budget_eur") or 0
    if budget > 0:
        mon0 = int(datetime(n.year, n.month, 1).timestamp())
        conn = db()
        wh = conn.execute("SELECT COALESCE(SUM(wh), 0) FROM readings WHERE ts >= ?",
                          (mon0,)).fetchone()[0]
        conn.close()
        cost = wh / 1000 * price
        raise_alert(f"budget-{n:%Y-%m}", cost >= budget,
                    f"This month's cost is {cost:.2f} €, over your budget of {budget:.2f} €")
    this_month = n.strftime("%Y-%m")
    if state["alerts"].get("monthly_report") and state.get("last_report") != this_month:
        prev_last_day = datetime(n.year, n.month, 1) - timedelta(days=1)
        text = month_report(prev_last_day.year, prev_last_day.month, price, poll)
        with state_lock:
            state["last_report"] = this_month     # once per month, even if there was no data
            save_state()
        if text:
            log("monthly report sent")
            threading.Thread(target=notify, args=(text,), daemon=True).start()


def energy_summary(price, live_w, poll, device=""):
    """Totals for the page. `device` limits everything to one label ('' = all)."""
    conn = db()
    now = datetime.now()
    day0 = int(datetime(now.year, now.month, now.day).timestamp())
    mon0 = int(datetime(now.year, now.month, 1).timestamp())
    w, p = dev_where(device)
    q = f"SELECT COALESCE(SUM(wh),0), COUNT(*) FROM readings WHERE ts >= ?{w}"
    today, n_today = conn.execute(q, (day0,) + p).fetchone()
    month, _ = conn.execute(q, (mon0,) + p).fetchone()
    total = conn.execute(q, (0,) + p).fetchone()[0]
    periods = {
        "today": period_stats(conn, day0, poll, price, device),
        "d7": period_stats(conn, day0 - 6 * 86400, poll, price, device),
        "d30": period_stats(conn, day0 - 29 * 86400, poll, price, device),
    }
    measured_s = measured_seconds(conn, mon0, poll, device)
    devices = device_totals(conn, poll, price)
    conn.close()

    # Month projection: average draw over the time actually measured this month,
    # applied to the whole month. Gaps (program off) are not counted as zero.
    days_in_month = monthrange(now.year, now.month)[1]
    proj_kwh = None
    if measured_s >= 3600:
        proj_kwh = month / 1000 / measured_s * days_in_month * 86400
    # Share of today that was actually logged (program running and plug reachable)
    secs_today = max(time.time() - day0, 1)
    coverage = min(100, round(n_today * poll / secs_today * 100))

    kwh = lambda wh: round(wh / 1000, 3)
    eur = lambda wh: round(wh / 1000 * price, 2)
    return {
        "price_per_kwh": price,
        "today_kwh": kwh(today), "today_eur": eur(today),
        "month_kwh": kwh(month), "month_eur": eur(month),
        "total_kwh": kwh(total), "total_eur": eur(total),
        "now_eur_per_hour": round((live_w or 0) / 1000 * price, 3),
        "now_eur_per_day": round((live_w or 0) / 1000 * 24 * price, 2),
        "projected_month_kwh": None if proj_kwh is None else round(proj_kwh, 1),
        "projected_month_eur": None if proj_kwh is None else round(proj_kwh * price, 2),
        "coverage_today_pct": coverage,
        "measured_month_h": round(measured_s / 3600, 1),
        "periods": periods,
        "device": device, "current_device": state["device"], "devices": devices,
    }


# --------------------------------------------------------------------------
# Parsing raw data points into something readable
# --------------------------------------------------------------------------

def parse(d):
    def scaled(dp, scale):
        v = d.get(dp)
        return None if v is None else round(v / scale, 3)
    fault = d.get(DP_FAULT) or 0
    return {
        "on": bool(d.get(DP_SWITCH, False)),
        "power_w": scaled(DP_POWER, 10),
        "voltage_v": scaled(DP_VOLTAGE, 10),
        "current_a": scaled(DP_CURRENT, 1000),
        "countdown_s": d.get(DP_COUNTDOWN, 0),
        "faults": [name for i, name in enumerate(FAULT_BITS) if fault & (1 << i)],
        "power_on": d.get(DP_POWER_ON),
        "overcurrent": d.get(DP_OVERCURRENT),
        "led": d.get(DP_LED),
        "child_lock": d.get(DP_CHILD_LOCK),
        "inching": decode_inching(d.get(DP_INCHING, "")),
        "cycle": decode_cycle(d.get(DP_CYCLE, "")),
        "random": decode_random(d.get(DP_RANDOM, "")),
    }


# --------------------------------------------------------------------------
# Background loop
# --------------------------------------------------------------------------

def set_plug(on, reason=""):
    try:
        plug.set_dp(DP_SWITCH, bool(on))
        log(f"plug -> {'ON' if on else 'OFF'} {reason}")
    except Exception as e:
        log(f"set failed: {e}")


def loop(interval):
    last_ts = time.time()
    fired = set()
    tick = 0
    while True:
        now = time.time()
        tick += 1
        if tick % max(1, 300 // interval) == 1:     # about every 5 minutes
            try:
                monthly_checks()
            except Exception as e:
                log(f"monthly checks failed: {e}")
        try:
            dps = plug.get_dps()
            last["dps"].update(dps)
            st = parse(last["dps"])
            # Cap the step so a sleep/hibernate gap is not counted as usage
            log_reading(st, min(now - last_ts, interval * 3))
            if last["fail_count"] >= 3:
                log("plug reachable again")
                threading.Thread(target=notify, args=("Plug is reachable again",), daemon=True).start()
            last.update(error=None, ts=now, fail_count=0)
            raise_alert("offline", False, "")
            raise_alert("fault", bool(st["faults"]),
                        "Plug reports fault: " + ", ".join(st["faults"]))
            limit = state["alerts"].get("power_above_w") or 0
            raise_alert("power", bool(limit) and (st["power_w"] or 0) > limit,
                        f"Power {st['power_w']:.0f} W is above your limit of {limit} W")
        except Exception as e:
            last["fail_count"] += 1
            last.update(error=str(e), ts=now)
            if last["fail_count"] == 3:
                raise_alert("offline", True, "Plug is not reachable")
                if hasattr(plug, "rediscover"):
                    try:
                        ip = plug.rediscover()
                        if ip:
                            log(f"plug found at new IP {ip}, config.json updated")
                    except Exception as e2:
                        log(f"rediscover failed: {e2}")
            if last["fail_count"] % 30 == 0 and hasattr(plug, "rediscover"):
                try:
                    plug.rediscover()
                except Exception:
                    pass
        last_ts = now

        with state_lock:
            n = datetime.now()
            key_min = n.strftime("%Y-%m-%d %H:%M")
            today = DAYS[(n.weekday() + 1) % 7]
            for i, s in enumerate(state["schedules"]):
                if not s.get("enabled", True) or today not in s["days"]:
                    continue
                if s["time"] == n.strftime("%H:%M") and (i, key_min) not in fired:
                    fired.add((i, key_min))
                    set_plug(s["action"] == "on", f"(schedule {s['time']})")
            if len(fired) > 500:
                fired.clear()

        time.sleep(interval)


# --------------------------------------------------------------------------
# Web API
# --------------------------------------------------------------------------

app = Flask(__name__)


def refresh_now():
    try:
        last["dps"].update(plug.get_dps())
        last.update(error=None, ts=time.time())
    except Exception as e:
        last.update(error=str(e))


@app.get("/api/status")
def api_status():
    st = parse(last["dps"]) if last["dps"] else None
    with state_lock:
        return jsonify(
            status=st, error=last["error"], updated=last["ts"],
            schedules=state["schedules"], alerts_cfg=state["alerts"],
            alerts=alerts_log[:10],
            energy=energy_summary(state["price_per_kwh"],
                                  st["power_w"] if st and st["on"] else 0,
                                  cfg.get("poll_seconds", 10),
                                  request.args.get("device", "")))


@app.post("/api/switch")
def api_switch():
    set_plug(request.json.get("on"), "(manual)")
    refresh_now()
    return jsonify(ok=True)


@app.post("/api/device")
def api_device():
    """Write an on-plug setting. Body: {"setting": name, "value": ...}"""
    b = request.json
    name, v = b["setting"], b.get("value")
    if name == "countdown":
        plug.set_dp(DP_COUNTDOWN, max(0, min(86400, int(v))))
    elif name == "power_on":
        assert v in POWER_ON_VALUES
        plug.set_dp(DP_POWER_ON, v)
    elif name == "led":
        assert v in LED_VALUES
        plug.set_dp(DP_LED, v)
    elif name == "child_lock":
        plug.set_dp(DP_CHILD_LOCK, bool(v))
    elif name == "overcurrent":
        plug.set_dp(DP_OVERCURRENT, bool(v))
    elif name == "inching":
        plug.set_dp(DP_INCHING, encode_inching(v["enabled"], max(1, min(65535, int(v["seconds"])))))
    elif name == "cycle":
        plug.set_dp(DP_CYCLE, encode_cycle(v))
    elif name == "random":
        plug.set_dp(DP_RANDOM, encode_random(v))
    else:
        return jsonify(ok=False, error="unknown setting"), 400
    time.sleep(0.5)
    refresh_now()
    return jsonify(ok=True)


@app.post("/api/schedules")
def api_schedules():
    body = request.json
    with state_lock:
        if "delete" in body:
            state["schedules"].pop(int(body["delete"]))
        elif "toggle" in body:
            s = state["schedules"][int(body["toggle"])]
            s["enabled"] = not s.get("enabled", True)
        else:
            state["schedules"].append({"time": body["time"], "action": body["action"],
                                       "days": body["days"], "enabled": True})
            state["schedules"].sort(key=lambda s: s["time"])
        save_state()
    return jsonify(ok=True)


@app.post("/api/settings")
def api_settings():
    b = request.json
    with state_lock:
        if "price_per_kwh" in b:
            state["price_per_kwh"] = float(b["price_per_kwh"])
        if "power_above_w" in b:
            state["alerts"]["power_above_w"] = float(b["power_above_w"] or 0)
        if "notify_url" in b:
            state["alerts"]["notify_url"] = (b["notify_url"] or "").strip()
        if "budget_eur" in b:
            state["alerts"]["budget_eur"] = max(0.0, float(b["budget_eur"] or 0))
        if "monthly_report" in b:
            state["alerts"]["monthly_report"] = bool(b["monthly_report"])
        if "device" in b:
            name = " ".join(str(b["device"]).split())[:40]
            if not name:
                return jsonify(ok=False, error="device name is empty"), 400
            if name != state["device"]:
                log(f"connected device: {state['device']} -> {name}")
            state["device"] = name
        save_state()
    return jsonify(ok=True)


@app.post("/api/relabel")
def api_relabel():
    """Change the device label of every reading between two local times.
    Body: {"from": "YYYY-MM-DDTHH:MM", "to": "YYYY-MM-DDTHH:MM", "device": name}"""
    b = request.json
    name = " ".join(str(b.get("device", "")).split())[:40]
    try:
        t0 = datetime.strptime(b["from"], "%Y-%m-%dT%H:%M")
        t1 = datetime.strptime(b["to"], "%Y-%m-%dT%H:%M")
    except (KeyError, ValueError):
        return jsonify(ok=False, error="from/to must be YYYY-MM-DDTHH:MM"), 400
    if not name or t1 <= t0:
        return jsonify(ok=False, error="need a device name and to > from"), 400
    conn = db()
    cur = conn.execute("UPDATE readings SET device = ? WHERE ts >= ? AND ts < ?",
                       (name, int(t0.timestamp()), int(t1.timestamp())))
    conn.commit()
    conn.close()
    log(f"relabeled {cur.rowcount} readings {t0:%d.%m. %H:%M}–{t1:%d.%m. %H:%M} as {name}")
    return jsonify(ok=True, changed=cur.rowcount)


@app.post("/api/test-report")
def api_test_report():
    """Send last month's report now (or this month so far if last month is empty)."""
    n = datetime.now()
    price, poll = state["price_per_kwh"], cfg.get("poll_seconds", 10)
    pl = datetime(n.year, n.month, 1) - timedelta(days=1)
    text = month_report(pl.year, pl.month, price, poll) or month_report(n.year, n.month, price, poll)
    if not text:
        return jsonify(ok=False, error="no data yet")
    err = notify(text)
    return jsonify(ok=err is None, error=err, text=text)


@app.post("/api/reset")
def api_reset():
    """Delete every stored reading. Settings, schedules and the price stay."""
    if request.json.get("confirm") != "RESET":
        return jsonify(ok=False, error="send {\"confirm\": \"RESET\"}"), 400
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.execute("DELETE FROM readings")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"energy log reset: {n} readings deleted")
    return jsonify(ok=True, deleted=n)


@app.post("/api/test-notify")
def api_test_notify():
    err = notify("Test notification from the smart plug page")
    return jsonify(ok=err is None, error=err)


@app.get("/api/info")
def api_info():
    return jsonify(
        name=cfg.get("name", "Smart Plug"), product=cfg.get("product"),
        device_id=cfg.get("device_id", "demo"), mac=cfg.get("mac"),
        ip=cfg.get("ip", "demo"), protocol=cfg.get("version"),
        model=cfg.get("model"), poll_seconds=cfg.get("poll_seconds", 10))


def _csv_response(rows, header, filename):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/export/readings.csv")
def export_readings():
    """Every stored reading (one row per poll). ?device=NAME limits it to one label."""
    w, p = dev_where(request.args.get("device", ""))
    conn = db()
    rows = conn.execute(f"""SELECT datetime(ts,'unixepoch','localtime'), device, on_state,
                                   power_w, voltage_v, current_a, ROUND(wh, 5)
                            FROM readings WHERE 1=1{w} ORDER BY ts""", p).fetchall()
    conn.close()
    return _csv_response(rows, ["time", "device", "on", "power_w", "voltage_v", "current_a",
                                "energy_wh"],
                         f"plug-readings-{datetime.now():%Y-%m-%d}.csv")


@app.get("/api/chart")
def api_chart():
    rng = request.args.get("range", "day")
    if rng not in ("day", "week", "month"):
        return jsonify(error="range must be day, week or month"), 400
    try:
        return jsonify(chart_data(rng, request.args.get("date"), state["price_per_kwh"],
                                  request.args.get("device", "")))
    except ValueError:
        return jsonify(error="date must be YYYY-MM-DD"), 400


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


# --------------------------------------------------------------------------

if __name__ == "__main__":
    if DEMO:
        plug = DemoPlug()
        cfg = {"poll_seconds": 2, "port": 8081}
        DB_PATH = os.path.join(DATA, "demo_energy.db")
        STATE_PATH = os.path.join(DATA, "demo_state.json")
        log("DEMO MODE: simulated plug (separate demo_* data files)")
    else:
        if not os.path.exists(CONFIG_PATH):
            sys.exit("config.json missing. Copy config.example.json and fill it in.")
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        plug = TuyaPlug(cfg)
    load_state(cfg)
    threading.Thread(target=loop, args=(cfg.get("poll_seconds", 10),), daemon=True).start()
    port = cfg.get("port", 8080)
    log(f"listening on port {port}, plug at {cfg.get('ip', 'demo')}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
