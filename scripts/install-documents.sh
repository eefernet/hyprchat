#!/usr/bin/env bash
# Install document support (Word, PowerPoint, Excel). Run this ON the Hephaestus
# VM, as root, from a copy of this repository. Documents use the same immutable
# Hephaestus release and isolated runtime; extra arguments go to
# scripts/hephaestus/install.py (for example --ollama-url http://<ollama>:11434).
set -euo pipefail
SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ! -f /etc/hephaestus/current-release ]]; then
  echo "Provision Hephaestus with scripts/create-hephaestus-vm.sh before installing document support." >&2
  exit 1
fi
exec python3 "$SOURCE_ROOT/scripts/hephaestus/install.py" --source "$SOURCE_ROOT" "$@"
