---
name: single-image-tts-video
description: Create a subtitled 9:16 MP4 video from one still image and a GPT-SoVITS narration WAV, or build an offline storyboard package from numbered TTS timing and multiple images, using the established Mutsumi reference video style. Use when the user provides or plans to provide an image for a single-frame vertical video, wants the local video WebUI, wants a 9:16 crop preview before production, wants the video duration to match narration audio, needs SRT subtitles generated or corrected, wants subtitle/audio QA, wants Hybrid/Stable/Quick TTS modes or manual missing-line patching inside the WebUI, wants storyboard image timing by numbered segments, wants to migrate the video-webui project, or wants Premiere/PR auxiliary files saved with the exported preview MP4.
---

# Single Image TTS Video

Use this skill after the GPT-SoVITS narration WAV is final.

If the user mentions the local WebUI, `video-webui`, automatic missing-sentence repair, manual patch targets such as `3.2`, or the optimized subtitle workflow, read `references/video-webui.md` before acting.

## Inputs

- One image from the user.
- One final reviewed narration WAV, usually from `outputs/tts_final`.
- The original narration text file when available, so subtitle and audio coverage can be checked.
- Optional BGM/music file from the user.
- Optional multiple storyboard images plus a storyboard mapping, such as image 1 for `1.1,1.2`, image 2 for `2.1,2.2`, and image 3 for `2.3`.
- Optional segment id map when the visible repair/listing numbers differ from the internal numbered timeline rows.

## Workflow

1. Save the user image into `work/` or use the attached file path directly.
2. Render a `1080x1920` crop preview first with `--crop-preview-only`. Send the preview image to the user and wait for confirmation.
3. If the crop is poor, adjust `--crop-x` and `--crop-y` until the user approves. Use `0.0` for left/top, `0.5` for center, and `1.0` for right/bottom.
4. Confirm the narration audio already passed the `gptsovits-tts` checker. If not, run that skill first.
5. Generate an SRT from the narration audio with Faster-Whisper when no corrected SRT exists. Use the local model configured in `config.json` or `FASTER_WHISPER_MODEL`.
6. Review the SRT against the source text and audio:
   - Missing or repeated sentence stems must be corrected before export.
   - Accent-related ASR substitutions are acceptable only when the spoken audio is complete.
   - Adjust SRT text manually when ASR wording differs from correct source text but the timing is usable.
   - Remove punctuation from rendered subtitle text by default.
   - Wrap long subtitle entries onto multiple lines so each line stays inside the vertical frame.
   - Prefer line breaks at original punctuation or natural speech pauses before removing punctuation.
7. If the user provides a music file, select a suitable BGM segment and mix it quietly under narration. Prioritize speech clarity.
8. Render a single-frame video using the approved 9:16 crop for the full audio duration.
9. Burn subtitles into the MP4 when ffmpeg supports the subtitle filter. Always keep the `.srt` sidecar even when subtitles are burned in.
10. Save the output folder under `outputs/video/`. It must include:
   - `crop_preview.png`
   - `preview.mp4`
   - `subtitle_qa.md`
   - `project_manifest.json`
   - `ffmpeg_command.txt`
   - `subtitles.srt` only when subtitles are not burned in or the user asks to keep the sidecar.
   - `premiere_aux/` only when the user asks for PR/Premiere helper files or `--keep-premiere-aux` is passed.
11. Verify the rendered video duration is close to the narration duration.
12. Return the MP4 path for preview and mention the SRT and auxiliary folder paths.

## BGM Rules

When the user sends a music file while making a video, use it as background music unless they explicitly say not to.

- Do not modify the original music file. Copy it into the output folder as `bgm.<ext>`.
- Choose a stable, less busy section for BGM. Avoid loud intros, drops, vocals, dense percussion, or sections that compete with narration.
- Keep BGM quiet. Start with `--bgm-volume 0.12`; use `0.08-0.18` as the normal range.
- Keep sidechain ducking enabled by default so narration lowers the BGM automatically.
- Use `--bgm-start` to choose the segment start when the first seconds are not suitable.
- Use gentle fades: default `--bgm-fade-in 1.5` and `--bgm-fade-out 3.0`.
- If speech feels masked, lower `--bgm-volume` before changing narration loudness.
- If the user only asks to make the video and supplies music, include BGM automatically at conservative volume.

## Storyboard Package Rules

Use the storyboard package path when the user wants multiple images to switch on the same numbered TTS timeline, or wants to finish detailed cutting in Premiere, Jianying/CapCut, or another mature editor.

- Keep the normal six-step WebUI video flow intact; storyboard export is an auxiliary handoff, not a replacement for TTS repair.
- Base timing on WebUI TTS mode outputs, manual patch indexes, and the reviewed final narration audio, not on guessed image durations.
- Subtitles are optional. Use `--subtitle-mode off` by default when images may already contain text.
- Use `sidecar` when the user wants an SRT file, and `burn` only when they ask for a rendered local preview with burned subtitles.
- A storyboard mapping can be uploaded as JSON or entered quickly in the WebUI as `image_number: segment_ids`.
- Segment ids may be simple internal row ids (`1`, `2`, `3`) or user-facing ids (`1.1`, `1.2`) when a `segment-id-map` file is supplied.
- Preserve exact timing artifacts: `timeline.json`, `segments.csv`, `shots.csv`, and `markers.csv` are the handoff source of truth for external editors.
- Render preview MP4s with `libx264`; do not silently fall back to `mpeg4/mp4v`.

## Default Render Choices

- Resolution: `1080x1920`
- FPS: `25`, matching the reference style used by this workflow.
- Image fit: fill the frame with a 9:16 crop. Do not add black bars unless the user explicitly asks to preserve the full image.
- Render frame: after crop preview, render the final MP4 from the cleaned `crop_preview.png` instead of looping the raw uploaded image. This avoids corrupt or oddly encoded PNG uploads producing one-frame/one-second videos.
- Audio codec: AAC, 192 kbps.
- BGM default: sidechain ducking on, volume `0.12`, fade in `1.5s`, fade out `3.0s`.
- Video codec: require H.264 through `libx264` for final MP4 renders. Do not silently fall back to `mpeg4/mp4v` or hardware H.264 for still-image videos; those paths can introduce periodic flicker that survives later editing.
- Subtitle style: bold white Chinese subtitles with black outline, centered near the lower safe area, matching `mutsmi_4.mp4`.
- Subtitle text: no punctuation by default; wrap long entries with `--subtitle-max-chars 14` unless the user asks for a different density.
- WebUI SRT editor text is authoritative for final render pages and manual line breaks. Keep each subtitle page to one or two lines; split longer editor blocks into consecutive pages before burning.
- Subtitle safe area: keep horizontal and bottom margins so burned subtitles do not touch the frame edge.
- Temporary copied source image, narration audio, BGM, and SRT sidecar are deleted by default after a burned-subtitle MP4 is exported. Pass `--keep-work-assets` only when those files are needed for manual editing.
- ffmpeg path: configured in `config.json`, or pass `--ffmpeg`. Prefer a full system ffmpeg build with `libx264`; the bundled GPT-SoVITS ffmpeg may not include it.
- ffprobe path: configured in `config.json`, or pass `--ffprobe`.
- After each final render, inspect the top static region for repeated brightness jumps. If `flicker_check.json` reports `REVIEW`, do not continue editing that MP4 until the preview has been checked or re-rendered.
- Manual sentence patch preview must stay isolated from the main narration audio. Only applying a candidate may update the main narration player/state. Replacement applies to the numbered target slot in the current full audio, preserving untouched prefix/suffix audio, and must validate that the resulting audio is not abnormally short or inconsistent with the replacement duration.

If the user requests a horizontal or square video, change `--resolution`.

## Command

Crop preview gate:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --crop-preview-only
```

Adjusted crop preview:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --crop-preview-only `
  --crop-x 0.35 `
  --crop-y 0.5
```

Run with the GPT-SoVITS runtime Python so Faster-Whisper is available:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --expected-text-file .\work\tts-input-20260610.txt
```

Run with BGM:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --expected-text-file .\work\tts-input-20260610.txt `
  --bgm .\work\music.mp3 `
  --bgm-volume 0.12 `
  --bgm-start 15
```

If the BGM still competes with speech, rerun with `--bgm-volume 0.08`. Use `--no-bgm-ducking` only when there is no important narration or the user explicitly wants music to stay constant.

Use an existing SRT instead of generating one:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --srt .\work\subtitles.srt
```

Create horizontal output:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --resolution 1920x1080
```

Export a storyboard handoff package from a WebUI job:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\build_storyboard_package.py `
  --webui-job .\video-webui\jobs\20260704_example `
  --storyboard .\work\storyboard.json `
  --subtitle-mode off `
  --output-dir .\outputs\storyboard `
  --name example_storyboard `
  --ffmpeg "<ffmpeg.exe>" `
  --ffprobe "<ffprobe.exe>"
```

Minimal storyboard JSON:

```json
{
  "shots": [
    {"image": "E:/TTS/assets/storyboard/image_001.png", "segments": ["1.1", "1.2"]},
    {"image": "E:/TTS/assets/storyboard/image_002.png", "segments": ["2.1", "2.2"]},
    {"image": "E:/TTS/assets/storyboard/image_003.png", "segments": ["2.3"]}
  ]
}
```

Render a local storyboard preview only after timing assets are ready:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\build_storyboard_package.py `
  --webui-job .\video-webui\jobs\20260704_example `
  --storyboard .\work\storyboard.json `
  --subtitle-mode sidecar `
  --render-preview `
  --output-dir .\outputs\storyboard `
  --name example_preview `
  --ffmpeg "<ffmpeg.exe>" `
  --ffprobe "<ffprobe.exe>"
```

## QA Rules

- Do not export the final MP4 if the TTS checker reports missing chunks, boundary omissions, or repeated chunks.
- Do not proceed from a new image to final MP4 until the user has approved the 9:16 crop preview.
- Do not rely on ASR text blindly; use the user's source text as the subtitle truth when audio is complete.
- Keep the SRT readable: short subtitle lines are better than long wall-text lines.
- Do not include punctuation in rendered subtitles unless the user explicitly asks for punctuation.
- Break long subtitle text into one or two display lines per subtitle page before burning subtitles. If it still does not fit, split it into another subtitle page instead of letting ffmpeg/libass auto-wrap it.
- When BGM is present, inspect the mix. Narration must remain clearly intelligible; lower BGM if it masks speech.
- If ffmpeg cannot burn subtitles, keep the sidecar SRT and report that subtitles were not burned in.
- If a real `.prproj` is required, use Premiere through desktop automation; otherwise the `premiere_aux` folder is the default PR-ready handoff.
- To save space, clean regenerated temp copies after large batches while keeping final videos, final WAVs, reports, manifests, and source originals.

