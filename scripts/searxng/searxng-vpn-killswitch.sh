#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# Fail-closed routing for the SearXNG service user.
# IPv4 uses fwmark policy routing: VPN default when up, unreachable default when down.
# IPv6 is blocked for SearXNG because the current Proton OpenVPN setup is IPv4-only.
set -euo pipefail

ACTION="${1:-apply}"
SEARXNG_USER="${SEARXNG_USER:-searxng}"
UID_NUM="$(id -u "$SEARXNG_USER")"
TABLE="${SEARXNG_VPN_TABLE:-100}"
MARK="${SEARXNG_VPN_MARK:-0x1}"
CHAIN6="SEARXNG_VPN6_OUT"
LAN_CIDR="${SEARXNG_LAN_CIDR:-192.168.1.0/24}"
LAN_DEV="${SEARXNG_LAN_DEV:-eth0}"
LAN6_CIDR="${SEARXNG_LAN6_CIDR:-fd73:a49c:ce1b:4f7a::/64}"
VPN_DEV="${SEARXNG_VPN_DEV:-${dev:-tun0}}"
VPN_GW="${SEARXNG_VPN_GW:-${route_vpn_gateway:-}}"

if [ -z "$VPN_GW" ]; then
    VPN_GW="$(ip route show table "$TABLE" default 2>/dev/null | awk '/dev/ {print $3; exit}' || true)"
fi
if [ -z "$VPN_GW" ] && ip link show "$VPN_DEV" >/dev/null 2>&1; then
    VPN_GW="$(ip -4 route show dev "$VPN_DEV" scope link 2>/dev/null | awk 'NR==1 {sub(/\/.*/,"",$1); split($1,a,"."); print a[1]"."a[2]".0.1"; exit}' || true)"
fi
DNS_SERVER="${SEARXNG_VPN_DNS:-${VPN_GW:-}}"
LAN_SRC="$(ip -4 addr show dev "$LAN_DEV" 2>/dev/null | awk '/inet / {print $2; exit}' | cut -d/ -f1 || true)"

ensure_rule() {
    local table_flag=()
    if [ "$1" != "filter" ]; then
        table_flag=(-t "$1")
    fi
    shift
    if ! iptables "${table_flag[@]}" -C "$@" 2>/dev/null; then
        iptables "${table_flag[@]}" -A "$@"
    fi
}

cleanup_old_ipv4_filter_chain() {
    while iptables -D OUTPUT -m owner --uid-owner "$UID_NUM" -j SEARXNG_VPN_OUT 2>/dev/null; do :; done
    iptables -F SEARXNG_VPN_OUT 2>/dev/null || true
    iptables -X SEARXNG_VPN_OUT 2>/dev/null || true
}

apply_ipv4() {
    cleanup_old_ipv4_filter_chain
    ensure_rule mangle OUTPUT -m owner --uid-owner "$UID_NUM" -j MARK --set-mark "$MARK"

    ip rule del fwmark "$MARK" table "$TABLE" 2>/dev/null || true
    ip rule add fwmark "$MARK" table "$TABLE" priority 100

    ip route flush table "$TABLE" 2>/dev/null || true
    if [ -n "$LAN_SRC" ]; then
        ip route replace "$LAN_CIDR" dev "$LAN_DEV" src "$LAN_SRC" table "$TABLE"
    else
        ip route replace "$LAN_CIDR" dev "$LAN_DEV" table "$TABLE"
    fi

    if [ -n "$VPN_GW" ] && ip link show "$VPN_DEV" >/dev/null 2>&1; then
        ip route replace default via "$VPN_GW" dev "$VPN_DEV" table "$TABLE"
        ensure_rule nat POSTROUTING -o "$VPN_DEV" -j MASQUERADE
    else
        ip route replace unreachable default table "$TABLE" metric 4278198272
    fi
}

apply_ipv6() {
    command -v ip6tables >/dev/null 2>&1 || return 0
    ip6tables -N "$CHAIN6" 2>/dev/null || true
    ip6tables -F "$CHAIN6"
    ip6tables -A "$CHAIN6" -o lo -j ACCEPT
    ip6tables -A "$CHAIN6" -d "$LAN6_CIDR" -j ACCEPT 2>/dev/null || true
    ip6tables -A "$CHAIN6" -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null || ip6tables -A "$CHAIN6" -j REJECT
    while ip6tables -D OUTPUT -m owner --uid-owner "$UID_NUM" -j "$CHAIN6" 2>/dev/null; do :; done
    ip6tables -I OUTPUT 1 -m owner --uid-owner "$UID_NUM" -j "$CHAIN6"
}

set_vpn_dns_if_ready() {
    if [ -n "$DNS_SERVER" ] && ip link show "$VPN_DEV" >/dev/null 2>&1; then
        printf 'nameserver %s\n' "$DNS_SERVER" > /etc/resolv.conf
    fi
}

case "$ACTION" in
    apply)
        apply_ipv4
        apply_ipv6
        set_vpn_dns_if_ready
        ;;
    down)
        cleanup_old_ipv4_filter_chain
        ensure_rule mangle OUTPUT -m owner --uid-owner "$UID_NUM" -j MARK --set-mark "$MARK"
        ip rule del fwmark "$MARK" table "$TABLE" 2>/dev/null || true
        ip rule add fwmark "$MARK" table "$TABLE" priority 100
        ip route flush table "$TABLE" 2>/dev/null || true
        [ -n "$LAN_SRC" ] && ip route replace "$LAN_CIDR" dev "$LAN_DEV" src "$LAN_SRC" table "$TABLE" || ip route replace "$LAN_CIDR" dev "$LAN_DEV" table "$TABLE"
        ip route replace unreachable default table "$TABLE" metric 4278198272
        apply_ipv6
        # HyprChat: restore a working public resolver so box DNS survives tunnel-down
        printf 'nameserver 1.1.1.1\nnameserver 192.168.1.1\n' > /etc/resolv.conf
        ;;
    status)
        echo "uid=$UID_NUM table=$TABLE mark=$MARK vpn_dev=$VPN_DEV vpn_gw=${VPN_GW:-none} dns=${DNS_SERVER:-none}"
        ip rule show | grep -E "fwmark $MARK|lookup $TABLE" || true
        ip route show table "$TABLE" || true
        iptables-save | grep -E "SEARXNG_VPN_OUT|uid-owner $UID_NUM|--set-xmark $MARK|--set-mark $MARK|POSTROUTING.*$VPN_DEV" || true
        ip6tables-save 2>/dev/null | grep -E "SEARXNG_VPN6_OUT|uid-owner $UID_NUM" || true
        ;;
    *)
        echo "usage: $0 [apply|down|status]" >&2
        exit 2
        ;;
esac
