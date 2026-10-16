#!/bin/bash
# Run with the repository's Python 3.12 environment, never the system pip.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
TEST_PYTHON="${HYPRCHAT_TEST_PYTHON:-$REPO_DIR/.venv/bin/python}"
if [[ ! -x "$TEST_PYTHON" ]]; then
    echo "Create the test environment first: uv venv --python 3.12 .venv"
    echo "Then: uv pip sync --python .venv/bin/python --require-hashes backend/test-requirements.lock.txt"
    exit 1
fi
"$TEST_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else "Backend tests require Python 3.12")'
export HYPRCHAT_URL="${HYPRCHAT_URL:-http://127.0.0.1:8000}"
cd "$REPO_DIR"
exec "$TEST_PYTHON" -m pytest backend/tests/ -v --tb=short "$@"
