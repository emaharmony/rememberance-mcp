# Windows deployment

The supported native reference is Windows Server 2022/2025 or Windows 11 with Python 3.12, Ollama, NATS Server, WinSW, and Tailscale installed.

## Install

Download the wheel, its matching `.sigstore.json` bundle, and `SHA256SUMS`
from the GitHub release. Verify the keyless release signature before running
any project code:

```powershell
$Version = "v2.1.0"
$Wheel = "C:\Install\recall_mcp-2.1.0-py3-none-any.whl"
cosign verify-blob $Wheel `
  --bundle "$Wheel.sigstore.json" `
  --certificate-identity "https://github.com/emaharmony/remembrance/.github/workflows/release.yml@refs/tags/$Version" `
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
Get-FileHash -Algorithm SHA256 $Wheel
```

Compare the displayed digest to the wheel entry in `SHA256SUMS`.

Open an elevated PowerShell session and run:

```powershell
.\deploy\windows\Install-Recall.ps1 `
  -WheelPath C:\Install\recall_mcp-2.1.0-py3-none-any.whl `
  -WheelSha256 RELEASE_SHA256 `
  -PythonExe C:\Python312\python.exe `
  -WinSWExe C:\Install\WinSW-x64.exe `
  -NatsServerExe C:\Install\nats-server.exe
```

The installer verifies the wheel SHA-256 before execution, requires Python 3.12,
creates `%ProgramData%\Recall`, locks its ACL to Administrators, SYSTEM, and
LocalService, preserves existing secrets on repeat runs, installs a virtual
environment, registers Recall and Ollama through WinSW, installs native NATS,
blocks remote access to service ports, pulls both models, and runs doctor.

The Prism publisher password is stored in
`%ProgramData%\Recall\secrets\prism-nats-password`. Treat it as a secret.
Recall's API token is never printed.

Publish Recall privately with Tailscale Serve:

```powershell
tailscale serve --bg http://127.0.0.1:8788
tailscale serve status
```

## Operations

```powershell
& "$env:ProgramData\Recall\venv\Scripts\recall-admin.exe" doctor
& "$env:ProgramData\Recall\venv\Scripts\recall-admin.exe" backup
Get-Service Recall, RecallOllama, nats-server
```

## Upgrade and rollback

Before upgrades, stop Recall, create a backup, install the new wheel in the
existing virtual environment, run `recall-admin migrate`, and restart. Retain
the previous signed wheel and database backup for rollback. A schema rollback
must restore the matching pre-upgrade database backup.

## Troubleshooting

```powershell
Get-Service Recall, RecallOllama, nats-server
Get-Content "$env:ProgramData\Recall\logs\*.out.log" -Tail 200
Invoke-RestMethod http://127.0.0.1:8788/health/live
& "$env:ProgramData\Recall\venv\Scripts\recall-admin.exe" doctor
& "$env:ProgramData\Recall\venv\Scripts\recall-admin.exe" integrity-check
Get-NetFirewallRule -DisplayName "Recall*"
```

If a service will not start, inspect the matching WinSW log and verify that
LocalService retains read/write access to `%ProgramData%\Recall`. If model
pulls fail, start `RecallOllama`, confirm port 11434 is listening only on
loopback, and rerun both `ollama pull` commands. Re-run the installer to repair
service definitions and ACLs; existing secrets are preserved.

## Removal

Run `Uninstall-Recall.ps1` to remove services and firewall rules while
retaining data. Add `-RemoveData` only after verifying an off-host backup; the
script requires confirmation and refuses paths outside ProgramData.
