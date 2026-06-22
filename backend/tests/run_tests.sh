#!/bin/bash
# HyprChat Test Runner
# Usage:
#   ./run_tests.sh                    # Run all tests
#   ./run_tests.sh -k "health"        # Run only health tests
#   ./run_tests.sh --tb=long          # Show full tracebacks

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"

# Default server URL (override with HYPRCHAT_URL env var)
export HYPRCHAT_URL="${HYPRCHAT_URL:-http://127.0.0.1:8000}"

echo "============================================"
echo "  HyprChat Test Suite"
echo "  Server: $HYPRCHAT_URL"
echo "============================================"
echo ""

# Check server is up. Offline suites (most of them) don't need it, so warn
# instead of aborting — only the live test_01..test_12 suites require it.
if ! curl -sf "$HYPRCHAT_URL/api/health" > /dev/null 2>&1; then
    echo "WARNING: Server at $HYPRCHAT_URL is not responding."
    echo "Offline suites will still run; live test_01..test_12 will fail/skip."
else
    echo "Server is up."
fi
echo ""

# Install test deps if needed
pip3 install pytest httpx --quiet 2>/dev/null

# Run tests. No -x: one failing suite must not halt the rest.
cd "$BACKEND_DIR"
python3 -m pytest tests/ -v --tb=short "$@"
