# Hephaestus execution VM

Hephaestus is live on pve2 as **VM118**, serving `192.168.1.201`. The matching HyprChat backend and frontend are deployed. The verified old Codebox CT115 and its 51 GiB disk were deleted after all 16 migration gates passed. The production Daedalus engine selection and model settings remain unchanged; the opt-in SDK 1.59 engine retains Qwen3.6.

## Provisioning

Run from a complete HyprChat checkout on pve2:

```sh
sudo scripts/create-hephaestus-vm.sh --plan
sudo scripts/create-hephaestus-vm.sh
```

The companion prompt in `scripts/create-lxc.sh` uses the same installer. Existing HyprChat installations can run the standalone command. `--vmid`, `--storage`, `--bridge`, `--ssh-public-key`, and `--state-dir` override the corresponding defaults. `--provision-only` creates the VM without installing its runtime. The matching private SSH key must be available locally to complete provisioning.

The installer checks IDs across VMs and containers, records a unique ownership tag before creation, verifies the Debian 13 cloud image against its published SHA512, and creates 4 vCPUs, 8 GiB RAM, a 20 GiB OS disk and an 80 GiB data disk. Configuration is recorded under `/var/lib/hephaestus-installer` on pve2. The new guest starts on DHCP. Installation never stops or deletes another guest.

Inside the VM, `scripts/hephaestus/bootstrap.sh` configures Docker from its official Debian repository, records exact package versions, installs QEMU Guest Agent, and mounts the serial-labelled data disk at `/srv/hephaestus` with XFS project quotas. Initial formatting requires both a completely blank disk and `--initialize-new-data-disk`. A changed UUID, occupied disk or unexpected filesystem fails closed. A completed rerun preserves data and running services. SSH host keys are retained across cloud-init instance changes, including the DHCP-to-static-IP cutover.

`python3 scripts/hephaestus/install.py --source /path/to/hyprchat --ollama-url http://192.168.1.110:11434` stages an immutable source release, hash-checked Python locks, image identities, matching Playwright browsers and the existing pinned Qwen tokenizer/template. It does not modify the model. Activating another release requires drained executions and stopped pinned applications; unchanged releases are not restarted. The legacy and modern worker units are staged separately and require compatibility validation before activation.

## Runtime layout and boundaries

| Data disk directory | Purpose |
|---|---|
| `control` | Broker ledger, quota registry, immutable dependency environments and browser/tokenizer assets |
| `releases` | Immutable controller source and release manifests |
| `projects` | Owner/conversation-scoped generic workspaces and legacy projects |
| `state` | Worker jobs, immutable revisions, evidence and preview records |
| `applications` | Reserved managed application storage |
| `runs` | Disposable execution scratch |
| `caches` | Reconstructible shared caches |
| `docker`, `containerd` | Separate Docker and containerd storage roots |

Only the trusted broker owns Docker access. Project containers have a read-only root filesystem, no Docker socket, no host network, dropped capabilities, and explicit controller-selected mounts. Dependency downloads use the existing public-address-only gateway. Document parsers have no public network access. Signed internal scope headers identify the authenticated owner and conversation/project; worker operation/revision metadata persists with jobs.

The compatibility API uses port 8585, legacy OpenHands/Aider port 8586, and the opt-in SDK 1.59 worker port 8587. `CODEBOX_URL`, `OPENHANDS_URL`, `AIDER_WORKER_URL` and `DAEDALUS_ENGINE_URL` retain their meanings. Set `HEPHAESTUS_TOKEN` on HyprChat from the new VM's private `/etc/hephaestus/token`, using an environment file with mode 0600. Never commit or print that value. The connection and status UI identifies the replacement as Hephaestus based on its health response. A local deploy configuration can use a `hephaestus` alias or retain `codebox` with `runtime: "hephaestus"`. Worker updates use the immutable installer instead of host `pip install`.

The aggregate workload slice limits project execution, preparation, documents and previews to 5 GiB. Individual coding operations retain a 3 GiB ceiling. Nested dependency preparation shares the operation's reservation and cgroup, avoiding an admission deadlock. Application containers use 1 GiB and at most two may be pinned concurrently. Preview process identities use the Linux boot ID and process start ticks, so clock synchronization cannot trigger duplicate recovery. Queued work retains its deadline and can be cancelled. Broker restart stops recorded containers without replaying their actions.

Project data has a 10 GiB quota and execution scratch a 2 GiB quota. Cleanup begins at 80% usage and targets 70%, while preserving a 10 GiB reserve. Active and pinned cache leases protect dependencies; revisions, artifacts and application data are not cache-cleanup targets. Docker logs rotate at 10 MiB with three files. Storage errors report the required operator action.

## Applications

Temporary previews expire after 15 minutes. **Keep running** retains the selected source revision and runtime image. **Stop** preserves data; **Restart** resumes that selected application; **Delete application data** explicitly removes its session data. Pinning does not accept a revision, change verification, or follow later edits. Web and desktop content is exposed through authenticated HyprChat/noVNC preview routes. Downloaded artifacts and acceptance remain revision-bound.

## Migration and retirement

1. Inventory active operations and services. Archive historical evaluation reports, traces, source/configuration and dependency identities off CT115. Verify both archive checksum and readability.
2. Validate the replacement while Codebox continues serving production. Run `scripts/hephaestus/qualify_languages.py` serially inside Hephaestus. Availability records are tied to the exact language image; untested languages remain unavailable.
3. Complete upload/file/download, legacy OpenHands/Aider, opt-in modern, document, web, Qt/GTK/Swing, preview persistence, containment, cancellation, restart and exact-revision tests.
4. Drain HyprChat execution/document jobs. Back up HyprChat's SQLite database and configuration. Mark intentionally discarded worker workspaces unavailable while retaining job JSON, artifacts, accepted revisions, budgets and historical evidence.
5. Stop CT115, assign its address to Hephaestus, activate the matching HyprChat integration, and verify service health, authenticated browser previews, console behavior and storage. Reboot Hephaestus and repeat recovery checks. Keep CT115 stopped for rollback until all gates pass.
6. Build a root-owned migration record with version 1, old guest/config identity, the verified evidence archive, replacement VM tag/release, cutover/reboot timestamps, workspace retirement confirmation, free-space measurement and every report required by `retire_codebox.REQUIRED_GATES`. Every gate must pass, reference the same release and include its report path/SHA256.
7. On pve2, run `python3 scripts/hephaestus/retire_codebox.py --record <migration.json>` to validate. Only the explicit `--retire-verified-codebox-ct115` flag authorizes deletion. The helper checks the exact archived CT115 configuration, hostname, MAC, disk, stopped state and replacement identity. Ordinary installers never invoke it.

Retain the old address/configuration and releases for rollback until step 7. No migration result qualifies the modern engine or resolves its remaining generation/GUI failures. Original Daedalus evaluation scores and evidence remain separate from these infrastructure checks.

## Deployment and validation record — 2026-11-03

The deployed worker release is `/srv/hephaestus/releases/f121221dcfc470570876`. HyprChat runs `/opt/hyprchat/.releases/20261103-092340-8b005f03`; its immutable frontend and matching backend were verified through the production HTTPS site. The later worker fix changes preview process identity without changing backend API shapes or the model engine.

Validation includes 29 language profiles, legacy OpenHands/Aider with the existing production model, opt-in SDK 1.59 with Qwen3.6, Python CLI and packaged Java, all four document formats, uploaded projects and downloads, served/file web plus Qt/GTK/Swing interaction, owner isolation, cancellation/deadlines, memory/process/disk containment, cache leases, and restart recovery. Five deliberately incorrect UI fixtures were rejected. The focused final Python regressions passed 133 tests with one skip; the frontend suite passed 156 tests.

The compatibility suite ran on `5d1c507f7c95130869f0`. Source manifests establish unchanged engine, dependency locks and runtime images in the final worker; preview lifecycle and recovery changes were tested separately on the final release. Temporary expiry tests verified the configured 900-second lifetime and advanced the controller fixture clock to exercise shutdown. They did not wait 15 wall-clock minutes per case.

The final recovery gate observes the pinned application through clock synchronization and checks exactly one supervisor and one restart, retained data/runtime/revision, and unchanged non-preview operation/budget records. Earlier measurements and the duplicate-supervisor failure are preserved alongside the corrected results. Plain-HTTP canary noVNC diagnostics are recorded; production uses HTTPS. These infrastructure results do not qualify arbitrary natural-language UI requirements or change previous Daedalus evaluation scores.

Production retained 113 conversations, 333 messages, 67 artifacts, 167 coding-project records and all 41 workflow records. Accepted revision pointers and model settings matched their pre-cutover hashes. Discarded Codebox workspaces are explicitly unavailable; retained artifacts remain downloadable. A stale, inactive July workflow was cancelled during the drain.

Private evidence lives on pve2 under `/var/lib/hephaestus-installer/migration-20261103/`; historical Codebox evidence is separately retained under `/var/lib/hephaestus-installer/evidence/`. The migration record binds each gate to its source reports and hashes. Local detailed receipts are in the ignored `agent-output/hephaestus-migration/` directory.

Maintenance uses pve2's installer SSH key, not Codebox's old password:

```sh
ssh pve2 'ssh -i /var/lib/hephaestus-installer/id_ed25519 root@fe80::be24:11ff:fe88:a52%vmbr0'
```

The host-specific link-local address is derived from VM118's recorded MAC; another installation has its own address. `deploy_monitor.py` retains frontend/backend deployment and rejects mutable worker copies. For worker changes, drain operations, stop pinned applications, copy the complete source checkout, run the immutable installer, verify health and restart the chosen applications. Keep runtime image/revision identity with each pinned session.

Retirement was verified in Proxmox: CT115 is absent, `vm-115-disk-0` is absent, and VM118 remains running. The new data disk had approximately 44.5 GiB free at completion, above its 10 GiB reserve. Disposable test applications were stopped with their data/evidence retained.
