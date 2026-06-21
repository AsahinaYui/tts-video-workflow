[CmdletBinding()]
param(
    [string]$GptSoVitsRoot = "",
    [string]$AsrModel = "",
    [switch]$InstallPythonPackages,
    [switch]$Force,
    [switch]$NoPrompt
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VideoDir = Join-Path $RepoRoot "video-webui"
$ConfigExample = Join-Path $VideoDir "config.example.json"
$ConfigPath = Join-Path $VideoDir "config.json"
$VoiceTemplate = Join-Path $RepoRoot "skills\gptsovits-tts\config\voice_default.json"
$LocalDir = Join-Path $RepoRoot "local"
$LocalVoiceConfig = Join-Path $LocalDir "voice_default.local.json"
$Requirements = Join-Path $VideoDir "requirements.txt"
$CheckScript = Join-Path $RepoRoot "tools\check_environment.py"

function Read-SetupValue {
    param(
        [string]$Prompt,
        [string]$Default
    )
    if ($NoPrompt) {
        return $Default
    }
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function To-JsonPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    return ([System.IO.Path]::GetFullPath($Path)).Replace("\", "/")
}

function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

if (-not (Test-Path -LiteralPath $ConfigExample)) {
    throw "Missing config example: $ConfigExample"
}
if (-not (Test-Path -LiteralPath $VoiceTemplate)) {
    throw "Missing voice template: $VoiceTemplate"
}

$RepoParent = Split-Path -Parent $RepoRoot
$DefaultGptSoVitsRoot = if ($env:GPTSOVITS_ROOT) {
    $env:GPTSOVITS_ROOT
} else {
    Join-Path $RepoParent "GPT-SoVITS-v2pro-20250604"
}
$DefaultAsrModel = if ($env:FASTER_WHISPER_MODEL) {
    $env:FASTER_WHISPER_MODEL
} else {
    Join-Path $RepoParent "faster-whisper-small"
}

if ([string]::IsNullOrWhiteSpace($GptSoVitsRoot)) {
    $GptSoVitsRoot = Read-SetupValue "GPT-SoVITS root" $DefaultGptSoVitsRoot
}
if ([string]::IsNullOrWhiteSpace($AsrModel)) {
    $AsrModel = Read-SetupValue "Faster-Whisper model directory" $DefaultAsrModel
}

$GptSoVitsRootFull = [System.IO.Path]::GetFullPath($GptSoVitsRoot)
$AsrModelFull = [System.IO.Path]::GetFullPath($AsrModel)
$PythonExe = Join-Path $GptSoVitsRootFull "runtime\python.exe"
$Ffmpeg = Join-Path $GptSoVitsRootFull "runtime\ffmpeg.exe"
$Ffprobe = Join-Path $GptSoVitsRootFull "runtime\ffprobe.exe"

if ((Test-Path -LiteralPath $ConfigPath) -and -not $Force) {
    $overwrite = if ($NoPrompt) { "N" } else { Read-Host "video-webui\config.json already exists. Overwrite it? [y/N]" }
    if ($overwrite -notin @("y", "Y", "yes", "YES")) {
        Write-Host "Keeping existing config.json."
        Write-Host "Use -Force to regenerate it."
        exit 0
    }
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

$cfg = Get-Content -LiteralPath $ConfigExample -Raw -Encoding UTF8 | ConvertFrom-Json
$gsv = To-JsonPath $GptSoVitsRootFull
$repo = To-JsonPath $RepoRoot

Set-JsonProperty $cfg "project_root" $repo
Set-JsonProperty $cfg "python_exe" (To-JsonPath $PythonExe)
Set-JsonProperty $cfg "gptsovits_root" $gsv
Set-JsonProperty $cfg "gpt_weights_dirs" @(
    "$gsv/GPT_weights",
    "$gsv/GPT_weights_v2",
    "$gsv/GPT_weights_v2Pro",
    "$gsv/GPT_weights_v2ProPlus",
    "$gsv/GPT_weights_v3",
    "$gsv/GPT_weights_v4"
)
Set-JsonProperty $cfg "sovits_weights_dirs" @(
    "$gsv/SoVITS_weights",
    "$gsv/SoVITS_weights_v2",
    "$gsv/SoVITS_weights_v2Pro",
    "$gsv/SoVITS_weights_v2ProPlus",
    "$gsv/SoVITS_weights_v3",
    "$gsv/SoVITS_weights_v4"
)
Set-JsonProperty $cfg "gsv_tts_script" (To-JsonPath (Join-Path $RepoRoot "skills\gptsovits-tts\scripts\gsv_tts.py"))
Set-JsonProperty $cfg "tts_checker_script" (To-JsonPath (Join-Path $RepoRoot "skills\gptsovits-tts\scripts\check_tts_match.py"))
Set-JsonProperty $cfg "video_script" (To-JsonPath (Join-Path $RepoRoot "skills\single-image-tts-video\scripts\make_single_image_video.py"))
Set-JsonProperty $cfg "ffmpeg" (To-JsonPath $Ffmpeg)
Set-JsonProperty $cfg "ffprobe" (To-JsonPath $Ffprobe)
Set-JsonProperty $cfg "asr_model" (To-JsonPath $AsrModelFull)
Set-JsonProperty $cfg "reference_assets" @()

if (-not $cfg.models -or $cfg.models.Count -eq 0) {
    throw "config.example.json does not contain a default model template."
}
$cfg.models[0].base_config = To-JsonPath $LocalVoiceConfig
$cfg.models[0].gpt_weights_path = "__use_pretrained_base__"
$cfg.models[0].sovits_weights_path = "__use_pretrained_base__"
$cfg.models[0].use_pretrained_base = $true

$cfg | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

$voice = Get-Content -LiteralPath $VoiceTemplate -Raw -Encoding UTF8 | ConvertFrom-Json
Set-JsonProperty $voice "gptsovits_root" $gsv
Set-JsonProperty $voice "python_exe" (To-JsonPath $PythonExe)
Set-JsonProperty $voice "tts_config_path" "skills/gptsovits-tts/config/tts_infer_v2pro.yaml"
Set-JsonProperty $voice "gpt_weights_path" "$gsv/GPT_weights_v2Pro/your_model.ckpt"
Set-JsonProperty $voice "sovits_weights_path" "$gsv/SoVITS_weights_v2Pro/your_model.pth"
$voice | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $LocalVoiceConfig -Encoding UTF8

Write-Host "Generated local config:"
Write-Host "  $ConfigPath"
Write-Host "  $LocalVoiceConfig"

if ($InstallPythonPackages) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Cannot install Python packages because python_exe is missing: $PythonExe"
    }
    Write-Host "Installing Python packages from $Requirements"
    Write-Host "This step may use the network through pip."
    & $PythonExe -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed."
    }
}

$ProbePython = if (Test-Path -LiteralPath $PythonExe) { $PythonExe } else { "python" }
Write-Host ""
Write-Host "Running environment check..."
& $ProbePython $CheckScript --config $ConfigPath
exit $LASTEXITCODE
