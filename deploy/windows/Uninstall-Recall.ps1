#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot = "$env:ProgramData\Recall",
    [switch]$RemoveData
)

$root = [System.IO.Path]::GetFullPath($InstallRoot)
foreach ($service in @("Recall", "RecallOllama", "nats-server")) {
    Stop-Service -Name $service -Force -ErrorAction SilentlyContinue
}
if (Test-Path "$root\services\recall\RecallService.exe") {
    & "$root\services\recall\RecallService.exe" uninstall
}
if (Test-Path "$root\services\ollama\RecallOllama.exe") {
    & "$root\services\ollama\RecallOllama.exe" uninstall
}
Get-NetFirewallRule -DisplayName "Recall block remote *" -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

if ($RemoveData -and $PSCmdlet.ShouldProcess($root, "Remove Recall data")) {
    $programData = [System.IO.Path]::GetFullPath($env:ProgramData)
    if (-not $root.StartsWith($programData, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside ProgramData."
    }
    Remove-Item -LiteralPath $root -Recurse -Force
}
