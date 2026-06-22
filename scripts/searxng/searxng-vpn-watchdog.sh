#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# SearXNG VPN watchdog: re-establish the ProtonVPN tunnel when it actually drops,
# instead of waiting up to an hour for the next cron rotation.
#
# Liveness is STRUCTURAL first (openvpn process alive + tun0 + table-100 default
# via tun0) so a healthy tunnel is NEVER torn down over a transient probe blip.
# A DNS-free egress probe (curl to 1.1.1.1 by IP) catches a "zombie" tunnel, but
# only triggers a re-roll after it fails TWO consecutive ticks. Restores public
# DNS during an outage so the box keeps resolving, and backs off when every
# server keeps failing (likely creds/egress) instead of thrashing.
set -uo pipefail

LOG="/var/log/vpn-watchdog.log"
STATE="/run/searxng-vpn-watchdog.state"
LOCK="/run/lock/searxng-vpn-watchdog.lock"
TABLE="100"
MAX_FAILS="${WATCHDOG_MAX_FAILS:-3}"
COOLDOWN="${WATCHDOG_COOLDOWN:-1800}"   # back-off seconds after MAX_FAILS rotate failures

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

# Don't stack ticks: a rotation can run for a while; the timer must skip while a
# previous watchdog run is still working.
exec 8>"$LOCK"
flock -n 8 || exit 0

fails=0; cooldown_until=0; last="unknown"; softfail=0
# shellcheck disable=SC1090
[ -f "$STATE" ] && . "$STATE"
now=$(date +%s)
save_state() { printf 'fails=%s\ncooldown_until=%s\nlast=%s\nsoftfail=%s\n' "$fails" "$cooldown_until" "$last" "$softfail" > "$STATE"; }

# Structural liveness — robust, no external dependency.
structural_up() {
    pgrep -f '/usr/sbin/openvpn' >/dev/null 2>&1 || return 1
    ip link show tun0 >/dev/null 2>&1 || return 1
    ip route show table "$TABLE" default 2>/dev/null | grep -q 'dev tun0' || return 1
}
# DNS-free egress through the tunnel (does searxng actually reach the internet?).
egress_ok() {
    runuser -u searxng -- curl -4 -sS --connect-timeout 8 -o /dev/null https://1.1.1.1/ >/dev/null 2>&1
}
restart_searxng() { systemctl restart --no-block searxng || true; }

if structural_up; then
    if egress_ok; then
        if [ "$last" != "up" ]; then
            log "tunnel healthy (egress ok) — restarting searxng to clear engine suspensions"
            restart_searxng
        fi
        fails=0; cooldown_until=0; softfail=0; last="up"
        save_state
        exit 0
    fi
    # Structurally up but egress failed — tolerate one blip, only re-roll on a
    # second consecutive failure (real zombie tunnel).
    softfail=$((softfail + 1))
    if [ "$softfail" -lt 2 ]; then
        log "tun0 up but egress probe failed (softfail=$softfail) — tolerating, recheck next tick"
        last="up"
        save_state
        exit 0
    fi
    log "egress failed $softfail consecutive ticks with tun0 up — zombie tunnel, re-rotating"
fi

# --- tunnel is down (or confirmed zombie): recover ---
last="down"; softfail=0
# Keep box DNS working during the outage even if openvpn was killed hard (so its
# down-script never restored a public resolver). searxng stays fail-closed.
printf 'nameserver 1.1.1.1\nnameserver 192.168.1.1\n' > /etc/resolv.conf

if [ "$fails" -ge "$MAX_FAILS" ] && [ "$now" -lt "$cooldown_until" ]; then
    log "in cooldown ($((cooldown_until - now))s left, $fails consecutive failures) — skipping re-roll"
    save_state
    exit 0
fi

log "VPN down — running rotate-ovpn.sh (consecutive failures so far: $fails)"
rotate-ovpn.sh >> "$LOG" 2>&1 || true

if structural_up && egress_ok; then
    log "recovered — restarting searxng"
    restart_searxng
    fails=0; cooldown_until=0; softfail=0; last="up"
    save_state
    exit 0
fi

fails=$((fails + 1))
if [ "$fails" -ge "$MAX_FAILS" ]; then
    cooldown_until=$((now + COOLDOWN))
    log "all configs still failing after $fails consecutive attempts — likely creds/egress. Backing off ${COOLDOWN}s."
fi
save_state
exit 0
