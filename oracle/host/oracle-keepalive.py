#!/usr/bin/env python3
# Oracle Always Free idle-reclaim guard (temporary, until real services keep the VM busy).
# Oracle reclaims an idle A1 VM only when CPU (95th pct), network AND memory all stay < 20% for 7 days.
# Memory: hold ~2 GB of touched RAM (no CPU cost). CPU: 12 min per hour at ~50% of one core (nice 19).
import time

HOLD_MB = 2048
ballast = bytearray(b"\x01") * (HOLD_MB * 1024 * 1024)   # written, so it is really resident

while True:
    end = time.time() + 12 * 60
    while time.time() < end:                 # burst: 50 ms busy, 50 ms idle
        t = time.time() + 0.05
        while time.time() < t:
            pass
        time.sleep(0.05)
    time.sleep(48 * 60)                      # rest of the hour idle
