#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# Called by OpenVPN when the tunnel comes up.
# Applies fail-closed UID routing/firewall and routes only SearXNG via Proton.
set -euo pipefail
/usr/local/sbin/searxng-vpn-killswitch.sh apply
exit 0
