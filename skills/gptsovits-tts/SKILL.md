---
name: gptsovits-tts
description: Generate and repair narration with the local GPT-SoVITS api_v2.py workflow, using the configured Mutsumi reference voice, v2 base model switching, 0.95 speed, 0.35 sentence gaps, text coverage checking through Faster-Whisper, and final WAV post-processing. Use when the user asks to run GPT-SoVITS TTS, synthesize Chinese narration, regenerate missing phrases, check whether generated speech matches source text, or produce a final reviewed narration audio file.
---

# GPT-SoVITS TTS

Use this skill to produce the final reviewed narration WAV for the Mutsumi workflow.

## Current Defaults

- Project root: the repository root that contains `skills/`.
- GPT-SoVITS root: set in `video-webui/config.json`, `voice_default.json`, or `GPTSOVITS_ROOT`.
- Runtime Python: `<GPT-SoVITS-root>/runtime/python.exe`.
- Voice config: `skills/gptsovits-tts/config/voice_default.json`
- Reference audio: configured locally in `config.json`.
- Reference text: configured locally in `config.json`.
- ASR model: set in `video-webui/config.json` or `FASTER_WHISPER_MODEL`.
- Final audio directory: `outputs/tts_final`

The voice config should keep these request parameters unless the user asks to change them:

```json
{
  "text_split_method": "cut1",
  "fragment_interval": 0.35,
  "speed_factor": 0.95
}
```

Generated audio should not be opened automatically. Keep `output.open_after_generate` as `false` and pass `--no-open` in ad-hoc commands unless the user explicitly asks to preview the WAV immediately.

## Workflow

1. Put the source copy in `work/` as a UTF-8 text file.
2. Confirm `voice_default.json` points to the 9.5 second reference WAV and matching reference text. Keep reference audio under 10 seconds.
3. Generate narration with `scripts/gsv_tts.py`. The script starts the local API when needed, writes the temporary workflow YAML under project `temp/`, switches SoVITS to `model_switch.default_version` first, then sets GPT weights and final SoVITS weights.
4. Check the generated WAV against the source text with `scripts/check_tts_match.py` and the local Faster-Whisper model.
5. Treat these checker findings as real problems unless human review proves otherwise:
   - Missing chunks, such as an entire phrase not spoken.
   - Boundary omissions, such as losing `结果` at the start of a covered sentence.
   - Repeated chunks.
6. If the checker fails, regenerate only the missing or wrong phrase into `outputs/tts_patch`, splice or replace that section in `outputs/tts_final`, and rerun the checker. Repeat until status is `PASS`.
7. Weak chunks are acceptable only when the audio is complete by human listening and the mismatch is caused by accent or ASR wording.
8. Apply final silence polish with `scripts/postprocess_audio.py`:
   - Add 0.25 seconds to each internal pause when requested.
   - Add 0.5 seconds of blank audio at the beginning when requested.
   - Ensure the final trailing blank is 1.0 seconds when requested.
9. Run the checker again on the final post-processed WAV.
10. Return the final WAV path, total duration, and the latest check report path.

## Commands

Generate from a text file:

```powershell
python .\skills\gptsovits-tts\scripts\gsv_tts.py --text-file .\work\tts-input-20260610.txt --no-open
```

Generate a short patch phrase:

```powershell
python .\skills\gptsovits-tts\scripts\gsv_tts.py --text "可能是我太主动" --output-dir .\outputs\tts_patch --no-open
```

Check a WAV against the intended text:

```powershell
& "<GPT-SoVITS-root>\runtime\python.exe" `
  .\skills\gptsovits-tts\scripts\check_tts_match.py `
  --audio .\outputs\tts_final\final.wav `
  --text-file .\work\tts-input-20260610.txt `
  --model "<Faster-Whisper-model-dir>"
```

Check the newest WAV in `outputs/tts_final`:

```powershell
.\check_latest_tts.ps1
```

Add current final spacing polish:

```powershell
python .\skills\gptsovits-tts\scripts\postprocess_audio.py `
  --input .\outputs\tts_final\input.wav `
  --internal-extra 0.25 `
  --head-add 0.5 `
  --tail-min 1.0
```

Readiness test without synthesis:

```powershell
python .\skills\gptsovits-tts\scripts\gsv_tts.py --test
```

Base-model diagnosis without model switching:

```powershell
python .\skills\gptsovits-tts\scripts\gsv_tts.py --test --skip-model-switch
```

## Acceptance Bar

- The final checker status is `PASS`.
- No missing chunk, boundary omission, or repeated chunk remains.
- The source text is audibly complete, even if ASR produces accent-related substitutions.
- The final WAV is saved in `outputs/tts_final`.
- A Markdown check report is saved in `outputs/checks`.

## Rules

- Do not modify the original reference audio or reference text.
- Do not use a reference clip longer than 10 seconds for this voice.
- Prefer the GPT-SoVITS API over browser WebUI control.
- Do not discard user edits or earlier generated audio; save repaired versions with new filenames.
- Prefer keeping only final reviewed WAVs in `outputs/tts_final`; temporary segment attempts may be cleaned with `tools/cleanup_temporary_assets.ps1`.
- Do not open GPT-SoVITS WebUI/audio players automatically during routine workflow runs.
- If a script fails, report the clear error and relevant log path.
- API logs live under `logs/`; workflow logs live at `temp/gsv_tts.log`.

