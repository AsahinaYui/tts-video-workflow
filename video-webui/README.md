# Local Video WebUI

本地单图口播视频流水线，目标是把 TTS、字幕、裁切和渲染流程固化成可重复运行的本地 WebUI。

如果要迁移或交接，先读仓库根目录 `README.md`，并确认本机 `config.json` 已经指向正确的 GPT-SoVITS、Faster-Whisper 和 FFmpeg 路径。

## 功能

- 上传图片、文案、可选旁白音频、可选 BGM、可选 SRT
- 从 `config.json` 选择 GPT-SoVITS 推理模型
- 生成 GPT-SoVITS 旁白并试听
- 生成 9:16 裁切预览，调节裁切锚点
- 建立手动修补分段索引，按编号替换局部音频
- 用 Faster-Whisper 生成/重排字幕并审核
- 在页面里校对/编辑 SRT
- 调用现有 `make_single_image_video.py` 渲染 MP4 并预览
- 每个任务保存输入、日志、SRT、QA、视频输出

## 启动

后台启动：

```powershell
cd <repo>\video-webui
.\start_webui.ps1
```

脚本会自动打开浏览器。浏览器页面关闭后，WebUI 会在短暂心跳超时后自动结束后台服务。

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

默认示例配置使用 GPT-SoVITS 底模推理：

- `gpt_weights_path`: `__use_pretrained_base__`
- `sovits_weights_path`: `__use_pretrained_base__`
- `reference_assets`: 空数组
- 参考音频和参考文本默认留空

实际生成时需要在页面上传参考音频/文本，或在本机 `config.json` 保存自己的参考预设。

新增自训练模型时，在 `config.json` 的 `models` 数组里添加一项。每个模型可以复用同一个 `base_config`，再覆盖：

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

## 推荐流程

页面顶部主流程按 `1-6` 排列：

1. `准备任务`
2. `生成 TTS`
3. `建立修补分段索引`
4. `生成裁切预览`
5. `字幕生成/重排/审核`
6. `渲染视频`

建议先用 `生成 TTS` 生成整段旁白并人工试听。若只发现一两处问题，点击 `建立修补分段索引`，再在 `单句补漏 / 替换试听` 里输入编号，例如 `3.2`、`3.2,4.1` 或整句编号 `3`。

手动填写编号时，修补语义是 **替换**：程序会裁掉原音频中对应编号的时间区间，再放入新生成的修补音频。跨句边界不自然时，优先选择连续编号一起替换，例如 `3.2,4.1`。

索引建立会记录每个编号的时间轴和对齐置信度。若某段置信度很低，建议扩大替换范围到连续小段或整句。

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

## 字幕审核

页面里的 `字幕生成/重排/审核` 会先做本地规则审核：

- 如果已有 SRT，按编辑器内容重排时间轴并审核
- 如果没有 SRT 但已有音频，会先用 Faster-Whisper 生成 SRT，再审核
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
