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
export HYPRCHAT_URL="${HYPRCHAT_URL:-http://192.168.1.120:8000}"

echo "============================================"
echo "  HyprChat Test Suite"
echo "  Server: $HYPRCHAT_URL"
echo "============================================"
echo ""

# Check server is up
if ! curl -sf "$HYPRCHAT_URL/api/health" > /dev/null 2>&1; then
    echo "ERROR: Server at $HYPRCHAT_URL is not responding"
    echo "Make sure hyprchat is running before running tests."
    exit 1
fi
echo "Server is up."
echo ""

# Install test deps if needed
pip3 install pytest httpx --quiet 2>/dev/null

# Run tests
cd "$BACKEND_DIR"
python3 -m pytest tests/ -v --tb=short -x "$@"
