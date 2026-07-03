#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# Keep SearXNG fail-closed when the VPN goes down.
set -euo pipefail
DEV="${dev:-tun0}"
/usr/local/sbin/searxng-vpn-killswitch.sh down 2>/dev/null || true
iptables -t nat -D POSTROUTING -o "$DEV" -j MASQUERADE 2>/dev/null || true
if [ "${script_type:-}" != "down" ]; then
    pkill -f '/usr/sbin/openvpn' 2>/dev/null || true
fi
exit 0
