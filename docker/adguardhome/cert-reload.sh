#!/bin/sh
# Restart AdGuard when the synced NPM cert differs from the one it is serving
f=$(openssl x509 -in /opt/docker/adguardhome/certs/fullchain.pem -noout -fingerprint -sha256 2>/dev/null)
s=$(echo | openssl s_client -connect 127.0.0.1:853 -servername yourdomain.dedyn.io 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null)
if [ -n "$f" ] && [ "$f" != "$s" ]; then
  docker restart adguardHome >/dev/null && echo "[$(date)] cert changed, AdGuard restarted"
fi
