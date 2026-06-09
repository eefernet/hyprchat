#!/usr/bin/env bash
# Harden an existing SearXNG host for HyprChat.
#
# Required env:
#   HYPRCHAT_IP=192.168.1.120
#
# Optional env:
#   DEV_IP=192.168.1.x          # may access SearXNG :8888, not proxy :8899
#   PROTON_OPENVPN=auto|true|false
#
# Proton assets, when used, must already exist on this host:
#   /etc/openvpn/proton-ovpn/*.ovpn
#   /etc/openvpn/proton-ovpn/auth.txt

set -u

HYPRCHAT_IP="${HYPRCHAT_IP:-}"
DEV_IP="${DEV_IP:-}"
PROTON_OPENVPN="${PROTON_OPENVPN:-auto}"
SEARXNG_USER="${SEARXNG_USER:-searxng}"
SEARXNG_GROUP="${SEARXNG_GROUP:-}"
PROXY_PORT="${PROXY_PORT:-8899}"
SEARXNG_PORT="${SEARXNG_PORT:-8888}"
POLICY_SCRIPT="/usr/local/sbin/searxng-vpn-policy"
FIREWALL_UNIT="/etc/systemd/system/searxng-privacy-firewall.service"
POLICY_UNIT="/etc/systemd/system/searxng-vpn-policy.service"
PROXY_UNIT="/etc/systemd/system/searxng-privacy-proxy.service"
PROXY_CONF="/etc/tinyproxy/searxng-privacy.conf"
OVPN_DIR="/etc/openvpn/proton-ovpn"
OVPN_AUTH="$OVPN_DIR/auth.txt"
OVPN_CONF="/etc/openvpn/client/searxng-proton.conf"
OPENVPN_UNIT="openvpn-client@searxng-proton.service"
BACKUP_DIR="/root/searxng-privacy-setup-$(date +%Y%m%d-%H%M%S)"

log() { printf '%s\n' "$*"; }
warn() { log "WARNING: $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "Run this script as root."
}

valid_ip_or_empty() {
  local ip="$1"
  [ -z "$ip" ] && return 0
  printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

backup() {
  local path="$1"
  [ -e "$path" ] || return 0
  mkdir -p "$BACKUP_DIR"
  local name="${path#/}"
  name="${name//\//__}"
  cp -a "$path" "$BACKUP_DIR/$name"
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null 2>&1 || warn "apt-get update failed; continuing with installed packages."
    apt-get install -y -qq iproute2 iptables openvpn tinyproxy ca-certificates curl >/dev/null 2>&1 \
      || warn "Could not install every package; setup will use what is already installed."
  fi
}

disable_conflicting_wireguard_units() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local units=()
  while IFS= read -r unit; do
    [ -n "$unit" ] && units+=("$unit")
  done < <(
    systemctl list-unit-files --type=service --no-legend 2>/dev/null \
      | awk '{print $1}' \
      | grep -Ei '^(wg-quick@.*(proton|searxng)|.*(wireguard|wg).*(proton|searxng)|.*proton.*(wireguard|wg|split)).service$' \
      || true
  )
  for unit in "${units[@]}"; do
    [ "$unit" = "$OPENVPN_UNIT" ] && continue
    if systemctl disable --now "$unit" >/dev/null 2>&1; then
      log "Disabled conflicting WireGuard/Proton unit: $unit"
    fi
  done
}

write_policy_script() {
  backup "$POLICY_SCRIPT"
  cat > "$POLICY_SCRIPT" <<EOF
#!/usr/bin/env bash
set -u

ACTION="\${1:-apply}"
SEARXNG_USER="$SEARXNG_USER"
HYPRCHAT_IP="$HYPRCHAT_IP"
DEV_IP="$DEV_IP"
SEARXNG_PORT="$SEARXNG_PORT"
PROXY_PORT="$PROXY_PORT"
TABLE_ID="177"
RULE_PRIO="17700"
UID_CHAIN="HC_SEARXNG_UID"
IN_CHAIN="HC_SEARXNG_IN"

uid_for_user() {
  id -u "\$SEARXNG_USER" 2>/dev/null
}

ipt() {
  iptables -w "\$@"
}

delete_jump() {
  local table_chain="\$1"
  shift
  while ipt -D "\$table_chain" "\$@" 2>/dev/null; do :; done
}

cleanup_uid() {
  local uid
  uid="\$(uid_for_user)" || return 0
  delete_jump OUTPUT -m owner --uid-owner "\$uid" -j "\$UID_CHAIN"
  ipt -F "\$UID_CHAIN" 2>/dev/null || true
  ipt -X "\$UID_CHAIN" 2>/dev/null || true
  while ip rule del priority "\$RULE_PRIO" table "\$TABLE_ID" 2>/dev/null; do :; done
  ip route flush table "\$TABLE_ID" 2>/dev/null || true
  ip route flush cache 2>/dev/null || true
}

cleanup_firewall() {
  delete_jump INPUT -p tcp -m multiport --dports "\$SEARXNG_PORT,\$PROXY_PORT" -j "\$IN_CHAIN"
  ipt -F "\$IN_CHAIN" 2>/dev/null || true
  ipt -X "\$IN_CHAIN" 2>/dev/null || true
}

add_bypass_route() {
  local ip="\$1"
  [ -z "\$ip" ] && return 0
  local route dev via
  route="\$(ip -4 route get "\$ip" 2>/dev/null | head -n1 || true)"
  dev="\$(printf '%s\n' "\$route" | awk '{for(i=1;i<=NF;i++) if(\$i=="dev") {print \$(i+1); exit}}')"
  via="\$(printf '%s\n' "\$route" | awk '{for(i=1;i<=NF;i++) if(\$i=="via") {print \$(i+1); exit}}')"
  [ -z "\$dev" ] && return 0
  if [ -n "\$via" ]; then
    ip route replace "\$ip/32" via "\$via" dev "\$dev" table "\$TABLE_ID" 2>/dev/null || true
  else
    ip route replace "\$ip/32" dev "\$dev" table "\$TABLE_ID" 2>/dev/null || true
  fi
}

private_dns_resolvers() {
  awk '/^nameserver[[:space:]]+/ {print \$2}' /etc/resolv.conf 2>/dev/null \\
    | grep -E '^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[0-1])\\.)' \\
    || true
}

apply_firewall() {
  cleanup_firewall
  ipt -N "\$IN_CHAIN" 2>/dev/null || true
  ipt -F "\$IN_CHAIN"
  ipt -A "\$IN_CHAIN" -i lo -j ACCEPT
  [ -n "\$HYPRCHAT_IP" ] && ipt -A "\$IN_CHAIN" -s "\$HYPRCHAT_IP" -p tcp --dport "\$SEARXNG_PORT" -j ACCEPT
  [ -n "\$HYPRCHAT_IP" ] && ipt -A "\$IN_CHAIN" -s "\$HYPRCHAT_IP" -p tcp --dport "\$PROXY_PORT" -j ACCEPT
  [ -n "\$DEV_IP" ] && ipt -A "\$IN_CHAIN" -s "\$DEV_IP" -p tcp --dport "\$SEARXNG_PORT" -j ACCEPT
  ipt -A "\$IN_CHAIN" -p tcp --dport "\$SEARXNG_PORT" -j REJECT --reject-with tcp-reset
  ipt -A "\$IN_CHAIN" -p tcp --dport "\$PROXY_PORT" -j REJECT --reject-with tcp-reset
  ipt -I INPUT 1 -p tcp -m multiport --dports "\$SEARXNG_PORT,\$PROXY_PORT" -j "\$IN_CHAIN"
}

apply_vpn_policy() {
  local uid tun_dev
  uid="\$(uid_for_user)" || exit 0
  cleanup_uid
  ip route replace 127.0.0.0/8 dev lo table "\$TABLE_ID" 2>/dev/null || true
  add_bypass_route "\$HYPRCHAT_IP"
  add_bypass_route "\$DEV_IP"
  for dns_ip in \$(private_dns_resolvers); do
    add_bypass_route "\$dns_ip"
  done
  tun_dev="\$(ip -o link show 2>/dev/null | awk -F': ' '/: tun[0-9A-Za-z_.-]*:/{print \$2; exit}')"
  if [ -n "\$tun_dev" ]; then
    ip route replace default dev "\$tun_dev" table "\$TABLE_ID"
  fi
  ip rule add uidrange "\$uid-\$uid" priority "\$RULE_PRIO" table "\$TABLE_ID" 2>/dev/null || true
  ip route flush cache 2>/dev/null || true

  ipt -N "\$UID_CHAIN" 2>/dev/null || true
  ipt -F "\$UID_CHAIN"
  ipt -A "\$UID_CHAIN" -o lo -j ACCEPT
  [ -n "\$HYPRCHAT_IP" ] && ipt -A "\$UID_CHAIN" -d "\$HYPRCHAT_IP" -j ACCEPT
  [ -n "\$DEV_IP" ] && ipt -A "\$UID_CHAIN" -d "\$DEV_IP" -j ACCEPT
  for dns_ip in \$(private_dns_resolvers); do
    ipt -A "\$UID_CHAIN" -d "\$dns_ip" -p udp --dport 53 -j ACCEPT
    ipt -A "\$UID_CHAIN" -d "\$dns_ip" -p tcp --dport 53 -j ACCEPT
  done
  ipt -A "\$UID_CHAIN" -o tun+ -j ACCEPT
  ipt -A "\$UID_CHAIN" -j REJECT
  ipt -I OUTPUT 1 -m owner --uid-owner "\$uid" -j "\$UID_CHAIN"
}

case "\$ACTION" in
  apply)
    apply_vpn_policy
    apply_firewall
    ;;
  apply-firewall)
    apply_firewall
    ;;
  vpn-down)
    cleanup_uid
    apply_firewall
    ;;
  cleanup-firewall)
    cleanup_firewall
    ;;
  cleanup)
    cleanup_uid
    cleanup_firewall
    ;;
  *)
    echo "usage: \$0 {apply|apply-firewall|vpn-down|cleanup-firewall|cleanup}" >&2
    exit 2
    ;;
esac
EOF
  chmod 0755 "$POLICY_SCRIPT"
}

write_firewall_unit() {
  backup "$FIREWALL_UNIT"
  cat > "$FIREWALL_UNIT" <<EOF
[Unit]
Description=HyprChat SearXNG privacy firewall
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$POLICY_SCRIPT apply-firewall
ExecStop=$POLICY_SCRIPT cleanup-firewall
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
}

write_proxy_config() {
  mkdir -p "$(dirname "$PROXY_CONF")"
  backup "$PROXY_CONF"
  cat > "$PROXY_CONF" <<EOF
Port $PROXY_PORT
Listen 0.0.0.0
User $SEARXNG_USER
Group $SEARXNG_GROUP
Timeout 600
LogLevel Warning
Syslog On
MaxClients 100
StartServers 1
MinSpareServers 1
MaxSpareServers 5
Allow 127.0.0.1
Allow $HYPRCHAT_IP
ConnectPort 443
ConnectPort 563
ViaProxyName "searxng-privacy"
DisableViaHeader Yes
EOF
}

write_proxy_unit() {
  backup "$PROXY_UNIT"
  cat > "$PROXY_UNIT" <<EOF
[Unit]
Description=HyprChat SearXNG outbound privacy proxy
After=network-online.target $OPENVPN_UNIT searxng-vpn-policy.service
Requires=searxng-vpn-policy.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/tinyproxy -d -c $PROXY_CONF
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

write_policy_unit() {
  backup "$POLICY_UNIT"
  cat > "$POLICY_UNIT" <<EOF
[Unit]
Description=HyprChat SearXNG UID VPN policy
After=$OPENVPN_UNIT
Requires=$OPENVPN_UNIT

[Service]
Type=oneshot
ExecStart=$POLICY_SCRIPT apply
ExecStop=$POLICY_SCRIPT vpn-down
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
}

find_proton_ovpn() {
  find "$OVPN_DIR" -maxdepth 1 -type f -name '*.ovpn' 2>/dev/null | sort | head -n1
}

proton_requested() {
  case "$(printf '%s' "$PROTON_OPENVPN" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

proton_assets_ready() {
  proton_requested || return 1
  [ -f "$OVPN_AUTH" ] || return 1
  [ -n "$(find_proton_ovpn)" ] || return 1
}

write_openvpn_config() {
  local src
  src="$(find_proton_ovpn)"
  [ -n "$src" ] || return 1
  mkdir -p "$(dirname "$OVPN_CONF")"
  backup "$OVPN_CONF"
  awk '
    /^auth-user-pass([[:space:]]|$)/ {next}
    /^auth-nocache([[:space:]]|$)/ {next}
    /^route-nopull([[:space:]]|$)/ {next}
    /^redirect-gateway([[:space:]]|$)/ {next}
    /^script-security([[:space:]]|$)/ {next}
    /^up[[:space:]]/ {next}
    /^down[[:space:]]/ {next}
    /^down-pre([[:space:]]|$)/ {next}
    {print}
  ' "$src" > "$OVPN_CONF"
  cat >> "$OVPN_CONF" <<EOF

auth-user-pass $OVPN_AUTH
auth-nocache
route-nopull
script-security 2
up $POLICY_SCRIPT apply
down $POLICY_SCRIPT vpn-down
down-pre
EOF
  chmod 0600 "$OVPN_CONF" "$OVPN_AUTH" 2>/dev/null || true
}

unit_file_exists() {
  systemctl list-unit-files "$1" --no-legend 2>/dev/null | grep -q .
}

wait_for_tun() {
  local i
  for i in $(seq 1 30); do
    ip -o link show 2>/dev/null | grep -Eq ': tun[0-9A-Za-z_.-]*:' && return 0
    sleep 1
  done
  return 1
}

disable_vpn_proxy_stack() {
  systemctl disable --now searxng-privacy-proxy.service >/dev/null 2>&1 || true
  systemctl disable --now searxng-vpn-policy.service >/dev/null 2>&1 || true
  systemctl disable --now "$OPENVPN_UNIT" >/dev/null 2>&1 || true
}

activate_firewall_only() {
  systemctl daemon-reload
  systemctl enable --now searxng-privacy-firewall.service >/dev/null 2>&1 \
    || warn "Could not enable privacy firewall service."
}

activate_vpn_proxy() {
  command -v tinyproxy >/dev/null 2>&1 || {
    warn "tinyproxy is not installed; proxy cannot be activated."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  command -v openvpn >/dev/null 2>&1 || {
    warn "openvpn is not installed; proxy cannot be activated."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  unit_file_exists "openvpn-client@.service" || {
    warn "openvpn-client@.service is unavailable; proxy cannot be activated."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }

  write_openvpn_config || {
    warn "Could not write Proton OpenVPN client config."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }

  systemctl daemon-reload
  systemctl enable --now "$OPENVPN_UNIT" >/dev/null 2>&1 || {
    warn "Could not start $OPENVPN_UNIT."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  wait_for_tun || {
    warn "OpenVPN did not create a tun device; leaving proxy disabled."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  systemctl enable searxng-vpn-policy.service >/dev/null 2>&1 || true
  systemctl restart searxng-vpn-policy.service >/dev/null 2>&1 || {
    warn "Could not apply UID VPN policy; leaving proxy disabled."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  systemctl enable --now searxng-privacy-proxy.service >/dev/null 2>&1 || {
    warn "Could not start searxng-privacy-proxy.service."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    return 0
  }
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(:|\\.)$PROXY_PORT$"; then
    echo "HYPRCHAT_PROXY_ACTIVE=1"
  else
    warn "Proxy service started but :$PROXY_PORT is not listening."
    echo "HYPRCHAT_PROXY_ACTIVE=0"
  fi
}

main() {
  need_root
  [ -n "$HYPRCHAT_IP" ] || die "HYPRCHAT_IP is required."
  valid_ip_or_empty "$HYPRCHAT_IP" || die "HYPRCHAT_IP must be an IPv4 address."
  valid_ip_or_empty "$DEV_IP" || die "DEV_IP must be empty or an IPv4 address."
  id -u "$SEARXNG_USER" >/dev/null 2>&1 || die "User '$SEARXNG_USER' does not exist. Install SearXNG first."
  SEARXNG_GROUP="${SEARXNG_GROUP:-$(id -gn "$SEARXNG_USER")}"

  mkdir -p "$BACKUP_DIR"
  log "Backup directory: $BACKUP_DIR"
  install_packages
  disable_conflicting_wireguard_units
  write_policy_script
  write_firewall_unit
  write_policy_unit
  write_proxy_config
  write_proxy_unit
  activate_firewall_only

  if ! proton_requested; then
    warn "PROTON_OPENVPN=$PROTON_OPENVPN; VPN/proxy activation skipped."
    disable_vpn_proxy_stack
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    exit 0
  fi

  if ! proton_assets_ready; then
    warn "Missing Proton OpenVPN assets. Expected $OVPN_DIR/*.ovpn and $OVPN_AUTH."
    warn "Firewall hardening was applied; VPN/proxy activation was skipped."
    disable_vpn_proxy_stack
    echo "HYPRCHAT_PROXY_ACTIVE=0"
    exit 0
  fi

  activate_vpn_proxy
}

main "$@"
