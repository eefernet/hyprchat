#!/usr/bin/env bash
# Run inside the newly provisioned VM, from a source bundle. No project code runs here.
set -euo pipefail
[[ $(id -u) == 0 && $(hostname -s) == hephaestus ]] || { echo 'Expected root inside hephaestus' >&2; exit 1; }
source /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 ]] || { echo 'Expected Debian 13' >&2; exit 1; }
# Proxmox changes cloud-init's instance identity when the cutover IP changes.
# Retain this already-initialized guest's SSH identity on subsequent boots.
printf 'ssh_deletekeys: false\n' > /etc/cloud/cloud.cfg.d/99-hephaestus-host-keys.cfg
if [[ -f /etc/hephaestus/bootstrap-complete ]]; then
  [[ $(findmnt -n -o UUID /srv/hephaestus) == "$(cat /etc/hephaestus/data-uuid)" ]] || { echo 'Data disk is not mounted as provisioned' >&2; exit 1; }
  [[ $(docker info --format '{{.DockerRootDir}}') == /srv/hephaestus/docker ]] || { echo 'Docker storage changed' >&2; exit 1; }
  grep -q '^root = "/srv/hephaestus/containerd"' /etc/containerd/config.toml || { echo 'containerd storage changed' >&2; exit 1; }
  echo 'Hephaestus base already provisioned; existing services and data preserved.'
  exit 0
fi
if command -v docker >/dev/null && [[ -n $(docker ps -q 2>/dev/null) ]]; then
  echo 'Drain running containers before changing the VM base configuration.' >&2; exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y ca-certificates curl python3 python3-venv qemu-guest-agent xfsprogs jq rsync strace
systemctl enable --now qemu-guest-agent
install -d -m 700 /etc/hephaestus

# Only the installer-created, serial-labelled, completely blank disk may be initialized.
DATA_DEVICE=$(lsblk -J -o PATH,SERIAL,TYPE | python3 -c 'import json,sys; a=[d["path"] for d in json.load(sys.stdin)["blockdevices"] if d.get("serial")=="HEPHAESTUS_DATA" and d["type"]=="disk"]; assert len(a)==1,"Expected exactly one labelled data disk"; print(a[0])')
if ! blkid "$DATA_DEVICE" >/dev/null 2>&1; then
  [[ ${1:-} == --initialize-new-data-disk ]] || { echo 'Blank data disk requires explicit initialization' >&2; exit 1; }
  [[ -z $(wipefs -n --noheadings -o TYPE "$DATA_DEVICE") ]] || { echo 'Data disk has existing signatures' >&2; exit 1; }
  [[ $(lsblk -n -o TYPE "$DATA_DEVICE" | wc -l) == 1 ]] || { echo 'Data disk contains partitions' >&2; exit 1; }
  mkfs.xfs -L hephaestus "$DATA_DEVICE"
fi
[[ $(blkid -s TYPE -o value "$DATA_DEVICE") == xfs && $(blkid -s LABEL -o value "$DATA_DEVICE") == hephaestus ]] || { echo 'Unexpected data filesystem; refusing to format' >&2; exit 1; }
DATA_UUID=$(blkid -s UUID -o value "$DATA_DEVICE")
if [[ -f /etc/hephaestus/data-uuid && $(cat /etc/hephaestus/data-uuid) != "$DATA_UUID" ]]; then
  echo 'Data disk identity changed' >&2; exit 1
fi
printf '%s\n' "$DATA_UUID" > /etc/hephaestus/data-uuid
mkdir -p /srv/hephaestus
if ! grep -q '^UUID=.* /srv/hephaestus ' /etc/fstab; then
  printf 'UUID=%s /srv/hephaestus xfs defaults,prjquota 0 2\n' "$DATA_UUID" >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q /srv/hephaestus || mount /srv/hephaestus
[[ $(findmnt -n -o UUID /srv/hephaestus) == "$DATA_UUID" ]] || { echo 'Wrong filesystem mounted' >&2; exit 1; }
install -d -m 700 /srv/hephaestus/{control,state,projects,applications,runs,caches,docker,containerd,releases}
install -d -m 755 /run/hephaestus /opt/hephaestus

if [[ ! -f /etc/hephaestus/docker-packages.lock ]]; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod 644 /etc/apt/keyrings/docker.asc
  printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable\n' > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  : > /etc/hephaestus/docker-packages.lock.tmp
  for package in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; do
    version=$(apt-cache policy "$package" | awk '/Candidate:/ {print $2}')
    [[ -n $version && $version != '(none)' ]] || exit 1
    printf '%s=%s\n' "$package" "$version" >> /etc/hephaestus/docker-packages.lock.tmp
  done
  mv /etc/hephaestus/docker-packages.lock.tmp /etc/hephaestus/docker-packages.lock
fi
mapfile -t DOCKER_PACKAGES < /etc/hephaestus/docker-packages.lock
apt-get install -y "${DOCKER_PACKAGES[@]}"
mkdir -p /etc/docker /etc/containerd
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/etc/docker/daemon.json')
data=json.loads(p.read_text()) if p.exists() else {}
if data.get('data-root', '/srv/hephaestus/docker') != '/srv/hephaestus/docker':
    raise SystemExit('Existing Docker storage requires an explicit migration')
data.update({'data-root':'/srv/hephaestus/docker', 'log-driver':'local',
             'log-opts':{'max-size':'10m','max-file':'3'}, 'live-restore':True,
             'builder':{'gc':{'enabled':True,'defaultKeepStorage':'8GB'}}})
p.write_text(json.dumps(data,indent=2)+'\n')
p=Path('/etc/containerd/config.toml')
text=p.read_text() if p.exists() else 'version = 2\n'
import re
if re.search(r'^root\s*=',text,re.M):
    existing=re.search(r'^root\s*=\s*["\']([^"\']+)',text,re.M).group(1)
    if existing not in ('/var/lib/containerd','/srv/hephaestus/containerd'):
        raise SystemExit('Unexpected containerd storage')
    text=re.sub(r'^root\s*=.*$', 'root = "/srv/hephaestus/containerd"',text,flags=re.M)
else: text='root = "/srv/hephaestus/containerd"\n'+text
p.write_text(text)
PY
cat > /etc/systemd/system/hephaestus-workloads.slice <<'EOF'
[Unit]
Description=Aggregate project, document and application resource boundary
[Slice]
MemoryAccounting=yes
MemoryHigh=4608M
MemoryMax=5G
MemorySwapMax=0
CPUQuota=350%
TasksMax=1024
EOF
systemctl daemon-reload
systemctl start hephaestus-workloads.slice
systemctl restart containerd docker
systemctl enable docker
dpkg-query -W > /etc/hephaestus/installed-packages.txt
docker info --format '{{json .}}' > /etc/hephaestus/docker-info.json
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=256M\nRuntimeMaxUse=64M\n' > /etc/systemd/journald.conf.d/hephaestus.conf
touch /etc/hephaestus/bootstrap-complete
echo 'Hephaestus base ready. Install a tested runtime release before switching HyprChat.'
