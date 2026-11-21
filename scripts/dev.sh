#!/usr/bin/env bash
# One-command local development: env check, demo data, n8n, backend, frontend.
#
#   bash scripts/dev.sh            # backend + frontend (+ n8n if available)
#   PULSE_N8N=off bash scripts/dev.sh
#
# n8n is started with Docker (preferred) or, without Docker, via npx when
# Node >= 24 is installed. Without either, Pulse runs and reports n8n offline.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_dir"
log() { printf '\033[1;33m[dev]\033[0m %s\n' "$*"; }

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }
command -v pnpm >/dev/null || { echo "pnpm is required (corepack enable)"; exit 1; }

python3 scripts/setup_env.py
set -a; . ./.env; set +a

for port in 8001 3000; do
  if (exec 3<>/dev/tcp/127.0.0.1/$port) 2>/dev/null; then
    echo "Port $port is already in use — stop the other process first."; exit 1
  fi
done

[ -d .venv ] || uv sync
[ -d frontend/node_modules ] || (cd frontend && pnpm install --frozen-lockfile)
uv run python -m backend.app.pulse.demo_data --with-analytics

pids=()
cleanup() {
  log "stopping…"
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  if [ "${n8n_mode:-}" = docker ]; then docker compose stop n8n >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

n8n_mode=off
start_n8n_npx() {
  export N8N_USER_FOLDER="$project_dir/data/runtime/n8n" N8N_PORT=5678 N8N_LISTEN_ADDRESS=127.0.0.1 \
    N8N_BLOCK_ENV_ACCESS_IN_NODE=false NODE_FUNCTION_ALLOW_BUILTIN=crypto N8N_DIAGNOSTICS_ENABLED=false \
    N8N_SECURE_COOKIE=false PULSE_BACKEND_URL=http://127.0.0.1:8001
  mkdir -p "$N8N_USER_FOLDER"
  log "importing n8n workflows (npx n8n@2.48.7 — first run downloads n8n)"
  N8N_CMD="npx --yes n8n@2.48.7" WORKFLOW_DIR="$project_dir/n8n/workflows" sh scripts/n8n-setup.sh >/dev/null
  log "starting n8n via npx (http://127.0.0.1:5678, log: data/runtime/n8n.log)"
  npx --yes n8n@2.48.7 start > data/runtime/n8n.log 2>&1 & pids+=($!)
  n8n_mode=npx
}
node_ok() { command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 24 ]; }
if [ "${PULSE_N8N:-auto}" != off ]; then
  if command -v docker >/dev/null && docker info >/dev/null 2>&1 && \
     PULSE_BACKEND_URL_COMPOSE=${PULSE_BACKEND_URL:-http://host.docker.internal:8001} docker compose up -d n8n; then
    n8n_mode=docker
    log "n8n running in Docker (http://127.0.0.1:5678)"
  elif node_ok; then
    start_n8n_npx
  else
    log "n8n not started: install Docker or Node >= 24. Pulse will show n8n offline and record dispatch failures."
  fi
fi

log "starting backend on http://127.0.0.1:8001"
uv run uvicorn backend.app.main:app --host "${PULSE_API_HOST:-127.0.0.1}" --port 8001 --reload --reload-dir backend & pids+=($!)
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:8001/api/health >/dev/null 2>&1 && break; sleep 1; done
curl -fs http://127.0.0.1:8001/api/pulse/status >/dev/null && log "backend healthy" || log "backend not healthy yet — see output above"

log "starting frontend on http://127.0.0.1:3000  (Pulse Ops: http://127.0.0.1:3000/pulse)"
(cd frontend && pnpm dev) & pids+=($!)
wait
