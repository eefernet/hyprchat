#!/usr/bin/env bash
# Documents use the same immutable Hephaestus release and isolated runtime.
set -euo pipefail
SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ! -f /etc/hephaestus/current-release ]]; then
  echo "Provision Hephaestus with scripts/create-hephaestus-vm.sh before installing document support." >&2
  exit 1
fi
exec python3 "$SOURCE_ROOT/scripts/hephaestus/install.py" --source "$SOURCE_ROOT"
