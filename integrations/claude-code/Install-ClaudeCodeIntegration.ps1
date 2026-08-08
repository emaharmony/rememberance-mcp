<#
.SYNOPSIS
    Deprecated: delegates to `recall-admin install-hooks --agent claude-code`.

.DESCRIPTION
    This script used to contain the full migration/install logic for wiring
    Claude Code up to Recall. That logic now lives in the cross-platform
    `recall-admin install-hooks` command (src\recall_mcp\install_hooks.py),
    which also works from a wheel-only install with no source checkout --
    something this PowerShell-only script could never do. This file is now a
    thin wrapper kept only so existing muscle memory and any external
    automation that still calls it keep working, following the same shim
    convention already used elsewhere in this repo (see
    integrations\windows\start_remembrance_rest.ps1 and
    integrations\claude-code\remembrance_mcp_stdio.cmd).

    Parameter mapping:
      -DryRun          -> --dry-run
      -Force           -> --force
      -SkipMcp         -> --skip-mcp
      -SkipHooks       -> --skip-hooks
      -Scope           -> --scope
      -ClaudeJsonPath  -> --config-path
      -ClaudeSettingsPath -> --settings-path

    -RepoRoot is accepted for backward compatibility but no longer has any
    effect: `recall-admin install-hooks` locates the packaged hook scripts
    itself via importlib.resources, regardless of where (or whether) a
    source checkout exists.

.PARAMETER RepoRoot
    Accepted for backward compatibility. No longer used.

.PARAMETER ClaudeJsonPath
    Forwarded as --config-path.

.PARAMETER ClaudeSettingsPath
    Forwarded as --settings-path.

.PARAMETER Scope
    Forwarded as --scope (user, local, or project).

.PARAMETER SkipMcp
    Forwarded as --skip-mcp.

.PARAMETER SkipHooks
    Forwarded as --skip-hooks.

.PARAMETER Force
    Forwarded as --force.

.PARAMETER DryRun
    Forwarded as --dry-run.

.EXAMPLE
    .\Install-ClaudeCodeIntegration.ps1 -DryRun

.EXAMPLE
    .\Install-ClaudeCodeIntegration.ps1 -Scope user
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoRoot,
    [string]$ClaudeJsonPath,
    [string]$ClaudeSettingsPath,
    [ValidateSet("user", "local", "project")][string]$Scope = "user",
    [switch]$SkipMcp,
    [switch]$SkipHooks,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# A native -WhatIf should behave exactly like -DryRun: plan-only, no writes.
if ($WhatIfPreference) {
    $DryRun = $true
}

Write-Warning "Install-ClaudeCodeIntegration.ps1 is deprecated; use 'recall-admin install-hooks --agent claude-code' directly. Delegating..."

if ($RepoRoot) {
    Write-Warning "-RepoRoot is accepted for backward compatibility but no longer has any effect; recall-admin locates its packaged assets on its own."
}

# Prefer a `recall-admin` console script already on PATH -- the normal case
# for a `pip install recall-mcp` install. Fall back to this checkout's own
# venv Python invoking the admin module directly, which is what a source
# checkout without an activated venv (or a fresh clone that has never put
# its Scripts directory on PATH) needs.
$recallAdminCmd = Get-Command recall-admin -ErrorAction SilentlyContinue
if ($recallAdminCmd) {
    $exe = $recallAdminCmd.Source
    $baseArgs = @("install-hooks")
} else {
    $repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $exe = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "Neither 'recall-admin' nor '$exe' was found. Install recall-mcp (pip install recall-mcp) or create the checkout's venv (python -m venv .venv)."
    }
    $baseArgs = @("-m", "recall_mcp.admin", "install-hooks")
}

$cliArgs = $baseArgs + @("--agent", "claude-code", "--scope", $Scope)
if ($DryRun) { $cliArgs += "--dry-run" }
if ($Force) { $cliArgs += "--force" }
if ($SkipMcp) { $cliArgs += "--skip-mcp" }
if ($SkipHooks) { $cliArgs += "--skip-hooks" }
if ($ClaudeJsonPath) { $cliArgs += @("--config-path", $ClaudeJsonPath) }
if ($ClaudeSettingsPath) { $cliArgs += @("--settings-path", $ClaudeSettingsPath) }

& $exe @cliArgs
exit $LASTEXITCODE
