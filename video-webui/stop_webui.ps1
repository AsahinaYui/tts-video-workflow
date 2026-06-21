$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root "app.py"

$escapedApp = $App.Replace("\", "\\")
$matches = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match "python" -and
        $_.CommandLine -and
        ($_.CommandLine -like "*$App*" -or $_.CommandLine -like "*$escapedApp*")
    }

if (-not $matches) {
    Write-Host "No Local Video WebUI process found."
    exit 0
}

foreach ($proc in $matches) {
    Write-Host "Stopping PID $($proc.ProcessId): $($proc.CommandLine)"
    Stop-Process -Id $proc.ProcessId -Force
}

Write-Host "Stopped Local Video WebUI."
