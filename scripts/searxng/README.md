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
| `searxng-web-proxy.py` | `/usr/local/bin/` |
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
- UDP/TCP DNS (port 53) for the search user is rejected outside `tun0`, before
  any LAN or loopback allowance, for both address families. Root's outage DNS
  fallback remains available for VPN recovery without exposing service DNS.
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
  so an intentional service restart actually stops the workers. The watchdog
  checks `/healthz` after VPN recovery and restarts SearXNG only if its listener
  fails. Normal VPN rotation/recovery preserves engine cooldowns.

The installed profile pool inspected on 2026-10-05 contains 111 US profiles.
Rotation changes exit servers; it does not currently provide country diversity.
Shared VPN addresses can still receive upstream blocks. Rotation is not a
guarantee against CAPTCHAs or a reason to clear engine cooldowns.

Hourly root cron entry:

```cron
0 * * * * /usr/bin/systemctl restart protonvpn-rotate.service
```

## Proxy trust and DNS privacy

The maintained proxy permits only configured clients (localhost and HyprChat
by default) and ports 80/443. It rejects private, loopback, link-local,
non-global, and mixed public/private DNS answers, then connects to the exact
validated public IPv4 address. It never resolves the target again while
connecting. HTTPS uses CONNECT; TLS remains between HyprChat and the site.

After installing and testing this proxy, HyprChat's private `.env` can contain:

```dotenv
SEARXNG_URL=http://192.168.1.56:8888
HYPRCHAT_OUTBOUND_PROXY=http://192.168.1.56:8899
HYPRCHAT_TRUSTED_WEB_PROXY=http://192.168.1.56:8899
```

The trusted URL must exactly match the configured outbound proxy. This explicit
opt-in moves page-fetch DNS validation to the proxy, through ProtonVPN, instead
of repeating it on the HyprChat host. URL/literal-address checks and checks on
every redirect remain in HyprChat. An unset or mismatched trust value retains
local DNS validation. Never enable trust for an arbitrary forwarding proxy.
Remove the trust value and restart HyprChat before rolling back proxy validation.

## Pinned release and compatibility patch

The 2026-10-05 deployment upgrades the February build `272833136` to upstream
`d48c4b555421e824342c51d68482dd0898e54d0f` (2026.10.4), plus the tracked
[`brave-html-fallback.patch`](patches/brave-html-fallback.patch). Brave currently
serves some result pages with function-wrapped Svelte data that the pinned
upstream JSON parser cannot read. The narrow fallback reads rendered web/news
HTML; it never executes JavaScript. Unknown HTML fails explicitly. The reported
version includes `+dirty` because this local patch is applied.

The new source and Python environment are isolated under
`/usr/local/searxng/releases/d48c4b555421/{src,venv}`. Production uses a separate
cache at `/var/cache/searxng/d48c4b555421`, selected by `TMPDIR` in uWSGI and
`/etc/systemd/system/searxng.service.d/30-release.conf`. Old source/environment
paths remain intact. Settings preserve existing engine enablement, categories,
and custom names where supported; new upstream engines remain disabled.
Retired `podcastindex` and `searchcode` modules are unavailable in this release.

For future updates, stage an exact upstream commit and its dependencies in a
new release directory. Reconcile settings against the effective old engine
configuration, including renamed templates and shortcut conflicts. Test on a
loopback-only port as the service user with separate cache files. Use the actual
service UID **and GID** from `id searxng`; they are not necessarily equal.
Apply the Brave patch only if still needed and `git apply --check` succeeds.
Run its four offline tests in the staged environment:

```bash
SEARXNG_SETTINGS_PATH=<staged-settings.yml> PYTHONPATH=<release>/src \
  <release>/venv/bin/python <repo>/scripts/searxng/test_brave_compat.py
```

Promote only after representative live searches and VPN outage checks pass.
Disable uWSGI request logging (`disable-logging = true`) to avoid storing every
search query in access logs. Engine errors may still contain request details.

This private deployment connects directly to uWSGI without a reverse proxy.
The pinned upstream still logs a missing `limiter.toml` warning at worker startup
and a missing forwarded-client-header error once per worker. Its request limiter
is disabled; these messages do not mean an upstream search engine is blocked.
Do not add fabricated forwarding headers or restart healthy workers to hide them.

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

Also attempt UDP and TCP DNS as the service user against public and LAN
resolvers (IPv4 and IPv6). Check rejection counters and capture `eth0` traffic
for a unique synthetic DNS query. A controlled 2026-10-05 check blocked all
eight DNS attempts, direct HTTPS, and proxied HTTPS, with zero captured test
DNS packets; root LAN access and subsequent VPN/proxy recovery succeeded.
Suspend scheduled rotation/watchdog activity during this check and install an
independent timed restore before stopping the tunnel. Always restore in a
`finally`/trap path as well. This is a bounded outage check, not continuous leak
monitoring.

From HyprChat (CT 120), verify an actual query, not only `/healthz`:

```bash
curl -sS --max-time 12 'http://192.168.1.56:8888/search?q=latest+US+news&format=json'
```

An HTTP 200 response with zero results and engine connection errors is an
upstream failure. It does not establish that SearXNG works or that credentials
have expired.

## Rollback of the 2026-10-05 deployment

Private CT 114 backups are in `/root/searxng-backup-20261005`; the prior source
and environment remain at `/usr/local/searxng/{searxng-src,searx-pyenv}`.
To roll back only SearXNG, stop its service, restore `etc/searxng/settings.yml`
and `etc/searxng/uwsgi.ini` from that backup, remove only the new
`30-release.conf` drop-in, reload systemd, and start SearXNG. Preserve the other
drop-ins. Ensure settings remain readable by the actual `searxng` group. The
hardened proxy and DNS guard work with the old SearXNG release and can stay.

HyprChat backups are in `/opt/hyprchat/backups/searxng-20261005`: the four
changed backend files, `.env`, and `frontend-dist/`. Restore those files and
restart `hyprchat` to roll back its deployment. Do not restore a whole backend
tree over unrelated changes. Validate `/api/health` and a real search afterward.
If reverting the proxy itself, remove `HYPRCHAT_TRUSTED_WEB_PROXY` and restart
HyprChat **first**; otherwise its safety checks would trust an older proxy.

The older `scripts/setup-searxng-privacy.sh` installer refuses to overwrite a
maintained rotation-service/killswitch installation. Use the staged workflow
above for this host.
