# SignalPilot one-click start for Windows (launched by start-signalpilot.cmd).
# Installs missing tools (uv, Node.js LTS) with winget, prepares .env + data,
# then starts backend, frontend and n8n in their own windows and opens the app.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:USERPROFILE\.local\bin"
}

Step "Checking tools"
Refresh-Path
if (-not (Have "uv")) {
  Write-Host "Installing uv (Python package manager)..."
  powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
  Refresh-Path
}
if (-not (Have "node")) {
  Write-Host "Installing Node.js LTS via winget..."
  winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements --silent
  Refresh-Path
}
if (-not (Have "uv") -or -not (Have "node")) { throw "uv or Node.js is still missing. Open a new terminal and run start-signalpilot.cmd again." }
Write-Host ("uv " + (uv --version) + " | node " + (node --version))

Step "Preparing environment (.env with generated secrets)"
uv run --python 3.13 python scripts/setup_env.py

# Load .env into this process so child windows inherit it.
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim().Trim("'").Trim('"'), "Process")
}

Step "Installing backend dependencies (uv sync)"
uv sync --python 3.13
Step "Installing frontend dependencies (pnpm)"
Push-Location frontend
npx --yes pnpm@11.21.0 install --frozen-lockfile
Pop-Location
Step "Generating the synthetic demo dataset"
uv run python -m backend.app.pulse.demo_data --with-analytics

Step "Preparing n8n workflows"
# Fill the secret + backend URL into the workflow templates (output is git-ignored).
$render = Join-Path $root "data\runtime\n8n-render"
New-Item -ItemType Directory -Force $render | Out-Null
$backendForN8n = "http://127.0.0.1:8001"
$nodeMajor = [int]((node --version).TrimStart('v').Split('.')[0])
$dockerOk = $false
if (Have "docker") { docker info *> $null; $dockerOk = ($LASTEXITCODE -eq 0) }
if ($dockerOk) { $backendForN8n = "http://host.docker.internal:8001" }
Get-ChildItem (Join-Path $root "n8n\workflows\*.json") | ForEach-Object {
  $text = [IO.File]::ReadAllText($_.FullName).Replace("__PULSE_WEBHOOK_SECRET__", $env:PULSE_WEBHOOK_SECRET).Replace("__PULSE_BACKEND_URL__", $backendForN8n)
  [IO.File]::WriteAllText((Join-Path $render $_.Name), $text, (New-Object System.Text.UTF8Encoding($false)))
}
$n8nLog = Join-Path $root "data\runtime\n8n-setup.log"
$n8nRunning = $false
try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5678/healthz -TimeoutSec 3 | Out-Null; $n8nRunning = $true } catch {}

if ($dockerOk -and -not $n8nRunning) {
  $env:PULSE_BACKEND_URL_COMPOSE = $backendForN8n
  docker compose up -d n8n
  Write-Host "n8n running in Docker (workflows imported by n8n-init)."
} else {
  $nodePkg = if ($nodeMajor -ge 24) { "" } else { "-p node@24 " }
  $n8n = "npx --yes $nodePkg-p n8n@2.48.7 n8n"
  Write-Host "Importing SignalPilot workflows into n8n (first run downloads n8n; log: data\runtime\n8n-setup.log)..."
  $import = "$n8n import:workflow --separate --input='$render'; foreach (`$id in 'PulseErrorHdl001','PulseExecute0001','PulseRiskScan001') { $n8n publish:workflow --id=`$id }"
  powershell -NoProfile -ExecutionPolicy Bypass -Command $import *> $n8nLog
  if ($LASTEXITCODE -ne 0) { Write-Host "n8n workflow import reported a problem - see data\runtime\n8n-setup.log" -ForegroundColor Yellow }
  if ($n8nRunning) {
    Write-Host "n8n is already running on port 5678 - workflows were updated in place."
  } else {
    Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
      "`$Host.UI.RawUI.WindowTitle='SignalPilot n8n'; `$env:N8N_PORT='5678'; `$env:N8N_DIAGNOSTICS_ENABLED='false'; `$env:N8N_SECURE_COOKIE='false'; $n8n start"
    Write-Host "n8n starting in its own window (http://127.0.0.1:5678)."
  }
}

Step "Starting backend (http://127.0.0.1:8001)"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
  "`$Host.UI.RawUI.WindowTitle='SignalPilot backend'; Set-Location '$root'; uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8001 --reload --reload-dir backend"
Step "Starting frontend (http://127.0.0.1:3000)"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
  "`$Host.UI.RawUI.WindowTitle='SignalPilot frontend'; Set-Location '$root\frontend'; npx --yes pnpm@11.21.0 dev"

Step "Waiting for the app"
for ($i = 0; $i -lt 90; $i++) {
  try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000 -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep 2 }
}
Start-Process "http://127.0.0.1:3000/pulse"
Write-Host "`nSignalPilot is running:" -ForegroundColor Green
Write-Host "  App        http://127.0.0.1:3000   (Agent Ops: /pulse)"
Write-Host "  Backend    http://127.0.0.1:8001/docs"
Write-Host "  n8n        http://127.0.0.1:5678"
Write-Host "Close the three SignalPilot windows to stop."
