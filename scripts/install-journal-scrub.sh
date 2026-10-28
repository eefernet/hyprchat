#!/bin/bash
# Retire the old journal scrub helper. Run on the HyprChat host as root.
# Image deletion no longer removes unrelated system logs.
set -euo pipefail
systemctl disable --now hyprchat-journal-scrub.path 2>/dev/null || true
systemctl stop hyprchat-journal-scrub.service 2>/dev/null || true
rm -f /etc/systemd/system/hyprchat-journal-scrub.path /etc/systemd/system/hyprchat-journal-scrub.service
rm -f /opt/hyprchat/data/.journal-scrub-request
systemctl daemon-reload
echo "Retired HyprChat journal scrub helper; system journals were preserved."
