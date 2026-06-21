---
name: single-image-tts-video
description: Create a subtitled 9:16 MP4 video from one still image and a GPT-SoVITS narration WAV, using the established Mutsumi reference video style. Use when the user provides or plans to provide an image for a single-frame vertical video, wants the local video WebUI, wants a 9:16 crop preview before production, wants the video duration to match narration audio, needs SRT subtitles generated or corrected, wants subtitle/audio QA, wants segmented TTS repair or manual missing-line patching inside the WebUI, wants to migrate the video-webui project, or wants Premiere/PR auxiliary files saved with the exported preview MP4.
---

# Single Image TTS Video

Use this skill after the GPT-SoVITS narration WAV is final.

If the user mentions the local WebUI, `video-webui`, migration to `E:\`, automatic missing-sentence repair, manual patch targets such as `3.2`, or the optimized subtitle workflow, read `references/video-webui.md` before acting. That reference records the newer WebUI project at `E:\TTS\video-webui`.

## Inputs

- One image from the user.
- One final reviewed narration WAV, usually from `outputs/tts_final`.
- The original narration text file when available, so subtitle and audio coverage can be checked.
- Optional BGM/music file from the user.

## Workflow

1. Save the user image into `work/` or use the attached file path directly.
2. Render a `1080x1920` crop preview first with `--crop-preview-only`. Send the preview image to the user and wait for confirmation.
3. If the crop is poor, adjust `--crop-x` and `--crop-y` until the user approves. Use `0.0` for left/top, `0.5` for center, and `1.0` for right/bottom.
4. Confirm the narration audio already passed the `gptsovits-tts` checker. If not, run that skill first.
5. Generate an SRT from the narration audio with Faster-Whisper when no corrected SRT exists. Use the local model at `E:/TTS/faster-whisper-small`.
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

## Default Render Choices

- Resolution: `1080x1920`
- FPS: `25`, matching `E:/TTS/reference_videos/Mutsumi小视频/成品/mutsmi_4.mp4`.
- Image fit: fill the frame with a 9:16 crop. Do not add black bars unless the user explicitly asks to preserve the full image.
- Audio codec: AAC, 192 kbps.
- BGM default: sidechain ducking on, volume `0.12`, fade in `1.5s`, fade out `3.0s`.
- Video codec: try H.264 first; if the bundled ffmpeg lacks `libx264`, fall back to MP4 `mpeg4`, `yuv420p`.
- Subtitle style: bold white Chinese subtitles with black outline, centered near the lower safe area, matching `mutsmi_4.mp4`.
- Subtitle text: no punctuation by default; wrap long entries with `--subtitle-max-chars 14` unless the user asks for a different density.
- Subtitle safe area: keep horizontal and bottom margins so burned subtitles do not touch the frame edge.
- Temporary copied source image, narration audio, BGM, and SRT sidecar are deleted by default after a burned-subtitle MP4 is exported. Pass `--keep-work-assets` only when those files are needed for manual editing.
- ffmpeg path: `E:/TTS/GPT-SoVITS-v2pro-20250604/runtime/ffmpeg.exe`
- ffprobe path: `E:/TTS/GPT-SoVITS-v2pro-20250604/runtime/ffprobe.exe`

If the user requests a horizontal or square video, change `--resolution`.

## Command

Crop preview gate:

```powershell
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --crop-preview-only
```

Adjusted crop preview:

```powershell
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --crop-preview-only `
  --crop-x 0.35 `
  --crop-y 0.5
```

Run with the GPT-SoVITS runtime Python so Faster-Whisper is available:

```powershell
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --expected-text-file .\work\tts-input-20260610.txt
```

Run with BGM:

```powershell
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
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
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --srt .\work\subtitles.srt
```

Create horizontal output:

```powershell
& "E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe" `
  .\skills\single-image-tts-video\scripts\make_single_image_video.py `
  --image .\work\image.png `
  --audio .\outputs\tts_final\final.wav `
  --resolution 1920x1080
```

## QA Rules

- Do not export the final MP4 if the TTS checker reports missing chunks, boundary omissions, or repeated chunks.
- Do not proceed from a new image to final MP4 until the user has approved the 9:16 crop preview.
- Do not rely on ASR text blindly; use the user's source text as the subtitle truth when audio is complete.
- Keep the SRT readable: short subtitle lines are better than long wall-text lines.
- Do not include punctuation in rendered subtitles unless the user explicitly asks for punctuation.
- Break long subtitle text into multiple lines before burning subtitles, prefer punctuation or speech-pause boundaries, and check that lines stay within the 9:16 safe area.
- When BGM is present, inspect the mix. Narration must remain clearly intelligible; lower BGM if it masks speech.
- If ffmpeg cannot burn subtitles, keep the sidecar SRT and report that subtitles were not burned in.
- If a real `.prproj` is required, use Premiere through desktop automation; otherwise the `premiere_aux` folder is the default PR-ready handoff.
- To save space, run `tools/cleanup_temporary_assets.ps1` after large batches; it removes regenerated temp copies while keeping final videos, final WAVs, reports, manifests, and source originals under `E:/TTS/input_images`.

