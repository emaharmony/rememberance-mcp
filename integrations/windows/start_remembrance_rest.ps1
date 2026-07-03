$ErrorActionPreference = "SilentlyContinue"

$portOpen = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 18790 -State Listen -ErrorAction SilentlyContinue
if ($portOpen) {
    exit 0
}

$repo = "D:\_projects_\remembrance-mcp"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$logDir = Join-Path $repo ".tmp\remembrance-flow"
$stdout = Join-Path $logDir "service.out.log"
$stderr = Join-Path $logDir "service.err.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force $logDir | Out-Null
}

Start-Process `
    -FilePath $python `
    -ArgumentList @("-m", "remembrance_mcp.server.serve", "--host", "127.0.0.1", "--port", "18790", "--no-nats") `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
