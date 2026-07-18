$ErrorActionPreference = "SilentlyContinue"

$portOpen = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 18790 -State Listen -ErrorAction SilentlyContinue
if ($portOpen) {
    exit 0
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
# Preserve the established log location during the compatibility window.
$logDir = Join-Path $repo ".tmp\remembrance-flow"
$stdout = Join-Path $logDir "service.out.log"
$stderr = Join-Path $logDir "service.err.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force $logDir | Out-Null
}

Start-Process -FilePath $python -ArgumentList @("-m", "recall_mcp.serve", "--host", "127.0.0.1", "--port", "18790", "--no-nats") -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr