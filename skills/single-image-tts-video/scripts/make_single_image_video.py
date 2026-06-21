#!/usr/bin/env python3
"""Create a subtitled MP4 from one still image and narration audio."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SKILL_ROOT.parent.parent if SKILL_ROOT.parent.name == "skills" else Path.cwd()
DEFAULT_GPTSOVITS_ROOT = Path(
    "E:/TTS/GPT-SoVITS-v2pro-20250604"
)
DEFAULT_ASR_MODEL = Path("E:/TTS/faster-whisper-small")


class VideoWorkflowError(RuntimeError):
    """User-facing video workflow error."""


@dataclass(frozen=True)
class SubtitleSegment:
    index: int
    start: float
    end: float
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Still image to use for the whole video.")
    parser.add_argument("--audio", help="Narration audio file.")
    parser.add_argument("--bgm", help="Optional background music file to mix under narration.")
    parser.add_argument(
        "--bgm-volume",
        type=float,
        default=0.12,
        help="Background music volume before ducking. Keep low to preserve speech clarity.",
    )
    parser.add_argument(
        "--bgm-start",
        type=float,
        default=0.0,
        help="Start offset in the BGM file, in seconds.",
    )
    parser.add_argument("--bgm-fade-in", type=float, default=1.5)
    parser.add_argument("--bgm-fade-out", type=float, default=3.0)
    parser.add_argument(
        "--no-bgm-ducking",
        action="store_true",
        help="Disable sidechain ducking. By default narration lowers BGM during speech.",
    )
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs" / "video"))
    parser.add_argument("--name", help="Output folder name. Defaults to a timestamp.")
    parser.add_argument("--resolution", default="1080x1920", help="Video size, for example 1080x1920.")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument(
        "--fit",
        choices=["crop", "pad"],
        default="crop",
        help="crop fills the 9:16 canvas; pad preserves the whole image with borders.",
    )
    parser.add_argument(
        "--crop-x",
        type=float,
        default=0.5,
        help="Horizontal crop anchor from 0.0 left to 1.0 right.",
    )
    parser.add_argument(
        "--crop-y",
        type=float,
        default=0.5,
        help="Vertical crop anchor from 0.0 top to 1.0 bottom.",
    )
    parser.add_argument(
        "--crop-preview-only",
        action="store_true",
        help="Only render the still-image crop preview and exit.",
    )
    parser.add_argument("--crop-preview-output", help="Crop preview PNG path.")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--asr-model", default=str(DEFAULT_ASR_MODEL), help="Local Faster-Whisper model dir.")
    parser.add_argument("--srt", help="Existing SRT to use instead of generating one.")
    parser.add_argument("--expected-text-file", help="Source text file for TTS coverage QA.")
    parser.add_argument("--expected-text", help="Source text for TTS coverage QA.")
    parser.add_argument("--ffmpeg", help="ffmpeg.exe path.")
    parser.add_argument("--ffprobe", help="ffprobe.exe path.")
    parser.add_argument("--no-burn-subtitles", action="store_true", help="Keep SRT sidecar only.")
    parser.add_argument(
        "--subtitle-max-chars",
        type=int,
        default=14,
        help="Maximum subtitle characters per rendered line after punctuation cleanup.",
    )
    parser.add_argument(
        "--keep-subtitle-punctuation",
        action="store_true",
        help="Do not remove punctuation from subtitle text.",
    )
    parser.add_argument("--require-tts-pass", action="store_true", help="Fail if the TTS checker fails.")
    parser.add_argument("--keep-existing", action="store_true", help="Do not delete an existing output folder.")
    parser.add_argument(
        "--keep-work-assets",
        action="store_true",
        help="Keep copied source image, narration audio, BGM, and SRT sidecar in the output folder.",
    )
    parser.add_argument(
        "--keep-premiere-aux",
        action="store_true",
        help="Create and keep the premiere_aux folder with editable/import helper copies.",
    )
    return parser.parse_args()


def resolve_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def find_runtime_tool(name: str, explicit: str | None) -> Path:
    if explicit:
        path = resolve_path(explicit)
        if path.exists():
            return path
        raise VideoWorkflowError(f"{name} does not exist: {path}")

    candidate = DEFAULT_GPTSOVITS_ROOT / "runtime" / f"{name}.exe"
    if candidate.exists():
        return candidate

    on_path = shutil.which(name)
    if on_path:
        return Path(on_path).resolve()
    raise VideoWorkflowError(f"Could not find {name}. Pass --{name} <path>.")


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", value)
    if not match:
        raise VideoWorkflowError(f"Invalid resolution: {value}")
    width = int(match.group(1))
    height = int(match.group(2))
    if width < 16 or height < 16:
        raise VideoWorkflowError("Resolution is too small.")
    return width, height


def clamp_anchor(value: float, name: str) -> float:
    if value < 0.0 or value > 1.0:
        raise VideoWorkflowError(f"{name} must be between 0.0 and 1.0.")
    return value


def image_fit_filters(width: int, height: int, fit: str, crop_x: float, crop_y: float) -> list[str]:
    if fit == "pad":
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
        ]
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}:(iw-ow)*{crop_x:.4f}:(ih-oh)*{crop_y:.4f}",
        "setsar=1",
    ]


def render_crop_preview(
    *,
    ffmpeg: Path,
    image: Path,
    output: Path,
    width: int,
    height: int,
    fit: str,
    crop_x: float,
    crop_y: float,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(image),
        "-vf",
        ",".join(image_fit_filters(width, height, fit, crop_x, crop_y)),
        "-frames:v",
        "1",
        str(output),
    ]
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    (output.parent / "crop_preview_ffmpeg_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output.parent / "crop_preview_ffmpeg_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise VideoWorkflowError(f"Crop preview render failed:\n{completed.stderr[-3000:]}")
    return output


def prepare_output_dir(base_dir: Path, name: str | None, keep_existing: bool) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = base_dir / (name or f"single_image_video_{timestamp}")
    if out_dir.exists() and not keep_existing:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "premiere_aux").mkdir(exist_ok=True)
    return out_dir


def copy_input(path: Path, target: Path) -> Path:
    if not path.exists():
        raise VideoWorkflowError(f"Input file does not exist: {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def remove_file_if_exists(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def remove_dir_if_exists(path: Path) -> None:
    try:
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass


def probe_duration(ffprobe: Path, media: Path) -> float:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media),
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if completed.returncode != 0:
        raise VideoWorkflowError(f"ffprobe failed:\n{completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    duration = float(payload["format"]["duration"])
    if duration <= 0:
        raise VideoWorkflowError(f"Invalid media duration: {duration}")
    return duration


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: list[SubtitleSegment], path: Path) -> None:
    blocks: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        blocks.append(
            "\n".join(
                [
                    str(segment.index),
                    f"{srt_time(segment.start)} --> {srt_time(segment.end)}",
                    text,
                ]
            )
        )
    if not blocks:
        raise VideoWorkflowError("No subtitle text was produced.")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def is_punctuation(char: str) -> bool:
    return unicodedata.category(char).startswith("P")


def subtitle_phrase_chunks(text: str, *, keep_punctuation: bool) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for char in unicodedata.normalize("NFKC", text):
        if is_punctuation(char):
            if keep_punctuation:
                current.append(char)
            chunk = re.sub(r"\s+", " ", "".join(current)).strip()
            if chunk:
                chunks.append(chunk)
            current = []
            continue
        current.append(char)
    chunk = re.sub(r"\s+", " ", "".join(current)).strip()
    if chunk:
        chunks.append(chunk)
    return chunks


def split_long_chunk(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    preferred_tokens = [
        "会不会",
        "都当作",
        "并不会",
        "让",
        "故作",
        "都",
        "也",
        "就",
        "只会",
        "连",
    ]
    candidates: list[int] = []
    for token in preferred_tokens:
        start = text.find(token)
        while start >= 0:
            if 4 <= start <= max_chars and len(text) - start >= 4:
                candidates.append(start)
            start = text.find(token, start + 1)

    if candidates:
        midpoint = len(text) / 2
        split_at = min(candidates, key=lambda pos: (abs(pos - midpoint), abs(pos - max_chars)))
        return split_long_chunk(text[:split_at], max_chars) + split_long_chunk(text[split_at:], max_chars)

    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def wrap_subtitle_text(text: str, max_chars: int, *, keep_punctuation: bool) -> str:
    max_chars = max(4, max_chars)
    chunks = subtitle_phrase_chunks(text, keep_punctuation=keep_punctuation)
    lines: list[str] = []
    current = ""
    for chunk in chunks:
        if len(chunk) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(split_long_chunk(chunk, max_chars))
            continue
        if not current:
            current = chunk
        elif len(current) + len(chunk) <= max_chars:
            current += chunk
        else:
            lines.append(current)
            current = chunk
    if current:
        lines.append(current)
    return "\n".join(lines)


def format_subtitle_text(text: str, *, max_chars: int, keep_punctuation: bool) -> str:
    return wrap_subtitle_text(text.strip(), max_chars, keep_punctuation=keep_punctuation)


def sanitize_srt(path: Path, *, max_chars: int, keep_punctuation: bool) -> None:
    raw = path.read_text(encoding="utf-8-sig")
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        index = lines[0]
        timing = lines[1]
        text = format_subtitle_text(
            "".join(lines[2:]),
            max_chars=max_chars,
            keep_punctuation=keep_punctuation,
        )
        if text:
            blocks.append("\n".join([index, timing, text]))
    if not blocks:
        raise VideoWorkflowError("No subtitle text remained after formatting.")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def generate_srt(audio: Path, model_path: Path, language: str, output: Path) -> list[SubtitleSegment]:
    if not model_path.exists():
        raise VideoWorkflowError(f"Faster-Whisper model does not exist: {model_path}")
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise VideoWorkflowError(
            "faster_whisper is not installed in this Python. Run with the GPT-SoVITS runtime Python."
        ) from exc

    model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    raw_segments, _info = model.transcribe(str(audio), language=language, vad_filter=False)
    segments: list[SubtitleSegment] = []
    for index, segment in enumerate(raw_segments, start=1):
        text = str(segment.text).strip()
        if text:
            segments.append(SubtitleSegment(index=index, start=float(segment.start), end=float(segment.end), text=text))
    write_srt(segments, output)
    return segments


def read_srt_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        lines.append(stripped)
    return "".join(lines)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def read_expected_text(args: argparse.Namespace) -> str | None:
    if args.expected_text:
        return args.expected_text.strip()
    if args.expected_text_file:
        path = resolve_path(args.expected_text_file)
        return path.read_text(encoding="utf-8-sig").strip()
    return None


def levenshtein_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1] if previous else len(b)


def run_tts_checker(args: argparse.Namespace, audio: Path, out_dir: Path) -> dict[str, Any] | None:
    if not args.expected_text and not args.expected_text_file:
        return None

    checker = PROJECT_ROOT / "skills" / "gptsovits-tts" / "scripts" / "check_tts_match.py"
    if not checker.exists():
        return {"status": "SKIPPED", "reason": f"checker not found: {checker}"}

    command = [
        sys.executable,
        str(checker),
        "--audio",
        str(audio),
        "--model",
        args.asr_model,
        "--json",
        "--report",
        str(out_dir / "tts_match_report.md"),
    ]
    if args.expected_text_file:
        command.extend(["--text-file", str(resolve_path(args.expected_text_file))])
    else:
        command.extend(["--text", args.expected_text])

    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    (out_dir / "tts_checker_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (out_dir / "tts_checker_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        result: dict[str, Any] = {"status": "ERROR", "stderr": completed.stderr.strip()}
    else:
        json_start = completed.stdout.find("{")
        result = json.loads(completed.stdout[json_start:]) if json_start >= 0 else {"status": "UNKNOWN"}
    if args.require_tts_pass and result.get("status") != "PASS":
        raise VideoWorkflowError(f"TTS checker did not pass: {result.get('status')}")
    return result


def render_video(
    *,
    ffmpeg: Path,
    image: Path,
    audio: Path,
    bgm: Path | None,
    srt: Path,
    output: Path,
    width: int,
    height: int,
    fps: int,
    duration: float,
    burn_subtitles: bool,
    fit: str,
    crop_x: float,
    crop_y: float,
    bgm_volume: float,
    bgm_start: float,
    bgm_fade_in: float,
    bgm_fade_out: float,
    bgm_ducking: bool,
) -> tuple[Path, list[str], bool]:
    vf_parts = image_fit_filters(width, height, fit, crop_x, crop_y)
    if burn_subtitles:
        style = (
            "FontName=Microsoft YaHei,"
            "Bold=1,"
            "FontSize=12,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,"
            "Outline=1,"
            "Shadow=0,"
            "Alignment=2,"
            "MarginL=100,"
            "MarginR=100,"
            "MarginV=80"
        )
        vf_parts.append(f"subtitles=subtitles.srt:force_style='{style}'")
    video_filter = ",".join(vf_parts)

    attempts: list[tuple[str, list[str], subprocess.CompletedProcess[str]]] = []
    for video_codec in ("libx264", "mpeg4"):
        command = [
            str(ffmpeg),
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            image.name,
            "-i",
            audio.name,
        ]
        if bgm:
            command.extend(["-stream_loop", "-1", "-i", bgm.name])
            fade_out_start = max(0.0, duration - max(0.0, bgm_fade_out))
            bgm_chain = (
                f"[2:a]atrim=start={max(0.0, bgm_start):.3f}:duration={duration:.3f},"
                "asetpts=PTS-STARTPTS,"
                f"volume={bgm_volume:.4f},"
                f"afade=t=in:st=0:d={max(0.0, bgm_fade_in):.3f},"
                f"afade=t=out:st={fade_out_start:.3f}:d={max(0.0, bgm_fade_out):.3f}[bgm]"
            )
            if bgm_ducking:
                audio_filter = (
                    f"[0:v]{video_filter}[v];"
                    f"{bgm_chain};"
                    "[bgm][1:a]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[ducked];"
                    "[1:a][ducked]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.95[a]"
                )
            else:
                audio_filter = (
                    f"[0:v]{video_filter}[v];"
                    f"{bgm_chain};"
                    "[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.95[a]"
                )
            command.extend(
                [
                    "-filter_complex",
                    audio_filter,
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                ]
            )
        else:
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-vf",
                    video_filter,
                ]
            )
        command.extend(["-t", f"{duration:.3f}", "-c:v", video_codec])
        if video_codec == "libx264":
            command.extend(["-preset", "medium", "-tune", "stillimage"])
        else:
            command.extend(["-q:v", "3"])
        command.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                output.name,
            ]
        )
        completed = subprocess.run(
            command,
            cwd=str(output.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        attempts.append((video_codec, command, completed))
        if completed.returncode == 0:
            (output.parent / "ffmpeg_stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output.parent / "ffmpeg_stderr.txt").write_text(completed.stderr, encoding="utf-8")
            return output, command, burn_subtitles

    video_codec, command, completed = attempts[-1]
    (output.parent / "ffmpeg_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output.parent / "ffmpeg_stderr.txt").write_text(
        "\n\n".join(
            f"=== {codec} ===\n{result.stderr}"
            for codec, _cmd, result in attempts
        ),
        encoding="utf-8",
    )

    if burn_subtitles:
        fallback = output.with_name(output.stem + "_sidecar_subtitles.mp4")
        fallback_output, fallback_command, burned = render_video(
            ffmpeg=ffmpeg,
            image=image,
            audio=audio,
            bgm=bgm,
            srt=srt,
            output=fallback,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            burn_subtitles=False,
            fit=fit,
            crop_x=crop_x,
            crop_y=crop_y,
            bgm_volume=bgm_volume,
            bgm_start=bgm_start,
            bgm_fade_in=bgm_fade_in,
            bgm_fade_out=bgm_fade_out,
            bgm_ducking=bgm_ducking,
        )
        (output.parent / "ffmpeg_burn_subtitles_failed.txt").write_text(
            completed.stderr,
            encoding="utf-8",
        )
        return fallback_output, fallback_command, burned

    raise VideoWorkflowError(f"ffmpeg failed:\n{completed.stderr[-3000:]}")


def write_qa_report(
    out_dir: Path,
    *,
    audio_duration: float,
    video_duration: float,
    srt_path: Path,
    expected_text: str | None,
    tts_check: dict[str, Any] | None,
    burned_subtitles: bool,
) -> Path:
    srt_text = read_srt_text(srt_path)
    lines = [
        "# Single Image TTS Video QA",
        "",
        f"- Audio duration: {audio_duration:.3f}s",
        f"- Video duration: {video_duration:.3f}s",
        f"- Burned subtitles: {'yes' if burned_subtitles else 'no'}",
        f"- SRT: `{srt_path}`",
        "",
        "## SRT Text",
        "",
        srt_text,
        "",
    ]
    if expected_text:
        expected_norm = normalize_text(expected_text)
        srt_norm = normalize_text(srt_text)
        distance = levenshtein_distance(expected_norm, srt_norm)
        cer = distance / max(1, len(expected_norm))
        lines.extend(
            [
                "## Expected Text Check",
                "",
                f"- SRT CER vs expected text: {cer:.2%} ({distance}/{len(expected_norm)} chars)",
                "",
            ]
        )
    if tts_check is not None:
        lines.extend(
            [
                "## TTS Audio Check",
                "",
                f"- Status: {tts_check.get('status')}",
                f"- Report: `{tts_check.get('report', out_dir / 'tts_match_report.md')}`",
                "",
            ]
        )
    report = out_dir / "subtitle_qa.md"
    report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report


def main() -> int:
    args = parse_args()
    try:
        ffmpeg = find_runtime_tool("ffmpeg", args.ffmpeg)
        ffprobe = find_runtime_tool("ffprobe", args.ffprobe)
        image_src = resolve_path(args.image)
        output_base = resolve_path(args.output_dir)
        out_dir = prepare_output_dir(output_base, args.name, args.keep_existing)
        width, height = parse_resolution(args.resolution)
        crop_x = clamp_anchor(args.crop_x, "--crop-x")
        crop_y = clamp_anchor(args.crop_y, "--crop-y")
        if args.bgm_volume < 0.0 or args.bgm_volume > 1.0:
            raise VideoWorkflowError("--bgm-volume must be between 0.0 and 1.0.")
        if args.bgm_start < 0.0:
            raise VideoWorkflowError("--bgm-start must be 0 or greater.")
        if args.bgm_fade_in < 0.0 or args.bgm_fade_out < 0.0:
            raise VideoWorkflowError("--bgm-fade-in and --bgm-fade-out must be 0 or greater.")

        image_work = copy_input(image_src, out_dir / f"source_image{image_src.suffix.lower()}")
        crop_preview = render_crop_preview(
            ffmpeg=ffmpeg,
            image=image_work,
            output=resolve_path(args.crop_preview_output)
            if args.crop_preview_output
            else out_dir / "crop_preview.png",
            width=width,
            height=height,
            fit=args.fit,
            crop_x=crop_x,
            crop_y=crop_y,
        )
        if args.crop_preview_only:
            manifest = {
                "crop_preview": str(crop_preview),
                "image": str(image_work),
                "resolution": f"{width}x{height}",
                "fps": args.fps,
                "fit": args.fit,
                "crop_x": crop_x,
                "crop_y": crop_y,
                "ffmpeg": str(ffmpeg),
            }
            (out_dir / "crop_preview_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0

        if not args.audio:
            raise VideoWorkflowError("--audio is required unless --crop-preview-only is used.")
        audio_src = resolve_path(args.audio)
        audio_work = copy_input(audio_src, out_dir / f"narration{audio_src.suffix.lower()}")
        bgm_work: Path | None = None
        if args.bgm:
            bgm_src = resolve_path(args.bgm)
            bgm_work = copy_input(bgm_src, out_dir / f"bgm{bgm_src.suffix.lower()}")
        srt_work = out_dir / "subtitles.srt"
        if args.srt:
            copy_input(resolve_path(args.srt), srt_work)
        else:
            generate_srt(audio_work, resolve_path(args.asr_model), args.language, srt_work)
        sanitize_srt(
            srt_work,
            max_chars=args.subtitle_max_chars,
            keep_punctuation=args.keep_subtitle_punctuation,
        )

        audio_duration = probe_duration(ffprobe, audio_work)
        video_path, ffmpeg_command, burned = render_video(
            ffmpeg=ffmpeg,
            image=image_work,
            audio=audio_work,
            bgm=bgm_work,
            srt=srt_work,
            output=out_dir / "preview.mp4",
            width=width,
            height=height,
            fps=args.fps,
            duration=audio_duration,
            burn_subtitles=not args.no_burn_subtitles,
            fit=args.fit,
            crop_x=crop_x,
            crop_y=crop_y,
            bgm_volume=args.bgm_volume,
            bgm_start=args.bgm_start,
            bgm_fade_in=args.bgm_fade_in,
            bgm_fade_out=args.bgm_fade_out,
            bgm_ducking=not args.no_bgm_ducking,
        )
        video_duration = probe_duration(ffprobe, video_path)

        expected_text = read_expected_text(args)
        tts_check = run_tts_checker(args, audio_work, out_dir)
        qa_report = write_qa_report(
            out_dir,
            audio_duration=audio_duration,
            video_duration=video_duration,
            srt_path=srt_work,
            expected_text=expected_text,
            tts_check=tts_check,
            burned_subtitles=burned,
        )

        aux_dir = out_dir / "premiere_aux"
        if args.keep_premiere_aux:
            copy_input(image_work, aux_dir / image_work.name)
            copy_input(audio_work, aux_dir / audio_work.name)
            if bgm_work:
                copy_input(bgm_work, aux_dir / bgm_work.name)
            copy_input(srt_work, aux_dir / srt_work.name)
            copy_input(crop_preview, aux_dir / crop_preview.name)
        else:
            remove_dir_if_exists(aux_dir)
        manifest = {
            "video": str(video_path),
            "image": str(image_work),
            "audio": str(audio_work),
            "bgm": str(bgm_work) if bgm_work else None,
            "srt": str(srt_work),
            "qa_report": str(qa_report),
            "duration": audio_duration,
            "resolution": f"{width}x{height}",
            "fps": args.fps,
            "fit": args.fit,
            "crop_preview": str(crop_preview),
            "crop_x": crop_x,
            "crop_y": crop_y,
            "bgm_volume": args.bgm_volume if bgm_work else None,
            "bgm_start": args.bgm_start if bgm_work else None,
            "bgm_fade_in": args.bgm_fade_in if bgm_work else None,
            "bgm_fade_out": args.bgm_fade_out if bgm_work else None,
            "bgm_ducking": (not args.no_bgm_ducking) if bgm_work else None,
            "burned_subtitles": burned,
            "ffmpeg": str(ffmpeg),
            "ffprobe": str(ffprobe),
            "work_assets_kept": args.keep_work_assets,
            "premiere_aux_kept": args.keep_premiere_aux,
        }
        if not args.keep_work_assets:
            deleted_assets: list[str] = []
            for path in [image_work, audio_work, bgm_work]:
                if path is not None and path.exists():
                    deleted_assets.append(str(path))
                if path is not None:
                    remove_file_if_exists(path)
            if burned:
                if srt_work.exists():
                    deleted_assets.append(str(srt_work))
                remove_file_if_exists(srt_work)
                manifest["srt"] = None
            manifest["image"] = None
            manifest["audio"] = None
            manifest["bgm"] = None
            manifest["deleted_work_assets"] = deleted_assets
        (out_dir / "project_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "ffmpeg_command.txt").write_text(
            subprocess.list2cmdline(ffmpeg_command) + "\n",
            encoding="utf-8",
        )

        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except VideoWorkflowError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

