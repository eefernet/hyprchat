#!/bin/bash
# Installs the HyprChat journal-scrub helper on the HyprChat server.
#
# The hyprchat service runs unprivileged (User=hyprchat, NoNewPrivileges), so
# the Image Studio "Delete all" purge cannot clear journald itself. This
# installs a root oneshot service + path unit: when the purge endpoint writes
# /opt/hyprchat/data/.journal-scrub-request, the helper rotates and vacuums
# the journal (removing log lines that contained generation prompts) and
# deletes the trigger. Run as root on the HyprChat host:
#
#   bash scripts/install-journal-scrub.sh
set -euo pipefail

TRIGGER=/opt/hyprchat/data/.journal-scrub-request

cat > /etc/systemd/system/hyprchat-journal-scrub.service <<'EOF'
[Unit]
Description=HyprChat journal scrub (triggered by image purge)

[Service]
Type=oneshot
ExecStart=/usr/bin/journalctl --rotate
ExecStart=/bin/sleep 2
ExecStart=/usr/bin/journalctl --vacuum-size=1
ExecStartPost=/bin/rm -f /opt/hyprchat/data/.journal-scrub-request
EOF

cat > /etc/systemd/system/hyprchat-journal-scrub.path <<EOF
[Unit]
Description=Watch for HyprChat journal scrub requests

[Path]
PathExists=$TRIGGER

[Install]
WantedBy=multi-user.target
EOF

rm -f "$TRIGGER"
systemctl daemon-reload
systemctl enable --now hyprchat-journal-scrub.path
echo "Installed. The image purge can now clear journald."
