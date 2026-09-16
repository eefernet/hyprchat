#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# Rotate OpenVPN through ProtonVPN configs for SearXNG IP rotation.
# Fail-closed: keep SearXNG UID blocked unless traffic exits via the VPN dev.
#
# Hardened (2026-06-22):
#  - shared flock so the hourly cron and the watchdog never fight over openvpn
#  - tries up to MAX_TRIES distinct-IP servers per run instead of giving up on
#    the first dead exit (the bug that blackholed search for a full hour)
#  - success is the full VPN_UP gate: tun0 + table-100 default via tun0 +
#    a real searxng-uid egress probe (not tun0 alone)
set -euo pipefail

# Every entry point uses the same systemd owner. In particular a watchdog
# oneshot must not own (and kill on exit) the long-lived OpenVPN process.
if [ "${SEARXNG_ROTATION_OWNER:-}" != "systemd" ]; then
    exec systemctl restart protonvpn-rotate.service
fi

CONF_DIR="/etc/openvpn/proton-ovpn"
AUTH_FILE="$CONF_DIR/auth.txt"
LOG_FILE="/var/log/vpn-rotation.log"
USAGE_DIR="/tmp/ovpn-usage"
LOCK_FILE="/run/lock/searxng-ovpn.lock"
KILLSWITCH="/usr/local/sbin/searxng-vpn-killswitch.sh"
MARK="0x1"
TABLE="100"
MAX_TRIES="${OVPN_MAX_TRIES:-8}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" | tee -a "$LOG_FILE"; }

# --- shared lock: cron + watchdog must never launch openvpn concurrently ---
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "rotation already running (lock held) — skipping"
    exit 75
fi

"$KILLSWITCH" apply

TODAY=$(date +%Y-%m-%d)
mkdir -p "$USAGE_DIR"
find "$USAGE_DIR" -name "*.count" -not -name "${TODAY}_*" -delete 2>/dev/null || true

mapfile -t CONFIGS < <(ls "$CONF_DIR"/*.ovpn 2>/dev/null | sort)
if [ ${#CONFIGS[@]} -eq 0 ]; then
    log "No OpenVPN configs found in $CONF_DIR"
    exit 1
fi

get_ip() { grep -m1 '^remote ' "$1" | awk '{print $2}'; }
get_usage() {
    local safe_ip file
    safe_ip=$(echo "$1" | tr '.' '_')
    file="$USAGE_DIR/${TODAY}_${safe_ip}.count"
    [ -f "$file" ] && cat "$file" || echo 0
}
record_usage() {
    local safe_ip file cur
    safe_ip=$(echo "$1" | tr '.' '_')
    file="$USAGE_DIR/${TODAY}_${safe_ip}.count"
    cur=$(get_usage "$1")
    echo $((cur + 1)) > "$file"
}

# Build an ordered candidate list of DISTINCT remote IPs:
#   pass 1 = fresh today (usage < 2), pass 2 = everything else.
declare -A SEEN
CANDIDATES=()
while IFS= read -r cfg; do
    ip=$(get_ip "$cfg"); [ -z "$ip" ] && continue
    [ -n "${SEEN[$ip]:-}" ] && continue
    if [ "$(get_usage "$ip")" -lt 2 ]; then SEEN[$ip]=1; CANDIDATES+=("$cfg"); fi
done < <(printf '%s\n' "${CONFIGS[@]}" | shuf)
while IFS= read -r cfg; do
    ip=$(get_ip "$cfg"); [ -z "$ip" ] && continue
    [ -n "${SEEN[$ip]:-}" ] && continue
    SEEN[$ip]=1; CANDIDATES+=("$cfg")
done < <(printf '%s\n' "${CONFIGS[@]}" | shuf)

# --- VPN_UP gate ----------------------------------------------------------
# Returns 0 only when ALL hold: tun0 exists, table-100 default routes via tun0,
# and searxng-uid traffic actually reaches the internet THROUGH the tunnel.
# The egress probe is DNS-free (curl to 1.1.1.1 by IP) so it doesn't false-fail
# on Proton-DNS timing or ifconfig.me rate-limiting — that flakiness used to make
# this gate reject perfectly good tunnels and cycle servers. Echoes the exit IP
# (best-effort, for logging only).
vpn_up_ip() {
    ip link show tun0 >/dev/null 2>&1 || return 1
    ip route show table "$TABLE" default 2>/dev/null | grep -q 'dev tun0' || return 1
    runuser -u searxng -- curl -4 -sS --connect-timeout 8 --max-time 12 -o /dev/null https://1.1.1.1/ >/dev/null 2>&1 || return 1
    local exitip=""
    exitip=$(runuser -u searxng -- curl -4 -sS --connect-timeout 3 --max-time 5 https://ifconfig.me/ip 2>/dev/null || true)
    echo "${exitip:-unknown}"
}

stop_openvpn() {
    local pid
    pid=$(cat /run/openvpn-proton.pid 2>/dev/null || true)
    if [[ "$pid" =~ ^[0-9]+$ ]] && [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" = /usr/sbin/openvpn ]; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "OpenVPN $pid did not stop; refusing to launch a second process"
            return 1
        fi
    fi
}

launch_openvpn() {
    local cfg="$1"
    stop_openvpn
    "$KILLSWITCH" apply
    : > /var/log/openvpn.log
    /usr/sbin/openvpn \
        --config "$cfg" \
        --auth-user-pass "$AUTH_FILE" \
        --auth-nocache \
        --daemon \
        --log /var/log/openvpn.log \
        --writepid /run/openvpn-proton.pid \
        --script-security 2 \
        --route-noexec \
        --connect-timeout 10 --hand-window 16 \
        --up /usr/local/bin/ovpn-up.sh \
        --down /usr/local/bin/ovpn-down.sh 8>&- 9>&-
}

CHOSEN_IP=""
EXIT_IP=""
TRIES=0
for cfg in "${CANDIDATES[@]}"; do
    [ "$TRIES" -ge "$MAX_TRIES" ] && break
    TRIES=$((TRIES + 1))
    ENTRY_IP=$(get_ip "$cfg")
    log "attempt $TRIES/$MAX_TRIES: $(basename "$cfg") (entry IP: $ENTRY_IP)"
    launch_openvpn "$cfg"

    # wait for tun0 + policy route, bail early on auth failure
    UP=""
    for _ in $(seq 1 25); do
        if ip link show tun0 >/dev/null 2>&1 && ip route show table "$TABLE" default 2>/dev/null | grep -q 'dev tun0'; then UP=1; break; fi
        if grep -qi "AUTH_FAILED" /var/log/openvpn.log 2>/dev/null; then
            log "AUTH_FAILED on $(basename "$cfg") — credential problem, aborting run"
            stop_openvpn
            exit 1
        fi
        sleep 1
    done
    if [ -z "$UP" ]; then
        log "  no tunnel from $(basename "$cfg") (TLS/connect timeout), trying next"
        continue
    fi

    "$KILLSWITCH" apply
    sleep 2

    # full egress gate
    for _ in 1 2 3; do
        EXIT_IP=$(vpn_up_ip || true)
        [ -n "$EXIT_IP" ] && break
        sleep 2
    done
    if [ -n "$EXIT_IP" ]; then
        CHOSEN_IP="$ENTRY_IP"
        record_usage "$ENTRY_IP"
        break
    fi
    log "  tun0 up but searxng egress failed on $(basename "$cfg"), trying next"
done

if [ -z "$EXIT_IP" ]; then
    log "FAILED — no working server in $TRIES tries (likely all-dead exits or upstream UDP block)"
    tail -20 /var/log/openvpn.log | tee -a "$LOG_FILE" || true
    stop_openvpn
    "$KILLSWITCH" apply
    exit 1
fi

"$KILLSWITCH" apply
MY_IP=$(curl -4 -sS --connect-timeout 3 --max-time 5 https://ifconfig.me/ip 2>/dev/null || echo "unknown")
log "VPN up | searxng=$EXIT_IP | root=$MY_IP | entry=$CHOSEN_IP | after $TRIES tries"
