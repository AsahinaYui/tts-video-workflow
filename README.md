# Mutsumi TTS Video Workflow

[English](README.md) | [中文](README.zh-CN.md)

A local single-image narration video workflow built around GPT-SoVITS, Faster-Whisper, FFmpeg, and a Gradio WebUI.

This repository contains:

- `video-webui/`: the local Gradio video production WebUI.
- `skills/gptsovits-tts/`: GPT-SoVITS TTS invocation, model switching, audio checking, and post-processing scripts.
- `skills/single-image-tts-video/`: single-image vertical video rendering, subtitle generation, and Premiere helper output.

The repository does not include model weights, reference audio, generated audio/video files, or local job outputs.

The example configuration defaults to GPT-SoVITS base-model inference. Reference audio and reference text are blank by default. For real generation, upload reference audio/text in the WebUI or save your own local presets in `config.json`.

## Runtime Model

This is a local deployment workflow. It does not depend on an online Agent and does not call any LLM service by default.

At runtime it uses local tools:

- local GPT-SoVITS API / runtime
- local Faster-Whisper model
- local FFmpeg / FFprobe
- local Gradio WebUI

## Local Dependencies

Prepare these locally and point the configuration to their real paths:

- a local GPT-SoVITS v2/v2Pro project
- GPT-SoVITS runtime Python, FFmpeg, and FFprobe
- a Faster-Whisper model directory
- reference audio and matching reference text
- optional custom GPT weights / SoVITS weights

## One-Step Local Setup

On Windows, run:

```powershell
cd <repo>
.\setup_windows.bat
```

The setup script asks for:

- GPT-SoVITS root directory
- Faster-Whisper model directory

Then it generates:

- `video-webui/config.json`
- `local/voice_default.local.json`

After generation, it immediately runs the local environment check.

By default, setup does not download GPT-SoVITS, models, or other large files. To also install Python packages, run:

```powershell
cd <repo>
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_local.ps1 -InstallPythonPackages
```

`-InstallPythonPackages` uses `pip` and may require network access.

## Launch

If you do not use `setup_windows.bat`, you can create the config manually:

```powershell
cd <repo>\video-webui
Copy-Item .\config.example.json .\config.json
```

Then edit `video-webui/config.json` for your local paths.

Before launching, you can check local dependencies:

```powershell
cd <repo>
python .\tools\check_environment.py
```

Or double-click:

```text
check_environment.bat
```

The environment checker only reads local paths and tests local tools. It does not download models or access the network.

Start the WebUI:

```powershell
cd <repo>\video-webui
.\start_webui.ps1
```

Default URL:

```text
http://127.0.0.1:7860
```

After the browser page is closed, the WebUI process exits automatically after a short heartbeat timeout.
