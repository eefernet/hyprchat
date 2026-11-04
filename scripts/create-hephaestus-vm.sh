#!/usr/bin/env bash
# Run on the Proxmox host. This never changes or deletes an existing Codebox.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/hephaestus/provision.py" "$@"
