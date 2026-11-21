#!/bin/sh
# Import + publish the PeCal Pulse workflows into an n8n instance.
# Runs inside the n8n container (compose "n8n-init") or locally with N8N_CMD set.
set -eu
N8N_CMD=${N8N_CMD:-n8n}
WORKFLOW_DIR=${WORKFLOW_DIR:-/workflows}
echo "[n8n-setup] importing workflows from $WORKFLOW_DIR"
$N8N_CMD import:workflow --separate --input="$WORKFLOW_DIR"
for id in PulseErrorHdl001 PulseExecute0001 PulseRiskScan001; do
  $N8N_CMD publish:workflow --id="$id"
done
echo "[n8n-setup] workflows imported and published"
