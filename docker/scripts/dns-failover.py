#!/usr/bin/env python3
# Phone DNS failover (every minute, home server).
# Checks the Oracle VM's DNS-over-TLS the way the phone uses it (its ClientID in the TLS name)
# and reports each check to the Uptime Kuma push monitor "Oracle DNS-over-TLS".
# 3 failed checks in a row -> the phone's DoT name points to home (its A record is deleted,
# so the wildcard CNAME to the DDNS name applies).
# Back to the VM only by hand for now (AUTO_BACK_TO_VM = False): dns-failover.py --to-vm
# It never switches when this server has no internet or the home AdGuard is down.
# deSEC token in .desec-token, push URL in .kuma-push-urls (both next to this script, not in git).
import base64, json, os, socket, ssl, struct, sys, time, urllib.parse, urllib.request
from datetime import datetime

DOMAIN = "yourdomain.dedyn.io"
NAME = "<CLIENTID>"                             # the phone's Private DNS name (ClientID)
VM_IP = "<ORACLE-PUBLIC-IP>"
HOME_DNS = "192.168.0.214"
MONITOR = "Oracle DNS-over-TLS"
FAILS_TO_HOME, OKS_TO_VM = 3, 10
AUTO_BACK_TO_VM = False  # switch back with --to-vm until the automatic way is tested

os.chdir(os.path.dirname(os.path.abspath(__file__)))
STATE = ".dns-failover.state"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def query(qname="example.com"):
    return struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"".join(
        bytes([len(p)]) + p.encode() for p in qname.split(".")) + b"\x00" + struct.pack(">HH", 1, 1)


def answered(resp):  # NOERROR with at least one answer
    _, flags, _, ancount = struct.unpack(">HHHH", resp[:8])
    return flags & 0xF == 0 and ancount > 0


def check_vm():
    """DNS-over-TLS query to the VM with the phone's ClientID. Returns (ok, ms, detail)."""
    start = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((VM_IP, 853), timeout=5) as raw, \
                ctx.wrap_socket(raw, server_hostname=f"{NAME}.{DOMAIN}") as s:
            q = query()
            s.sendall(struct.pack(">H", len(q)) + q)
            n = struct.unpack(">H", s.recv(2))[0]
            resp = b""
            while len(resp) < n:
                chunk = s.recv(n - len(resp))
                if not chunk:
                    break
                resp += chunk
        ms = int((time.monotonic() - start) * 1000)
        return (answered(resp), ms, "OK" if answered(resp) else "no answer")
    except Exception as e:
        return (False, 0, type(e).__name__)


def home_ok():
    """This server has internet (TCP to 1.1.1.1:853) and the home AdGuard answers."""
    try:
        socket.create_connection(("1.1.1.1", 853), timeout=5).close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.settimeout(3)
            u.sendto(query(), (HOME_DNS, 53))
            return answered(u.recv(512))
    except Exception:
        return False


def desec(method, body=None):
    token = open(".desec-token").read().strip()
    req = urllib.request.Request(
        f"https://desec.io/api/v1/domains/{DOMAIN}/rrsets/", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def current_mode():
    for rr in desec("GET"):
        if rr["subname"] == NAME and rr["type"] == "A":
            return "vm"
    return "home"  # no own record: the wildcard CNAME sends it home


def switch(to, name=NAME):
    if to == "vm":
        # deSEC's DynDNS interface sets the 60 s TTL (normal records need at least 900 s)
        token = open(".desec-token").read().strip()
        q = urllib.parse.urlencode({"hostname": f"{name}.{DOMAIN}", "myipv4": VM_IP, "myipv6": "preserve"})
        req = urllib.request.Request(f"https://update.dedyn.io/?{q}", headers={
            "Authorization": "Basic " + base64.b64encode(f"{DOMAIN}:{token}".encode()).decode()})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.read().decode().strip() != "good":
                raise RuntimeError("deSEC DynDNS did not answer good")
    else:
        # delete the record: the wildcard CNAME then sends the name home (follows the DDNS IP)
        desec("PATCH", [{"subname": name, "type": "A", "ttl": 900, "records": []}])


def push(status, msg, ms):
    try:
        url = next(l.split("=", 1)[1].strip() for l in open(".kuma-push-urls") if l.startswith(MONITOR + "="))
        q = urllib.parse.urlencode({"status": status, "msg": msg, "ping": ms or ""})
        urllib.request.urlopen(f"{url}?{q}", timeout=10).read()
    except StopIteration:
        pass
    except Exception as e:
        log(f"Kuma push failed: {type(e).__name__}")


if sys.argv[1:] == ["--to-vm"]:  # switch the phone back to the VM by hand (only if the VM answers)
    ok, ms, detail = check_vm()
    if not ok:
        sys.exit(f"VM DoT not OK ({detail}), not switching")
    switch("vm")
    json.dump({"mode": "vm", "fails": 0, "oks": 0}, open(STATE, "w"))
    log("switched by hand: phone DNS -> VM")
    sys.exit(0)

if sys.argv[1:] == ["--status"]:  # report only, change nothing
    ok, ms, detail = check_vm()
    print(f"VM DoT: {detail} ({ms} ms) | home internet + AdGuard: {home_ok()} | phone DNS now on: {current_mode()}")
    sys.exit(0)

try:
    st = json.load(open(STATE))
except Exception:
    st = {"mode": current_mode(), "fails": 0, "oks": 0}

ok, ms, detail = check_vm()
st["fails"], st["oks"] = (0, st["oks"] + 1) if ok else (st["fails"] + 1, 0)

if st["mode"] == "vm" and st["fails"] >= FAILS_TO_HOME:
    if home_ok():
        try:
            switch("home"); st["mode"] = "home"; log(f"VM DoT failed {st['fails']}x ({detail}): phone DNS -> HOME")
        except Exception as e:
            log(f"switch to home failed: {e}")
    elif st["fails"] == FAILS_TO_HOME:
        log("VM DoT failing, but home has no internet or AdGuard is down: not switching")
elif st["mode"] == "home" and st["oks"] >= OKS_TO_VM and not AUTO_BACK_TO_VM:
    if st["oks"] == OKS_TO_VM:
        log(f"VM DoT OK {OKS_TO_VM}x: staying on HOME, switch back with --to-vm")
elif st["mode"] == "home" and st["oks"] >= OKS_TO_VM:
    try:
        switch("vm"); st["mode"] = "vm"; log(f"VM DoT OK {st['oks']}x: phone DNS -> VM")
    except Exception as e:
        log(f"switch to VM failed: {e}")

where = "phone DNS on VM" if st["mode"] == "vm" else "phone DNS on HOME"
push("up" if ok else "down", f"{detail} ({where})", ms)
json.dump(st, open(STATE, "w"))
