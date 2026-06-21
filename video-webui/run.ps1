$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Join-Path $Root "config.json"
$Example = Join-Path $Root "config.example.json"

if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath $Example -Destination $Config
    Write-Host "Created config.json from config.example.json. Edit model paths if needed."
}

$Python = "python"
try {
    $cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.python_exe -and (Test-Path -LiteralPath $cfg.python_exe)) {
        $Python = $cfg.python_exe
    }
} catch {
    Write-Warning "Could not read config.json, falling back to PATH python."
}

$localBypass = "127.0.0.1,localhost,::1"
if ($env:NO_PROXY) {
    $env:NO_PROXY = "$localBypass,$env:NO_PROXY"
} else {
    $env:NO_PROXY = $localBypass
}
$env:no_proxy = $env:NO_PROXY
$env:GRADIO_ANALYTICS_ENABLED = "False"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "Starting Local Video WebUI..."
Write-Host "URL: http://127.0.0.1:7860"
Write-Host "Logs: $Root\server_boot.log"
& $Python (Join-Path $Root "app.py") --config $Config
