#!/usr/bin/env python3
# Nightly consistent copies of the SQLite databases of the Oracle apps, made before Backrest (03:15).
# Uses the SQLite online backup API: no container stop. One copy per DB, overwritten each night,
# written next to the live file as <name>.snapshot. To restore: stop the app, copy <name>.snapshot
# over <name>, delete <name>-wal and <name>-shm, start the app.
import os
import sqlite3
import sys

DBS = [
    "/opt/docker/pocket-id/data/pocket-id.db",
    "/opt/docker/pangolin/config/db/db.sqlite",
    "/opt/docker/uptime-kuma/data/kuma.db",
    "/opt/docker/navidrome/navidrome-data/navidrome.db",
    "/opt/docker/crowdsec/data/crowdsec.db",
    "/opt/docker/crowdsec/web-ui-data/crowdsec.db",
]

failed = 0
for db in DBS:
    if not os.path.exists(db):
        print(f"skip (missing): {db}")
        continue
    snap, tmp = db + ".snapshot", db + ".snapshot.tmp"
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        ok = dst.execute("pragma integrity_check").fetchone()[0]
        dst.close()
        src.close()
        if ok != "ok":
            raise RuntimeError(f"integrity_check: {ok}")
        st = os.stat(db)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, 0o600)
        os.replace(tmp, snap)
        print(f"ok  {os.path.getsize(snap):>9} B  {snap}")
    except Exception as e:
        failed += 1
        print(f"ERR {db}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
sys.exit(1 if failed else 0)
