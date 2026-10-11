# Project State

Last updated: 2026-10-11

This file is the handoff note for new Codex threads. Read it before changing the workflow.

## Canonical Locations

- Development repository: `E:\TTS\mutsumi-tts-video-workflow`
- Active WebUI runtime copy: `E:\TTS\video-webui`
- Active skill runtime copy: `E:\TTS\Mutsumi\skills`
- Preferred launcher: `E:\TTS\打开视频制作WebUI.bat`
- WebUI URL: `http://127.0.0.1:7860`

Do not use legacy temporary WebUI copies such as:

- `%USERPROFILE%\Documents\Codex\2026-06-13\api\outputs\video-webui`

If the UI shows old buttons such as `2b. 分段 TTS 自动修复` or `6. 自动补漏句`, the browser is probably connected to an old WebUI process.

## New Thread Checklist

1. Start in `E:\TTS\mutsumi-tts-video-workflow`.
2. Run `git status --short` and `git log --oneline -5`.
3. Check the active WebUI process on port `7860`; its command line should point to `E:\TTS\video-webui\app.py`.
4. Read `video-webui/app.py`, `skills/single-image-tts-video/SKILL.md`, and `skills/single-image-tts-video/references/video-webui.md` before changing workflow behavior.
5. After repository edits, sync the changed runtime files to `E:\TTS\video-webui` and `E:\TTS\Mutsumi\skills` before restarting the WebUI.

Suggested process check:

```powershell
$conn = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
  $ownerPid = [int]$conn.OwningProcess
  Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" | Select-Object ProcessId,ExecutablePath,CommandLine
}
```

## Current WebUI Flow

The top command bar should be the current six-step flow:

1. `1. 准备任务`
2. `2. 生成 TTS`
3. `3. 建立修补分段索引`
4. `4. 生成裁切预览`
5. `5. 字幕生成/重排/审核`
6. `6. 渲染视频`

The old automatic-repair top-level buttons are deprecated:

- `2b. 分段 TTS 自动修复`
- `4. Faster-Whisper 字幕`
- `4b. 按编辑器重排时间轴`
- `5. 审核音频/字幕`
- `6. 自动补漏句`

Automatic repair helpers may still exist internally, but they should not drive the primary UI.

## TTS Mode Rules

- The TTS mode dropdown lives in the page header/right side, not as a top command-bar button.
- Default mode is `Hybrid`: generate sentence-level TTS first, then rebuild only likely omitted sentences from comma-level clauses.
- `Stable` generates comma-level clauses directly.
- `Quick` keeps the old whole-script TTS path.
- Stable/Hybrid ASR self-check is only a likely-omission check: retry when normalized ASR text is more than 2 characters shorter than the target segment.
- Do not use ASR keyword mismatch or strict CER as the automatic retry criterion for these modes.
- Final subtitle display text must still come from the source script or SRT editor, never raw ASR wording.
- Long TTS runs should update `logs/tts_progress.json`; hard timeouts are fallback guards, not the main progress signal.

## Storyboard Handoff Rules

- Storyboard export is allowed as a folded auxiliary WebUI section, not as a seventh top command.
- It uses `skills/single-image-tts-video/scripts/build_storyboard_package.py`.
- It should run from the current WebUI job after TTS timing or a manual patch index exists.
- Subtitles are optional and should default to off because storyboard images may already contain text.
- Package outputs live under the current job at `storyboard/package/` and include `timeline.json`, `segments.csv`, `shots.csv`, `markers.csv`, copied assets, and optional `preview.mp4`.
- Preview rendering must use `libx264`; do not silently fall back to `mpeg4/mp4v`.

## Manual Audio Patch Rules

- Generating a single-sentence patch candidate must only update the lower patch preview/player and candidate state.
- It must not update the main narration player or `audio_state`.
- The main narration audio may change only after the user clicks `应用到当前音频`.
- A patch candidate records the base full-audio path. If the main audio has changed, applying the old candidate must be blocked.
- Manual replacement should use the patch index only to locate the target slot, then replace that slot in the current full audio while preserving untouched prefix/suffix audio. Do not rebuild the whole file from pre-cut ASR chunks, and do not free-cut by the raw target ASR span alone.
- Applying a patch must validate that the resulting full audio is not abnormally short before writing it back to the main audio state.
- On failure, keep the existing main narration audio visible and unchanged.
- Replacement preserves samples at joins without crossfade or inserted padding, so the following clause's onset and timing remain intact. Replacing the first indexed slot starts at zero to remove any unindexed lead-in.

## Subtitle Rules

- Final subtitles should use the source text/editor text for display text, not raw ASR wording.
- ASR is used for timing and review, not as the final source of subtitle text.
- The SRT editor display should match final rendered subtitle pages and line breaks as closely as possible.
- Subtitle generation and audio patching should not silently rewrite each other's text grouping.
- When the SRT editor contains subtitle text, its pages and manual line breaks are the final render authority. Do not fall back to source-text regrouping just because punctuation or repair edits make the normalized text differ.
- Each subtitle page should render as one or two lines. If an editor block is longer than two safe lines, split it into consecutive subtitle pages before burning subtitles.

## Video Render Rules

- Final vertical video resolution: `1080x1920`.
- Use H.264 through `libx264` for final MP4 renders.
- Do not fall back silently to `mpeg4/mp4v` for still-image videos.
- Render the final MP4 from the cleaned crop preview image, not the raw uploaded image.
- Crop, render and storyboard fallback use the currently uploaded image, including when the image is changed after preparing a job.
- Run the flicker check after final render; if it reports `REVIEW`, inspect or re-render before further editing.

## Runtime Notes

- The WebUI is local and does not require an online Agent for normal use.
- GPT-SoVITS, Faster-Whisper, and FFmpeg are local dependencies configured in `video-webui/config.json`.
- Model weights and generated jobs are intentionally not tracked by Git.
- The active runtime copy may contain local presets and job outputs that should not be committed.
