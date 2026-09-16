#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# Keep SearXNG fail-closed when the VPN goes down.
set -euo pipefail
DEV="${dev:-tun0}"
/usr/local/sbin/searxng-vpn-killswitch.sh down 2>/dev/null || true
iptables -t nat -D POSTROUTING -o "$DEV" -j MASQUERADE 2>/dev/null || true
if [ "${script_type:-}" != "down" ]; then
    PID=$(cat /run/openvpn-proton.pid 2>/dev/null || true)
    if [[ "$PID" =~ ^[0-9]+$ ]] && [ "$(readlink "/proc/$PID/exe" 2>/dev/null || true)" = /usr/sbin/openvpn ]; then
        kill -TERM "$PID" 2>/dev/null || true
    fi
fi
exit 0
