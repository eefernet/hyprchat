# Daedalus deployment — 2026-09-10

Deployed after explicit user approval, at 2026-09-11 05:34 UTC (September 10 in America/Phoenix).

- HyprChat: 154 application, frontend, and documentation files deployed; service restarted and active.
- Codebox: 8 worker files deployed; `openhands-worker` restarted and active. Existing Aider installation remains available.
- `daedalus_v3_enabled` is explicitly `false` in production Settings.
- Existing settings were preserved: global context 32,768; Daedalus context 34,816. The only saved-settings change was the disabled feature flag.
- Deployed file hashes matched the staged bundle. Startup logs contained no detected import, permission, database, or startup failures. The additive workflow migration is present.
- Chromium verified the production Settings page, the disabled checkbox, and context persistence after reload, with no page errors, console errors, or failed requests. The check did not change context settings.
- Health checks passed for HyprChat, storage, Ollama, Codebox, N8N, ComfyUI, STT, and TTS. SearXNG reported degraded search health before and after deployment.

The experimental workflow has not met its model-quality promotion requirements. See the [evaluation](evaluations/daedalus-2026-09-10.md). The earlier evaluation's deployment statements describe its state before this approved deployment.

## Rollback material

Protected backups are retained on each server:

- HyprChat: `/opt/hyprchat/.deployments/daedalus-20260911T053406Z`
- Codebox: `/opt/openhands-worker/.deployments/daedalus-20260911T053406Z`

Each directory contains `manifest.json`, `originals.json`, the previous files under `backup/`, and an activation record. HyprChat also has the prior frontend directory, settings, and a consistent SQLite backup taken while the service was stopped. Settings and database backups are private files and must not be committed.

For code rollback, stop the affected service, restore existing files from `backup/` with their recorded ownership/modes, remove only files recorded as newly added in `originals.json`, and start the service again. The migration is additive; restoring the database is not necessary for an ordinary code rollback. Restoring an older database would discard later writes and requires a separate recovery decision.

The [deployment evidence](evaluations/daedalus-2026-09-10-deployment.json) records file hashes, service verification, and browser results without credentials.
