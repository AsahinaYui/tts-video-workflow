# Mutsumi TTS Video Workflow

[English](README.md) | [中文](README.zh-CN.md)

本仓库是本地单图口播视频工作流的代码整理版，包含：

- `video-webui/`：Gradio 本地视频制作 WebUI。
- `skills/gptsovits-tts/`：GPT-SoVITS TTS 调用、参数切换、音频校验和后处理脚本。
- `skills/single-image-tts-video/`：单图生成竖屏视频、字幕和 Premiere 辅助文件。

仓库不包含模型权重、参考音频、生成音视频或本地任务产物。

示例配置默认使用 GPT-SoVITS 底模推理，参考音频/参考文本留空；实际生成时请在 WebUI 上传参考音频/文本，或只在本机 `config.json` 中保存自己的参考预设。

## 运行方式

这是一个本地部署工作流，不依赖在线 Agent，也不会主动联网调用 LLM。

运行时会在本机调用：

- 本地 GPT-SoVITS API / runtime
- 本地 Faster-Whisper 模型
- 本地 FFmpeg / FFprobe
- 本地 Gradio WebUI

## 本地依赖

需要自行准备，并在本地配置中填写真实路径：

- GPT-SoVITS v2/v2Pro 本地项目
- GPT-SoVITS runtime Python、FFmpeg、FFprobe
- Faster-Whisper 模型
- 参考音频和对应参考文本
- 可选的自训练 GPT weights / SoVITS weights

## 一键本地配置

Windows 用户可以先运行：

```powershell
cd <repo>
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
cd <repo>
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_local.ps1 -InstallPythonPackages
```

`-InstallPythonPackages` 会调用 pip，可能需要联网。

## 启动

如果不使用 `setup_windows.bat`，也可以手动复制示例配置：

```powershell
cd <repo>\video-webui
Copy-Item .\config.example.json .\config.json
```

然后按本机路径编辑 `video-webui/config.json`。

启动前建议先检查本机依赖：

```powershell
cd <repo>
python .\tools\check_environment.py
```

也可以双击：

```text
check_environment.bat
```

检查脚本只读取本地路径并测试本机工具，不下载模型、不联网。

启动 WebUI：

```powershell
cd <repo>\video-webui
.\start_webui.ps1
```

默认地址：

```text
http://127.0.0.1:7860
```

浏览器页面关闭后，WebUI 会在短暂心跳超时后自动结束后台服务。
