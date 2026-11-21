#!/bin/sh
# Import + publish the SignalPilot workflows into an n8n instance.
# The committed workflow files hold placeholders; the webhook secret and backend
# URL are filled in here at import time and never written to the repository.
# Runs inside the n8n container (compose "n8n-init") or locally with N8N_CMD set.
set -eu
N8N_CMD=${N8N_CMD:-n8n}
WORKFLOW_DIR=${WORKFLOW_DIR:-/workflows}
: "${PULSE_WEBHOOK_SECRET:?PULSE_WEBHOOK_SECRET must be set}"
BACKEND=${PULSE_BACKEND_URL:-http://host.docker.internal:8001}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for f in "$WORKFLOW_DIR"/*.json; do
  sed -e "s|__PULSE_WEBHOOK_SECRET__|$PULSE_WEBHOOK_SECRET|g" -e "s|__PULSE_BACKEND_URL__|$BACKEND|g" "$f" > "$TMP/$(basename "$f")"
done
echo "[n8n-setup] importing workflows (backend: $BACKEND)"
$N8N_CMD import:workflow --separate --input="$TMP"
for id in PulseErrorHdl001 PulseExecute0001 PulseRiskScan001; do
  $N8N_CMD publish:workflow --id="$id"
done
echo "[n8n-setup] workflows imported and published"
