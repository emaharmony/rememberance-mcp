#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Fa-f0-9]{64}$")]
    [string]$WheelSha256,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$WinSWExe,
    [Parameter(Mandatory = $true)]
    [string]$NatsServerExe,
    [string]$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    [string]$InstallRoot = "$env:ProgramData\Recall"
)

$ErrorActionPreference = "Stop"

$resolvedWheel = (Resolve-Path -LiteralPath $WheelPath).Path
$actualWheelSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedWheel).Hash
if (-not $actualWheelSha256.Equals($WheelSha256, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Wheel SHA-256 verification failed."
}
foreach ($requiredExecutable in @($PythonExe, $WinSWExe, $NatsServerExe, $OllamaExe)) {
    if (-not (Test-Path -LiteralPath $requiredExecutable -PathType Leaf)) {
        throw "Required executable not found: $requiredExecutable"
    }
}
$pythonVersion = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion.Trim() -ne "3.12") {
    throw "The production installer requires Python 3.12."
}

$resolvedRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$programDataRoot = [System.IO.Path]::GetFullPath($env:ProgramData)
if (-not $resolvedRoot.StartsWith($programDataRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallRoot must remain under ProgramData."
}

$directories = @(
    $resolvedRoot,
    "$resolvedRoot\config",
    "$resolvedRoot\secrets",
    "$resolvedRoot\data",
    "$resolvedRoot\models",
    "$resolvedRoot\nats",
    "$resolvedRoot\backups",
    "$resolvedRoot\logs",
    "$resolvedRoot\services\recall",
    "$resolvedRoot\services\ollama"
)
foreach ($directory in $directories) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

function New-RandomSecret([int]$Bytes = 48) {
    $buffer = New-Object byte[] $Bytes
    [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return [Convert]::ToBase64String($buffer)
}

function Get-OrCreateSecret([string]$Path, [int]$Bytes = 48) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-Content -LiteralPath $Path -Raw).Trim()
    }
    $value = New-RandomSecret $Bytes
    Set-Content -LiteralPath $Path -Value $value -NoNewline
    return $value
}

$apiToken = Get-OrCreateSecret "$resolvedRoot\secrets\api-token"
$natsRecallPassword = (Get-OrCreateSecret "$resolvedRoot\secrets\recall-nats-password" 32).Replace("/", "a").Replace("+", "b")
$natsPrismPassword = (Get-OrCreateSecret "$resolvedRoot\secrets\prism-nats-password" 32).Replace("/", "c").Replace("+", "d")

$natsConfig = @"
server_name: recall-windows
listen: 127.0.0.1:4222
http: 127.0.0.1:8222
jetstream {
  store_dir: "$($resolvedRoot.Replace('\', '/'))/nats/jetstream"
  max_mem_store: 1GB
  max_file_store: 20GB
}
authorization {
  users: [
    {
      user: recall
      password: "$natsRecallPassword"
      permissions: {
        publish: ["__DOLLAR__JS.API.>", "__DOLLAR__JS.ACK.>", "recall.agent.output.dlq"]
        subscribe: ["*.agent.output", "_INBOX.>"]
      }
    }
    {
      user: prism
      password: "$natsPrismPassword"
      permissions: {
        publish: ["*.agent.output"]
        subscribe: ["_INBOX.>"]
      }
    }
  ]
}
"@
$natsConfig = $natsConfig.Replace("__DOLLAR__", [char]36)
Set-Content -LiteralPath "$resolvedRoot\config\nats.conf" -Value $natsConfig -Encoding UTF8

$existingServices = @(
    @{ Directory = "$resolvedRoot\services\recall"; Executable = "RecallService.exe" },
    @{ Directory = "$resolvedRoot\services\ollama"; Executable = "RecallOllama.exe" }
)
foreach ($service in $existingServices) {
    $servicePath = Join-Path $service.Directory $service.Executable
    if (Test-Path -LiteralPath $servicePath -PathType Leaf) {
        Push-Location $service.Directory
        try {
            & ".\$($service.Executable)" stop 2>$null
            & ".\$($service.Executable)" uninstall 2>$null
        }
        finally {
            Pop-Location
        }
    }
}

$venv = "$resolvedRoot\venv"
& $PythonExe -m venv $venv
& "$venv\Scripts\python.exe" -m pip install --upgrade pip "setuptools>=83" wheel
& "$venv\Scripts\python.exe" -m pip install "$resolvedWheel[all]"

Copy-Item -LiteralPath $WinSWExe -Destination "$resolvedRoot\services\recall\RecallService.exe" -Force
Copy-Item -LiteralPath $WinSWExe -Destination "$resolvedRoot\services\ollama\RecallOllama.exe" -Force

$recallXml = @"
<service>
  <id>Recall</id>
  <name>Recall Memory Service</name>
  <description>Private Recall 2.1 memory service</description>
  <executable>$venv\Scripts\recall-service.exe</executable>
  <workingdirectory>$resolvedRoot</workingdirectory>
  <env name="RECALL_HOME" value="$resolvedRoot\data"/>
  <env name="RECALL_HOST" value="127.0.0.1"/>
  <env name="RECALL_PORT" value="8788"/>
  <env name="RECALL_API_TOKEN_FILE" value="$resolvedRoot\secrets\api-token"/>
  <env name="RECALL_OLLAMA_URL" value="http://127.0.0.1:11434"/>
  <env name="RECALL_EMBEDDINGS_ENABLED" value="true"/>
  <env name="RECALL_NATS_URL" value="nats://recall:$natsRecallPassword@127.0.0.1:4222"/>
  <logpath>$resolvedRoot\logs</logpath>
  <log mode="roll-by-size-time"/>
  <stoptimeout>15 sec</stoptimeout>
  <onfailure action="restart" delay="10 sec"/>
  <serviceaccount>
    <username>NT AUTHORITY\LocalService</username>
  </serviceaccount>
</service>
"@
Set-Content -LiteralPath "$resolvedRoot\services\recall\RecallService.xml" -Value $recallXml -Encoding UTF8

$ollamaXml = @"
<service>
  <id>RecallOllama</id>
  <name>Recall Ollama</name>
  <description>Local Ollama service for Recall</description>
  <executable>$OllamaExe</executable>
  <arguments>serve</arguments>
  <env name="OLLAMA_HOST" value="127.0.0.1:11434"/>
  <env name="OLLAMA_MODELS" value="$resolvedRoot\models"/>
  <logpath>$resolvedRoot\logs</logpath>
  <onfailure action="restart" delay="10 sec"/>
  <serviceaccount>
    <username>NT AUTHORITY\LocalService</username>
  </serviceaccount>
</service>
"@
Set-Content -LiteralPath "$resolvedRoot\services\ollama\RecallOllama.xml" -Value $ollamaXml -Encoding UTF8

& icacls $resolvedRoot /inheritance:r | Out-Null
& icacls $resolvedRoot /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "LOCAL SERVICE:(OI)(CI)M" | Out-Null

& $NatsServerExe -sl stop 2>$null
& $NatsServerExe -sl remove 2>$null
& $NatsServerExe -sl install -c "$resolvedRoot\config\nats.conf"

Push-Location "$resolvedRoot\services\ollama"
try { & ".\RecallOllama.exe" install } finally { Pop-Location }
Push-Location "$resolvedRoot\services\recall"
try { & ".\RecallService.exe" install } finally { Pop-Location }

$firewallRules = @(
    @{ Name = "Recall block remote API"; Ports = "8788" },
    @{ Name = "Recall block remote Ollama"; Ports = "11434" },
    @{ Name = "Recall block remote NATS"; Ports = "4222,8222" }
)
foreach ($rule in $firewallRules) {
    if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Block -Protocol TCP -LocalPort $rule.Ports -RemoteAddress Any | Out-Null
    }
}

Start-Service nats-server
Start-Service RecallOllama
Start-Sleep -Seconds 3
& $OllamaExe pull embeddinggemma
& $OllamaExe pull nemotron-3-nano:4b
Start-Service Recall

& "$venv\Scripts\recall-admin.exe" --token-file "$resolvedRoot\secrets\api-token" doctor
Write-Host "Recall is installed on 127.0.0.1:8788."
Write-Host "Use Tailscale Serve to publish that loopback endpoint privately."
