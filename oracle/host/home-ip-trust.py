#!/usr/bin/env python3
# Home's internet IP counts like Tailscale on the gateway: admin pages open from home, and Pangolin
# lets home through without its login (same as Tailscale). yourdomain.dedyn.io (no subdomain) is home's
# current IP, kept up to date by home's deSEC updater. Root cron, every 5 minutes: when that IP changes,
# swap the old /32 for the new one in Traefik's rules (file reloads by itself), in Pangolin's
# resource rules and in CrowdSec's "homelab" allowlist (so failed logins from home never ban the house).
# Prints only when something changed.
import os
import re
import sqlite3
import subprocess
import sys

TRAEFIK = "/opt/docker/pangolin/config/traefik/dynamic_config.yml"
PANGOLIN_DB = "/opt/docker/pangolin/config/db/db.sqlite"
IP = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"

out = subprocess.run(["dig", "+short", "A", "yourdomain.dedyn.io", "@ns1.desec.io"],
                     capture_output=True, text=True, timeout=20).stdout.split()
new = next((x for x in out if re.fullmatch(IP, x)), None)
s = open(TRAEFIK).read()
m = re.search(rf"^# home-ip ({IP})", s, re.M)
if not new or not m:
    sys.exit(f"skip: home IP from DNS={new!r}, marker found={bool(m)}")
old = m.group(1)
if new == old:
    sys.exit(0)

st = os.stat(TRAEFIK)
tmp = TRAEFIK + ".tmp"
with open(tmp, "w") as f:
    f.write(s.replace(f"{old}/32", f"{new}/32").replace(f"# home-ip {old}", f"# home-ip {new}"))
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, st.st_mode & 0o777)
os.replace(tmp, TRAEFIK)

db = sqlite3.connect(PANGOLIN_DB, timeout=30)
n = db.execute("update resourcePolicyRules set value = ? where match = 'CIDR' and value = ?",
               (f"{new}/32", f"{old}/32")).rowcount
db.commit()
db.close()

# new one first, then the old one: no moment without the allowlist entry
cscli = ["docker", "exec", "crowdsec", "cscli", "allowlists"]
subprocess.run(cscli + ["add", "homelab", new, "-d", "home internet IP"], check=True, capture_output=True, timeout=60)
subprocess.run(cscli + ["remove", "homelab", old], check=True, capture_output=True, timeout=60)
print(f"home IP {old} -> {new}: Traefik updated, {n} Pangolin rules updated, CrowdSec allowlist updated")
