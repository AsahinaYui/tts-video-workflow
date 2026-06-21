# Local Video WebUI

本地单图口播视频流水线，目标是把之前靠 Codex Agent 串起来的流程固化成可重复运行的本地 WebUI。

如果要迁移或交接，先读仓库根目录 `README.md`，并确认本机 `config.json` 已经指向正确的 GPT-SoVITS、Faster-Whisper 和 FFmpeg 路径。

## 功能

- 上传图片、文案、可选旁白音频、可选 BGM、可选 SRT
- 从 `config.json` 选择 GPT-SoVITS 推理模型
- 生成 GPT-SoVITS 旁白并试听
- 生成 9:16 裁切预览，调节裁切锚点
- 用 Faster-Whisper 生成字幕草稿
- 在页面里校对/编辑 SRT
- 调用现有 `make_single_image_video.py` 渲染 MP4 并预览
- 按 B站预设生成发布包、标题/简介/标签草稿和 16:9 封面预览
- 每个任务保存输入、日志、SRT、QA、视频输出

## 启动

后台启动：

```powershell
cd <repo>\video-webui
.\start_webui.ps1
```

停止后台服务：

```powershell
cd <repo>\video-webui
.\stop_webui.ps1
```

前台启动（方便实时看日志）：

```powershell
cd <repo>\video-webui
.\run.ps1
```

第一次启动会从 `config.example.json` 复制出 `config.json`。如果路径不对，先改 `config.json`。

启动后打开：

```text
http://127.0.0.1:7860
```

如果窗口里显示已启动但页面打不开，先看：

```text
server_boot.log
server_stdout.txt
server_stderr.txt
```

脚本会自动设置 `NO_PROXY=127.0.0.1,localhost,::1`，避免系统代理影响 Gradio 的本机自检。

如果 GPT-SoVITS runtime 里没有 Gradio：

```powershell
<GPT-SoVITS-root>\runtime\python.exe -m pip install -r requirements.txt
```

## 模型配置

新增模型时，在 `config.json` 的 `models` 数组里添加一项。每个模型可以复用同一个 `base_config`，再覆盖：

- `gpt_weights_path`
- `sovits_weights_path`
- `ref_audio_path`
- `ref_text_path`
- `default_version`
- `speed_factor`
- `fragment_interval`
- `text_split_method`

WebUI 会为每个任务生成临时 voice config，不会修改原来的 `voice_default.json`。

## 任务输出

默认输出在：

```text
video-webui/jobs/
```

每个任务会有独立目录，里面包含：

- `input/source.txt`
- `input/source_image.*`
- `tts/*.wav`
- `asr/subtitles.srt`
- `checks/tts_match_report.md`
- `render/*/preview.mp4`
- `logs/*.txt`

## 长文本 TTS 自动修复

长文本建议优先使用页面里的 `2b. 分段 TTS 自动修复`，不要直接用普通 `2. 生成 TTS`。

这个流程不调用 Agent，也不消耗 token。它会：

- 按句子把长文切成较短片段
- 每段单独调用 GPT-SoVITS 生成音频
- 用 Faster-Whisper 本地回听每段音频
- 用 CER 和长度比例判断是否漏句、掐头、去尾
- 失败片段自动重试
- 每段首尾补一点静音后拼接成完整旁白
- 自动生成一份 SRT 和一份修复报告

推荐默认参数：

- `自动修复分段字数`: 60-80
- `失败片段重试次数`: 2
- `ASR 通过 CER`: 0.12-0.16
- `片段首尾静音 ms`: 120-200

输出文件通常在当前 job 目录下：

- `tts/tts_segmented_auto_repaired.wav`
- `asr/subtitles.segmented_auto.srt`
- `checks/tts_segmented_auto_repair.md`

## 推理面板

页面里的 `TTS 模型与参考信息` 支持按任务覆盖角色预设：

- `GPT weights`: 选择 `.ckpt`
- `SoVITS weights`: 选择 `.pth`
- `参考音频/文本预设`: 从 `config.json` 的 `reference_assets` 读取
- `参考音频`: 上传后会覆盖预设里的参考音频
- `参考音频文本`: 可从预设回填，也可以手动修改
- `无参考文本模式`: 生成时传空 `prompt_text`，v3/v4 模型不建议也不支持
- `参考音频语种`: `zh/en/ja/ko/yue`
- `合成文本语种`: `zh/en/ja/ko/yue`
- `文本切分方式`: `cut0` 到 `cut5`
- `语速`、`句间停顿秒`、`top_k`、`top_p`、`temperature`

这些选择只会写入当前 job 的临时 `tmp/voice_config*.json`，不会修改原始 `voice_default.json`。

新增模型或参考素材时，优先编辑 `config.json`：

- `gpt_weights_dirs`
- `sovits_weights_dirs`
- `reference_assets`
- `models`

## 二次审核

页面里的 `6. 二次审核` 会先做本地规则审核：

- 如果已有 SRT，直接审核 SRT 和原文
- 如果没有 SRT 但已有音频，会先用 Faster-Whisper 生成 SRT
- 输出 `checks/tts_secondary_review.md`
- 标记 `PASS / REVIEW / FAIL`
- 定位缺失片段、弱匹配片段、疑似掐头去尾

也可以选择 `本地 + Chat API`，通过 OpenAI-compatible Chat Completions 接口做语义复核。这个接口只使用 `/chat/completions`，不使用 Responses API。

可填写：

```text
http://127.0.0.1:1234
http://127.0.0.1:1234/v1
http://127.0.0.1:1234/v1/chat/completions
```

程序会统一补成：

```text
/v1/chat/completions
```

可用于 LM Studio、Ollama OpenAI-compatible、LiteLLM、vLLM、llama.cpp server 或其它兼容服务。API Key 默认从环境变量读取：

```text
AGENT_REVIEW_API_KEY
```

也可以在 `config.json` 的 `agent_review` 里改：

```json
{
  "agent_review": {
    "mode": "local",
    "api_url": "",
    "model": "",
    "api_key_env": "AGENT_REVIEW_API_KEY",
    "timeout": 120
  }
}
```

Gradio API 端点名：

```text
secondary_review
```
