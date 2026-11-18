#!/bin/bash
# Acceptance check for scripts/staged_release.py activate:
#   python3 scripts/staged_release.py activate --release <path> \
#       --acceptance bash /path/to/checkout/scripts/release_acceptance.sh
# staged_release.py sets HYPRCHAT_URL. A failure here rolls the release back.
set -euo pipefail
: "${HYPRCHAT_URL:?HYPRCHAT_URL is not set}"
curl -fsS --max-time 15 "$HYPRCHAT_URL/api/health/live" > /dev/null
echo "HyprChat answers at $HYPRCHAT_URL"
