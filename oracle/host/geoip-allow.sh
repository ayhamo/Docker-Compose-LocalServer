#!/bin/sh
# GeoIP allow-list for the public web ports: only these countries may reach 80/443 (NPM).
# DoT 853, Tailscale and SSH (Tailscale only) are not affected. Country lists from ipdeny.com,
# cached in /etc/geoip-allow/ so a failed download keeps the last good list.
#   geoip-allow.sh update   download fresh lists, then apply (weekly timer)
#   geoip-allow.sh apply    load the cached lists and make sure the rule is in place (at boot)
#   geoip-allow.sh off      remove the rule (everyone may reach 80/443 again)
#   geoip-allow.sh status
set -eu
COUNTRIES="de kw"
DIR=/etc/geoip-allow
SET=geo-allow
IF=enp0s6
RULE="DOCKER-USER -i $IF -p tcp -m multiport --dports 80,443 -m set ! --match-set $SET src -j DROP"

case "${1:-apply}" in
update)
  for c in $COUNTRIES; do
    if curl -fsS -m 60 -o "$DIR/$c.zone.new" "https://www.ipdeny.com/ipblocks/data/aggregated/$c-aggregated.zone" \
        && [ "$(grep -c / "$DIR/$c.zone.new")" -gt 10 ]; then
      mv "$DIR/$c.zone.new" "$DIR/$c.zone"
    else
      rm -f "$DIR/$c.zone.new"; echo "[$(date)] $c: download failed, keeping the cached list"
    fi
  done
  exec "$0" apply
  ;;
apply)
  n=$(cat "$DIR"/*.zone 2>/dev/null | grep -c / || true)
  # Fail-safe: never block with an empty or broken list
  [ "$n" -gt 1000 ] || { echo "[$(date)] only $n networks cached, NOT applying"; exit 1; }
  ipset create "$SET" hash:net maxelem 262144 -exist
  ipset create "$SET-new" hash:net maxelem 262144 -exist
  ipset flush "$SET-new"
  cat "$DIR"/*.zone | grep / | sed "s/^/add $SET-new /" | ipset restore -exist
  ipset swap "$SET-new" "$SET"
  ipset destroy "$SET-new"
  iptables -C $RULE 2>/dev/null || iptables -I $RULE
  echo "[$(date)] applied: $n networks ($COUNTRIES) may reach 80/443"
  ;;
off)
  while iptables -C $RULE 2>/dev/null; do iptables -D $RULE; done
  echo "[$(date)] rule removed: 80/443 open to all countries"
  ;;
status)
  iptables -C $RULE 2>/dev/null && echo "rule: ACTIVE" || echo "rule: off"
  ipset list "$SET" -terse 2>/dev/null | grep -E "Number of entries" || echo "set: missing"
  ;;
esac
