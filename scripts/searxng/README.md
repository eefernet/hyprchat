# SearXNG VPN egress + self-healing rotation

Infra scripts for the SearXNG LXC (`192.168.1.120` → `192.168.1.141`, `root`).
SearXNG's outbound engine traffic is forced through ProtonVPN (OpenVPN) with a
fail-closed killswitch, the exit server is rotated, and a watchdog re-establishes
the tunnel automatically if it drops.

> These files are **version-control copies** of what runs on the box. They are
> NOT auto-deployed by `deploy_monitor.py`. Deploy them by hand (see below).

## How it fits together

```
searxng (uid) ──fwmark policy routing (table 100)──► tun0 ──ProtonVPN──► internet
                         │
            searxng-vpn-killswitch.sh  (fail-closed: no tun0 ⇒ searxng egress unreachable)
```

| File | Installed path | Role |
|---|---|---|
| `rotate-ovpn.sh` | `/usr/local/bin/rotate-ovpn.sh` | Pick a Proton config, bring up the tunnel, apply killswitch. Hourly cron + called by the watchdog. |
| `searxng-vpn-killswitch.sh` | `/usr/local/sbin/searxng-vpn-killswitch.sh` | `apply`/`down`/`status`: fwmark routing + iptables fail-closed for the `searxng` uid; sets VPN DNS on `apply`, restores public DNS on `down`. |
| `ovpn-up.sh` / `ovpn-down.sh` | `/usr/local/bin/` | OpenVPN `--up`/`--down` hooks (apply / tear down the killswitch). |
| `searxng-vpn-watchdog.sh` | `/usr/local/sbin/searxng-vpn-watchdog.sh` | Every 3 min: re-establish the tunnel if it actually dropped; restore DNS during an outage; back off if every server fails. |
| `searxng-vpn-watchdog.service` / `.timer` | `/etc/systemd/system/` | Runs the watchdog on a 3-minute timer. |

ProtonVPN OpenVPN configs live in `/etc/openvpn/proton-ovpn/*.ovpn` (~112,
`auth.txt` alongside). Their `remote` lines are literal IPs.

## Key design points (learned the hard way — see 2026-06-22 incident)

- **VPN_UP success gate** is `tun0` + `ip route show table 100 default` via tun0 +
  a **DNS-free** egress probe (`curl https://1.1.1.1/` by IP as the `searxng`
  user). Do **not** use `ifconfig.me` or any DNS-dependent endpoint as a liveness
  probe — rate-limiting / Proton-DNS timing makes it false-fail and churn healthy
  tunnels on every tick.
- `rotate-ovpn.sh` tries up to **8 distinct-IP servers** per run (a single dead
  Proton exit must not blackhole search) and shares a `flock`
  (`/run/lock/searxng-ovpn.lock`) with the watchdog so cron and watchdog never
  kill each other's OpenVPN.
- The **watchdog** treats a tunnel as healthy on **structural** liveness
  (openvpn process + tun0 + route) so a transient probe blip never tears down a
  working tunnel; the egress zombie-check only re-rolls after **2 consecutive**
  failures. Restart of SearXNG uses `--no-block` (a plain restart takes ~85 s and
  would otherwise hold the watchdog lock). Backoff: 3 consecutive rotate failures
  ⇒ 30-min cooldown (likely creds/egress, not a dead exit).
- **DNS:** `10.96.0.1` in `/etc/resolv.conf` is ProtonVPN's pushed DNS (tunnel
  subnet `10.96.0.0/16`), set by the killswitch `apply`. The killswitch `down`
  now restores `1.1.1.1`/`192.168.1.1` so the box keeps DNS when the tunnel is
  down. `eth0` is DHCP; `/etc/dhcp/dhclient.conf` pins DNS with:
  `supersede domain-name-servers 1.1.1.1, 192.168.1.1;`

## Deploy (manual)

```bash
# from this directory, to the box (root@192.168.1.141)
scp rotate-ovpn.sh root@192.168.1.141:/usr/local/bin/
scp searxng-vpn-killswitch.sh ovpn-up.sh ovpn-down.sh root@192.168.1.141:/usr/local/sbin/   # ovpn-up/down go in /usr/local/bin
scp searxng-vpn-watchdog.sh root@192.168.1.141:/usr/local/sbin/
scp searxng-vpn-watchdog.service searxng-vpn-watchdog.timer root@192.168.1.141:/etc/systemd/system/
ssh root@192.168.1.141 'chmod +x /usr/local/bin/rotate-ovpn.sh /usr/local/bin/ovpn-*.sh /usr/local/sbin/searxng-vpn-*.sh && systemctl daemon-reload && systemctl enable --now searxng-vpn-watchdog.timer'
```

Hourly rotation cron (on the box):

```
0 * * * * /usr/local/bin/rotate-ovpn.sh >> /var/log/vpn-rotation.log 2>&1
```

## Verify

```bash
ssh root@192.168.1.141 'ip -br addr show tun0; ip route show table 100 default; \
  runuser -u searxng -- curl -4 -sS --connect-timeout 8 -o /dev/null -w "%{http_code}\n" https://1.1.1.1/'
# search (from the HyprChat host, which can reach :8888):
curl -s "http://192.168.1.141:8888/search?q=test&format=json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["results"]),"results",d["unresponsive_engines"])'
```
