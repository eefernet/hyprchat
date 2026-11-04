# Hephaestus execution VM

Hephaestus is live on pve2 as **VM118**, serving `192.168.1.201`. The matching HyprChat backend and frontend are deployed. The verified old Codebox CT115 and its 51 GiB disk were deleted after all 16 migration gates passed. Daedalus uses OpenHands 1.59; the current Qwen3.6 model and inference settings are retained.

## Provisioning

Run from a complete HyprChat checkout on pve2:

```sh
sudo scripts/create-hephaestus-vm.sh --plan
sudo scripts/create-hephaestus-vm.sh
```

The companion prompt in `scripts/create-lxc.sh` uses the same installer. Existing HyprChat installations can run the standalone command. `--vmid`, `--storage`, `--bridge`, `--ssh-public-key`, and `--state-dir` override the corresponding defaults. `--provision-only` creates the VM without installing its runtime. The matching private SSH key must be available locally to complete provisioning.

The installer checks IDs across VMs and containers, records a unique ownership tag before creation, verifies the Debian 13 cloud image against its published SHA512, and creates 4 vCPUs, 8 GiB RAM, a 20 GiB OS disk and an 80 GiB data disk. Configuration is recorded under `/var/lib/hephaestus-installer` on pve2. The new guest starts on DHCP. Installation never stops or deletes another guest.

Inside the VM, `scripts/hephaestus/bootstrap.sh` configures Docker from its official Debian repository, records exact package versions, installs QEMU Guest Agent, and mounts the serial-labelled data disk at `/srv/hephaestus` with XFS project quotas. Initial formatting requires both a completely blank disk and `--initialize-new-data-disk`. A changed UUID, occupied disk or unexpected filesystem fails closed. A completed rerun preserves data and running services. SSH host keys are retained across cloud-init instance changes, including the DHCP-to-static-IP cutover.

`python3 scripts/hephaestus/install.py --source /path/to/hyprchat --ollama-url http://192.168.1.110:11434` stages an immutable source release, hash-checked Python locks, image identities, matching Playwright browsers and the existing pinned Qwen tokenizer/template. It does not modify the model. Activating another release requires drained executions and stopped pinned applications; unchanged releases are not restarted. Only the modern worker is provisioned. Application compatibility checks precede retirement of any existing legacy service.

## Runtime layout and boundaries

| Data disk directory | Purpose |
|---|---|
| `control` | Broker ledger, quota registry, immutable dependency environments and browser/tokenizer assets |
| `releases` | Immutable controller source and release manifests |
| `projects` | Owner/conversation-scoped generic workspaces |
| `state` | Worker jobs, immutable revisions, evidence and preview records |
| `applications` | Reserved managed application storage |
| `runs` | Disposable execution scratch |
| `caches` | Reconstructible shared caches |
| `docker`, `containerd` | Separate Docker and containerd storage roots |

Only the trusted broker owns Docker access. Project containers have a read-only root filesystem, no Docker socket, no host network, dropped capabilities, and explicit controller-selected mounts. Dependency downloads use the existing public-address-only gateway. Document parsers have no public network access. Signed internal scope headers identify the authenticated owner and conversation/project; worker operation/revision metadata persists with jobs.

The scoped execution API uses port 8585 and the OpenHands 1.59 worker uses port 8587. Set `CODEBOX_URL` to the execution API and `DAEDALUS_ENGINE_URL` to the modern worker. `CODEBOX_URL` remains a compatibility configuration name. Set `HEPHAESTUS_TOKEN` on HyprChat from the new VM's private `/etc/hephaestus/token`, using an environment file with mode 0600. Never commit or print that value. The connection and status UI identifies the replacement as Hephaestus based on its health response. A local deploy configuration can use a `hephaestus` alias or retain `codebox` with `runtime: "hephaestus"`. Worker updates use the immutable installer instead of host `pip install`.

The aggregate workload slice limits project execution, preparation, documents and previews to 5 GiB. Individual coding operations retain a 3 GiB ceiling. Nested dependency preparation shares the operation's reservation and cgroup, avoiding an admission deadlock. Application containers use 1 GiB and at most two may be pinned concurrently. Preview process identities use the Linux boot ID and process start ticks, so clock synchronization cannot trigger duplicate recovery. Queued work retains its deadline and can be cancelled. Broker restart stops recorded containers without replaying their actions.

Project data has a 10 GiB quota and execution scratch a 2 GiB quota. Cleanup begins at 80% usage and targets 70%, while preserving a 10 GiB reserve. Active and pinned cache leases protect dependencies; revisions, artifacts and application data are not cache-cleanup targets. Docker logs rotate at 10 MiB with three files. Storage errors report the required operator action.

## Applications

Temporary previews expire after 15 minutes. **Keep running** retains the selected source revision and runtime image. **Stop** preserves data; **Restart** resumes that selected application; **Delete application data** explicitly removes its session data. Pinning does not accept a revision, change verification, or follow later edits. Web and desktop content is exposed through authenticated HyprChat/noVNC preview routes. Downloaded artifacts and acceptance remain revision-bound.

## Operations and upgrades

Codebox CT115 and its disk were retired after the Hephaestus migration. Ordinary installer reruns never delete guests. The old guest retirement helpers have been removed.

Hephaestus uses pve2's installer SSH key in `/var/lib/hephaestus-installer/id_ed25519`. Connect to the VM address recorded in the installer state. `deploy_monitor.py` refuses mutable execution-worker copies; use a complete immutable release. Drain coding, document operations and previews before activation, then verify the matching application and stop any retired worker. Rollback resources remain protected while referenced.

Historical one-off validation reports are disposable under the latest-evidence retention policy. Keep the latest validation receipts and required rollback manifests; runtime state is not a substitute for HyprChat's durable artifacts.

## Storage maintenance

Settings → Storage & Cleanup separates HyprChat's local output folder from the
Hephaestus data disk. HyprChat retains delivered bundles in its durable artifact
store and records exact-revision acceptance; Hephaestus retains the working
project, revisions, dependencies, and preview/application data. Accept does not
remove the VM workspace.

The VM inventory is cached under `/srv/hephaestus/control/storage`. A background
FIEMAP scan accounts for physical XFS extents, hard links and sparse files. Blocks
shared across categories and unattributed filesystem allocation appear under
Shared/system. Docker-reported image and build-cache sizes are estimates that
must not be added to the physical totals. Failed or changing scans preserve the
last valid snapshot with a stale warning.

Only the Main administrator profile can preview or execute VM maintenance.
Requests to the internal service carry a signed maintenance scope. Resource
identities, short-lived previews, serialized admission, cache leases and durable
operation receipts protect deletion. Unknown directories are kept. Reset requires
`DELETE VM PROJECT DATA`, blocks active coding/document/preview work, preserves
controller ledgers, and marks removed workspaces unavailable. It never deletes
HyprChat chats or saved artifacts. Installing the control does not run a reset.

The ten-minute timer retains completed test environments for 24 hours and unused
caches for seven days. Pressure cleanup starts at 80% usage or below a 10 GiB
reserve and aims for 70%. Manual safe cleanup ignores age thresholds but retains
reference and lease checks. Docker storage is removed using Docker operations;
containerd directories are never deleted directly. A verified retention manifest
protects the current release and a rollback before image/release cleanup is enabled.

Obsolete test controllers are drained, stopped and disabled before retirement.
Evidence bundles contain per-file SHA-256 manifests, and are verified on HyprChat
under `/opt/hyprchat/data/evaluation-archives/hephaestus/<date>-storage-cleanup/`
before the VM acknowledges them as removable. Original evaluation records are
archived unchanged. Internal SDK bookkeeping is owned by its parent store; it is
not a separate test environment or an independent active job.

`HEPHAESTUS_SHARED_CACHE=1` enables optional controller-published public downloads
and compatible browser binaries. Containers receive these read-only; installation
and build outputs stay private. Python virtual environments remain at their final
operation paths. Only exact public wheel pins and public integrity-pinned npm
archives are prefetched; other dependency setups keep their existing preparation
path. Browser entries include the declared driver identity, runtime image,
platform, and browser manifest. Project scripts cannot publish shared entries.
Existing Docker image layers are shared regardless of this optional setting.
