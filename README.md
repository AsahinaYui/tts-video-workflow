# Mutsumi TTS Video Workflow

本仓库是本地单图口播视频工作流的代码整理版，包含：

- `video-webui/`：Gradio 本地视频制作 WebUI。
- `skills/gptsovits-tts/`：GPT-SoVITS TTS 调用、参数切换、音频校验和后处理脚本。
- `skills/single-image-tts-video/`：单图生成竖屏视频、字幕和 Premiere 辅助文件。
- `skills/video-platform-publisher/`：发布前素材打包和平台预设说明。

仓库不包含模型权重、参考音频、生成音视频、平台账号信息或本地任务产物。

## 运行方式

这是一个本地部署工作流，不依赖 Codex Agent，也不会主动联网调用 LLM。

运行时会在本机调用：

- 本地 GPT-SoVITS API / runtime
- 本地 Faster-Whisper 模型
- 本地 FFmpeg / FFprobe
- 本地 Gradio WebUI

如果配置了平台发布自动化，登录和上传动作另算，需要用户在浏览器中授权确认。

## 本地依赖

需要自行准备：

- GPT-SoVITS v2/v2Pro 本地项目，例如 `E:/TTS/GPT-SoVITS-v2pro-20250604`
- GPT-SoVITS runtime Python、FFmpeg、FFprobe
- Faster-Whisper 模型，例如 `E:/TTS/faster-whisper-small`
- 参考音频和对应参考文本
- 可选的自训练 GPT weights / SoVITS weights

## 一键本地配置

Windows 用户可以先运行：

```powershell
cd E:\TTS\mutsumi-tts-video-workflow
.\setup_windows.bat
```

它会引导填写：

- GPT-SoVITS 根目录
- Faster-Whisper 模型目录

然后自动生成：

- `video-webui/config.json`
- `local/voice_default.local.json`

生成后会立即运行环境检测。

默认不会下载 GPT-SoVITS、模型或其他大文件。如果需要顺便安装 Python 包，可以运行：

```powershell
cd E:\TTS\mutsumi-tts-video-workflow
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_local.ps1 -InstallPythonPackages
```

`-InstallPythonPackages` 会调用 pip，可能需要联网。

## 启动

如果不使用 `setup_windows.bat`，也可以手动复制示例配置：

```powershell
cd E:\TTS\mutsumi-tts-video-workflow\video-webui
Copy-Item .\config.example.json .\config.json
```

然后按本机路径编辑 `video-webui/config.json`。

启动前建议先检查本机依赖：

```powershell
cd E:\TTS\mutsumi-tts-video-workflow
python .\tools\check_environment.py
```

也可以双击：

```text
check_environment.bat
```

检查脚本只读取本地路径并测试本机工具，不下载模型、不联网。

启动 WebUI：

```powershell
cd E:\TTS\mutsumi-tts-video-workflow\video-webui
.\start_webui.ps1
```

默认地址：

```text
http://127.0.0.1:7860
```

## GitHub 上传建议

建议先作为 Private 仓库发布。发布前确认 GitHub Desktop 的 Changes 列表里没有：

- `jobs/`
- `temp/`
- `reference/`
- `.wav` / `.mp4` / `.srt`
- `.ckpt` / `.pth` / `model.bin`
- `config.json`

如果以后要公开仓库，需要再次检查图片、音频、模型权重和第三方项目 License。
