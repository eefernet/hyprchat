# SearXNG VPN egress and recovery

SearXNG is **CT 114 on pve2 (`192.168.1.101`)**, with static address
`192.168.1.56/24` and gateway `192.168.1.1`. Access it through `pct exec 114`
or `pct enter 114` on pve2; a separate SearXNG root login is unnecessary.
HyprChat (`192.168.1.120`) uses search on port 8888 and the Python web proxy
on port 8899. Both services run as the `searxng` user (currently UID 999).

These are versioned copies of installed infrastructure files. They are not
auto-deployed by `deploy_monitor.py`.

| File | Installed path |
|---|---|
| `rotate-ovpn.sh`, `ovpn-up.sh`, `ovpn-down.sh` | `/usr/local/bin/` |
| `searxng-vpn-killswitch.sh`, `searxng-vpn-watchdog.sh` | `/usr/local/sbin/` |
| `protonvpn-rotate.service`, `searxng-vpn-watchdog.service`, `searxng-vpn-watchdog.timer` | `/etc/systemd/system/` |
| `20-shutdown.conf` | `/etc/systemd/system/searxng.service.d/` |

The existing `searxng-vpn-killswitch.service` must be enabled before these
services start. Proton profiles and credentials remain in
`/etc/openvpn/proton-ovpn/`; never copy `auth.txt` into this repository.

## Routing and process ownership

- Main-table traffic (including OpenVPN's connection to its server) needs the
  LAN gateway. Persist `gw=192.168.1.1` in CT 114's `net0` configuration,
  preserving its other attributes. Proxmox generates the guest interface file.
- UID 999 receives mark `0x1`. Priority 100 routes it through table 100, which
  contains the LAN route and either a VPN default or an unreachable default.
  Priority 101 prohibits marked traffic from falling through to the main table,
  including while the VPN table is rebuilt.
- IPv6 public egress is rejected for the search user. The IPv6 chain is replaced
  atomically; loopback and the configured local IPv6 subnet remain reachable.
- `protonvpn-rotate.service` owns OpenVPN. Cron, the watchdog, and manual
  `rotate-ovpn.sh` invocations all request a restart of that service.
- The launcher closes lock descriptors 8 and 9 before daemonizing OpenVPN.
  Lock contention returns 75. The watchdog reads the **ExecStart** status of the
  forking service and does not count a skipped rotation as a connection failure.
- Rotation tries at most eight distinct servers, with bounded connection and
  egress probes and a 600-second service startup limit. The watchdog backs off
  after three failed rotations, and tolerates one failed egress probe while a
  tunnel is structurally up.
- A working tunnel requires the service-owned process, `tun0`, the table-100
  VPN default, and a DNS-free HTTPS probe as the search user. A public-IP lookup
  is diagnostic only. DNS follows the VPN gateway while connected.
- uWSGI normally treats SIGTERM as a reload. The shutdown drop-in uses SIGQUIT
  so a service restart actually stops the workers and clears engine suspensions.

Hourly root cron entry:

```cron
0 * * * * /usr/bin/systemctl restart protonvpn-rotate.service
```

## Updating an existing installation

Make a private backup of the CT configuration, installed files, root crontab,
and SearXNG settings before replacing them. Transfer these files to pve2 and
use `pct push 114 <host-file> <installed-path> --perms 0755` for shell scripts
(`0644` for units/drop-ins). Run `bash -n` on scripts, then
`systemd-analyze verify` on the units and `systemctl daemon-reload` inside CT 114.

Suspend the watchdog timer and the hourly rotation entry during maintenance.
Install and apply the routing protection **before** adding a missing gateway.
Stop a stale OpenVPN process only after verifying its PID and executable. Start
`protonvpn-rotate.service`, verify the tunnel, then restore the timer and cron.
Do not remove the lock file to work around a live descriptor holding its lock.

The September 2026 outage involved a missing gateway and a daemon holding the
rotation lock for days. The existing VPN credentials worked after those repairs.
Bing also needed explicit `disabled: false` in its existing settings block:
with `use_default_settings: true`, merely listing an engine does not necessarily
enable it. Enable engines based on live results; individual VPN exits can still
receive upstream denials or rate limits.

## Verification (inside CT 114 unless noted)

```bash
systemctl is-active protonvpn-rotate searxng searxng-web-proxy searxng-vpn-watchdog.timer
ip -4 route get 193.37.254.66
ip -4 rule
ip route show table 100
flock -n /run/lock/searxng-ovpn.lock true
runuser -u searxng -- curl -4 -sS --connect-timeout 5 --max-time 10 -o /dev/null -w '%{http_code}\n' https://1.1.1.1/
tail -n 15 /var/log/vpn-watchdog.log
```

When the tunnel is deliberately down during maintenance, marked public routes
and search-user HTTPS must fail while the root LAN route still works. Never
disable the policy rule or firewall as a search workaround.

From HyprChat (CT 120), verify an actual query, not only `/healthz`:

```bash
curl -sS --max-time 12 'http://192.168.1.56:8888/search?q=latest+US+news&format=json'
```

An HTTP 200 response with zero results and engine connection errors is an
upstream failure. It does not establish that SearXNG works or that credentials
have expired.
