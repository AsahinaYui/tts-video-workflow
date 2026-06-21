param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $Root "config.json"
$Example = Join-Path $Root "config.example.json"
$App = Join-Path $Root "app.py"

if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath $Example -Destination $Config
    Write-Host "Created config.json from config.example.json. Edit model paths if needed."
}

$cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$Python = if ($cfg.python_exe -and (Test-Path -LiteralPath $cfg.python_exe)) { $cfg.python_exe } else { "python" }
$Port = if ($cfg.port) { [int]$cfg.port } else { 7860 }
$HostName = if ($cfg.host) { [string]$cfg.host } else { "127.0.0.1" }
$Url = "http://${HostName}:${Port}"

$existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -First 1

if ($existing) {
    Write-Host "Local Video WebUI already appears to be running."
    Write-Host "URL: $Url"
    Write-Host "OwningProcess: $($existing.OwningProcess)"
    if (-not $NoBrowser) {
        Start-Process $Url
    }
    exit 0
}

$gradioCheck = & $Python -c "import gradio" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Configured Python cannot import gradio: $Python`n$gradioCheck"
}

$env:NO_PROXY = "127.0.0.1,localhost,::1,$env:NO_PROXY"
$env:no_proxy = $env:NO_PROXY
$env:GRADIO_ANALYTICS_ENABLED = "False"
$env:PYTHONIOENCODING = "utf-8"

foreach ($name in @("server_boot.log", "server_stdout.txt", "server_stderr.txt")) {
    $log = Join-Path $Root $name
    if (Test-Path -LiteralPath $log) {
        Clear-Content -LiteralPath $log
    }
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = '"' + $App + '" --config "' + $Config + '"'
$psi.WorkingDirectory = $Root
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.UseShellExecute = $true
$process = [System.Diagnostics.Process]::Start($psi)

Start-Sleep -Seconds 5
$status = try {
    (Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 10).StatusCode
} catch {
    $_.Exception.Message
}

Write-Host "Started Local Video WebUI"
Write-Host "PID: $($process.Id)"
Write-Host "URL: $Url"
Write-Host "HTTP: $status"
Write-Host "Logs:"
Write-Host "  $Root\server_boot.log"
Write-Host "  $Root\server_stdout.txt"
Write-Host "  $Root\server_stderr.txt"

if (($status -eq 200 -or "$status" -eq "200") -and -not $NoBrowser) {
    Start-Process $Url
}
