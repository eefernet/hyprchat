#!/usr/bin/env bash
# Run on Codebox after copying backend/document*.py and document-requirements.txt
# into /opt/hyprchat-documents/. No changes to Ollama or the coding worker venv.
set -euo pipefail
apt-get update -qq
apt-get install -y python3-venv bubblewrap libreoffice-writer libreoffice-calc libreoffice-impress fonts-liberation fonts-dejavu-core
install -d -m 700 /root/hyprchat-documents/jobs
python3 -m venv /opt/hyprchat-documents/venv
/opt/hyprchat-documents/venv/bin/pip install -r /opt/hyprchat-documents/document-requirements.txt
/opt/hyprchat-documents/venv/bin/python /opt/hyprchat-documents/document_worker.py health
# Remove private worker staging after one day, including failed jobs/logs. Backend
# artifact copies and originals follow the existing HyprChat retention policy.
cat > /etc/tmpfiles.d/hyprchat-documents.conf <<'EOF'
d /root/hyprchat-documents/jobs 0700 root root 1d -
EOF
