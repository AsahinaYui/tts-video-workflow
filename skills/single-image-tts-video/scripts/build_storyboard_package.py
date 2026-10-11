#!/usr/bin/env python3
"""Build an offline storyboard package from segmented TTS outputs.

The package keeps one timeline as the source of truth:

- segment timings come from accepted segmented TTS audio
- shots bind images to segment ids
- subtitles are optional and derived from the same timeline
- rendered previews and editor handoff files are derived artifacts
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
import wave
from dataclasses import asdict, dataclass, field
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
    "E:/GPT-SoVITS/GPT-SoVITS-v2pro-20250604/GPT-SoVITS-v2pro-20250604"
)


class StoryboardError(RuntimeError):
    """User-facing storyboard workflow error."""


@dataclass
class SegmentRow:
    index: int
    id: str
    text: str
    audio: str
    start: float
    end: float
    duration: float
    source_duration: float
    status: str = "UNKNOWN"
    cer: float | None = None
    repair_target: str = ""


@dataclass
class ShotRow:
    id: str
    image: str
    segment_ids: list[str]
    spoken_start: float
    spoken_end: float
    visual_start: float
    visual_end: float
    fit: str = "crop"
    crop_x: float = 0.5
    crop_y: float = 0.5
    notes: str = ""
    copied_image: str | None = None


@dataclass
class TimelineBuild:
    source_kind: str
    source_path: Path
    audio_path: Path | None
    segments: list[SegmentRow]
    audio_duration: float
    computed_duration: float
    timing: dict[str, Any]
    shots: list[ShotRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--webui-job", help="Local video WebUI job folder.")
    source.add_argument("--segments-manifest", help="Segmented TTS manifest.json.")
    source.add_argument("--timeline", help="Existing timeline.json to repackage or render.")
    parser.add_argument("--audio", help="Override full narration audio path.")
    parser.add_argument("--storyboard", help="Storyboard JSON mapping images to segment ids.")
    parser.add_argument(
        "--segment-id-map",
        help="Optional JSON/CSV/TXT map assigning display ids such as 1.1, 1.2 to segment indexes.",
    )
    parser.add_argument(
        "--image",
        action="append",
        dest="images",
        help="Image path. If --storyboard is omitted, images are assigned in order.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "outputs" / "storyboard"),
        help="Base folder for generated packages.",
    )
    parser.add_argument("--name", help="Package folder name. Defaults to a timestamp.")
    parser.add_argument(
        "--subtitle-mode",
        choices=["off", "sidecar", "burn"],
        default="off",
        help="off writes no SRT, sidecar writes SRT, burn also burns subtitles into preview render.",
    )
    parser.add_argument("--subtitle-max-chars", type=int, default=14)
    parser.add_argument("--keep-subtitle-punctuation", action="store_true")
    parser.add_argument(
        "--render-preview",
        action="store_true",
        help="Render a local MP4 preview from the storyboard image track and narration audio.",
    )
    parser.add_argument("--resolution", default="1080x1920")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--fit", choices=["crop", "pad"], default="crop")
    parser.add_argument("--crop-x", type=float, default=0.5)
    parser.add_argument("--crop-y", type=float, default=0.5)
    parser.add_argument("--ffmpeg", help="ffmpeg.exe path.")
    parser.add_argument("--ffprobe", help="ffprobe.exe path.")
    parser.add_argument(
        "--manifest-head",
        type=float,
        default=0.5,
        help="Head silence used when deriving timing from older segment manifests.",
    )
    parser.add_argument(
        "--manifest-gap",
        type=float,
        default=0.6,
        help="Gap between spoken segments for older manifests, usually raw 0.35 + postprocess 0.25.",
    )
    parser.add_argument(
        "--manifest-tail",
        type=float,
        default=1.0,
        help="Tail silence used when deriving timing from older segment manifests.",
    )
    parser.add_argument(
        "--manifest-trim-mode",
        choices=["silence", "none"],
        default="silence",
        help="Older scripted manifests usually trim segment edges before assembly.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not delete an existing output folder with the same name.",
    )
    return parser.parse_args()


def resolve_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", value or "")
    if not match:
        raise StoryboardError(f"Invalid resolution: {value}")
    width = int(match.group(1))
    height = int(match.group(2))
    if width < 16 or height < 16:
        raise StoryboardError("Resolution is too small.")
    return width, height


def clamp_anchor(value: float, name: str) -> float:
    if value < 0.0 or value > 1.0:
        raise StoryboardError(f"{name} must be between 0.0 and 1.0.")
    return value


def find_runtime_tool(name: str, explicit: str | None) -> Path:
    if explicit:
        path = resolve_path(explicit)
        if path.exists():
            return path
        raise StoryboardError(f"{name} does not exist: {path}")
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path).resolve()
    candidate = DEFAULT_GPTSOVITS_ROOT / "runtime" / f"{name}.exe"
    if candidate.exists():
        return candidate
    raise StoryboardError(f"Could not find {name}. Pass --{name} <path>.")


def prepare_output_dir(base_dir: Path, name: str | None, keep_existing: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = base_dir / (name or f"storyboard_{stamp}")
    if out_dir.exists() and not keep_existing:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def read_wav(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(params.nframes)
    return params, frames


def wav_duration(path: Path) -> float | None:
    if path.suffix.lower() != ".wav" or not path.exists():
        return None
    try:
        params, _frames = read_wav(path)
        if params.framerate > 0:
            return params.nframes / float(params.framerate)
    except Exception:
        return None
    return None


def probe_duration(ffprobe: Path | None, media: Path) -> float | None:
    wav = wav_duration(media)
    if wav is not None:
        return wav
    if not ffprobe or not media.exists():
        return None
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
        return None
    try:
        payload = json.loads(completed.stdout)
        return float(payload["format"]["duration"])
    except Exception:
        return None


def frame_slice(frames: bytes, frame_size: int, start: int, end: int) -> bytes:
    return frames[max(0, start) * frame_size : max(0, end) * frame_size]


def chunk_dbfs(chunk: bytes, sampwidth: int) -> float:
    if not chunk:
        return float("-inf")
    if sampwidth == 1:
        values = [sample - 128 for sample in chunk]
        max_amp = 128.0
    elif sampwidth == 2:
        count = len(chunk) // 2
        values = struct.unpack("<" + "h" * count, chunk[: count * 2])
        max_amp = 32768.0
    elif sampwidth == 4:
        count = len(chunk) // 4
        values = struct.unpack("<" + "i" * count, chunk[: count * 4])
        max_amp = 2147483648.0
    else:
        raise StoryboardError(f"Unsupported WAV sample width: {sampwidth}")
    if not values:
        return float("-inf")
    rms = math.sqrt(sum(float(value) * float(value) for value in values) / len(values))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / max_amp)


def trimmed_wav_duration(path: Path, threshold_dbfs: float = -56.0, pad_seconds: float = 0.08) -> float:
    params, frames = read_wav(path)
    frame_size = params.nchannels * params.sampwidth
    total_frames = len(frames) // frame_size
    chunk_frames = max(1, int(round(params.framerate * 0.01)))
    voiced_starts: list[int] = []
    for start in range(0, total_frames, chunk_frames):
        end = min(total_frames, start + chunk_frames)
        if chunk_dbfs(frame_slice(frames, frame_size, start, end), params.sampwidth) > threshold_dbfs:
            voiced_starts.append(start)
    if not voiced_starts:
        return total_frames / params.framerate
    pad = int(round(params.framerate * pad_seconds))
    start = max(0, voiced_starts[0] - pad)
    end = min(total_frames, voiced_starts[-1] + chunk_frames + pad)
    return (end - start) / params.framerate


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_segment_id_map(path: Path | None, count: int) -> dict[int, str]:
    if path is None:
        return {index: str(index) for index in range(1, count + 1)}
    if not path.exists():
        raise StoryboardError(f"Segment id map does not exist: {path}")

    mapping: dict[int, str] = {}
    if path.suffix.lower() == ".json":
        payload = read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
            payload = payload["segments"]
        if isinstance(payload, list):
            for offset, item in enumerate(payload, start=1):
                if isinstance(item, str):
                    mapping[offset] = item.strip()
                elif isinstance(item, dict):
                    index = int(item.get("index") or offset)
                    segment_id = str(item.get("id") or item.get("segment_id") or "").strip()
                    if segment_id:
                        mapping[index] = segment_id
        elif isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).isdigit():
                    mapping[int(key)] = str(value).strip()
        else:
            raise StoryboardError(f"Unsupported segment id map JSON shape: {path}")
    elif path.suffix.lower() in (".csv", ".tsv"):
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for offset, row in enumerate(reader, start=1):
                index_value = row.get("index") or row.get("segment_index") or offset
                segment_id = row.get("id") or row.get("segment_id") or row.get("display_id")
                if segment_id:
                    mapping[int(index_value)] = str(segment_id).strip()
    else:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        mapping = {index: line for index, line in enumerate(lines, start=1)}

    for index in range(1, count + 1):
        mapping.setdefault(index, str(index))
    duplicate_ids = sorted({value for value in mapping.values() if list(mapping.values()).count(value) > 1})
    if duplicate_ids:
        raise StoryboardError("Duplicate segment ids in map: " + ", ".join(duplicate_ids))
    return mapping


def parse_webui_segment_report(report_path: Path) -> tuple[list[dict[str, Any]], float]:
    if not report_path.exists():
        raise StoryboardError(f"WebUI segmented repair report not found: {report_path}")
    text = report_path.read_text(encoding="utf-8", errors="replace")
    padding = 0.0
    padding_match = re.search(r"^Padding:\s+([0-9.]+)s\s*$", text, re.MULTILINE)
    if padding_match:
        padding = float(padding_match.group(1))

    segments: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        header = re.match(r"^## Segment\s+(\d+)\s+attempt\s+(\d+)", line)
        if header:
            index = int(header.group(1))
            current = {"attempt": int(header.group(2)), "expected": "", "audio": None, "cer": 999.0, "passed": False}
            segments.setdefault(index, {"index": index, "attempts": []})["attempts"].append(current)
            continue
        accepted = re.match(r"^- Accepted audio:\s+`(.*)`\s*$", line)
        if accepted and segments:
            last_index = sorted(segments)[-1]
            segments[last_index]["accepted_audio"] = Path(accepted.group(1))
            continue
        accepted_status = re.match(r"^- Accepted status:\s+(PASS|FALLBACK)", line)
        if accepted_status and segments:
            last_index = sorted(segments)[-1]
            segments[last_index]["accepted_passed"] = accepted_status.group(1) == "PASS"
            continue
        if current is None:
            continue
        expected = re.match(r"^- Expected:\s+`(.*)`\s*$", line)
        if expected:
            current["expected"] = expected.group(1)
            continue
        audio = re.match(r"^- Audio:\s+`(.*)`\s*$", line)
        if audio:
            current["audio"] = Path(audio.group(1))
            continue
        result = re.match(r"^- Result:\s+(PASS|RETRY|FALLBACK)\s+\((.*)\)", line)
        if result:
            current["passed"] = result.group(1) == "PASS"
            cer_match = re.search(r"CER=([0-9.]+)%", result.group(2))
            if cer_match:
                current["cer"] = float(cer_match.group(1)) / 100.0

    parsed: list[dict[str, Any]] = []
    for index in sorted(segments):
        entry = segments[index]
        accepted_audio = entry.get("accepted_audio")
        attempts = entry.get("attempts", [])
        expected = str((attempts[0].get("expected") if attempts else "") or "")
        if accepted_audio and Path(accepted_audio).exists():
            parsed.append(
                {
                    "index": index,
                    "text": expected,
                    "audio": Path(accepted_audio).resolve(),
                    "status": "PASS" if entry.get("accepted_passed") else "FALLBACK",
                    "cer": 0.0 if entry.get("accepted_passed") else None,
                }
            )
            continue
        usable = [attempt for attempt in attempts if attempt.get("audio") and Path(attempt["audio"]).exists()]
        if not usable:
            raise StoryboardError(f"No usable audio found for WebUI segment {index:03d}")
        accepted = next((attempt for attempt in usable if attempt.get("passed")), None)
        if accepted is None:
            accepted = min(usable, key=lambda item: float(item.get("cer", 999.0)))
        parsed.append(
            {
                "index": index,
                "text": str(accepted.get("expected") or expected),
                "audio": Path(accepted["audio"]).resolve(),
                "status": "PASS" if accepted.get("passed") else "FALLBACK",
                "cer": float(accepted.get("cer", 999.0)),
            }
        )
    if not parsed:
        raise StoryboardError(f"No segments parsed from report: {report_path}")
    return parsed, padding


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def chunks_signature(chunks: list[str]) -> str:
    payload = "\n".join(normalize_text(chunk) for chunk in chunks)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def load_webui_current_sequence(job: Path, raw_rows: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]] | None, list[str]]:
    sequence_path = job / "tts" / "sentence_patch" / "current_sequence.json"
    if not sequence_path.exists():
        return None, []
    warnings: list[str] = []
    try:
        payload = read_json(sequence_path)
    except Exception as exc:
        return None, [f"Could not read current audio sequence: {exc}"]
    if not isinstance(payload, dict):
        return None, [f"Current audio sequence has unsupported shape: {sequence_path}"]

    actual_signature = str(payload.get("chunks_signature") or "")
    if raw_rows:
        expected_signature = chunks_signature([str(row.get("text") or "") for row in raw_rows])
        if actual_signature and actual_signature != expected_signature:
            warnings.append(
                "Current audio sequence text signature differs from the segmented report; "
                "using the current sequence because it may be a finer manual patch index."
            )
    else:
        warnings.append("Using current audio sequence without a segmented repair report.")

    entries = payload.get("entries") or []
    if not isinstance(entries, list) or not entries:
        return None, []

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for offset, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            warnings.append(f"Ignored current audio sequence entry {offset}: not an object.")
            continue
        raw_audio = str(entry.get("audio") or "").strip()
        if not raw_audio:
            warnings.append(f"Ignored current audio sequence entry {offset}: missing audio.")
            continue
        audio = Path(raw_audio)
        if not audio.is_absolute():
            audio = (job / audio).resolve()
        if not audio.exists():
            warnings.append(f"Ignored current audio sequence entry {offset}: audio does not exist: {audio}")
            continue

        chunk_index = int(entry.get("chunk_index") or offset)
        locator = str(entry.get("locator") or "").strip()
        default_id = locator or str(chunk_index)
        if default_id in seen_ids:
            default_id = f"{default_id}.{offset}"
        seen_ids.add(default_id)
        rows.append(
            {
                "index": len(rows) + 1,
                "id": default_id,
                "source_index": chunk_index,
                "text": str(entry.get("text") or ""),
                "audio": audio.resolve(),
                "status": str(entry.get("kind") or "current").upper(),
                "cer": None,
                "repair_target": locator or str(chunk_index),
            }
        )
        if entry.get("start") is not None and entry.get("end") is not None:
            try:
                rows[-1]["start"] = float(entry["start"])
                rows[-1]["end"] = float(entry["end"])
            except Exception:
                warnings.append(f"Ignored invalid explicit timing on current audio sequence entry {offset}.")

    if not rows:
        return None, warnings
    warnings.append(f"Using current audio sequence for storyboard timing: {sequence_path}")
    return rows, warnings


def infer_sequence_padding(rows: list[dict[str, Any]], audio_path: Path | None, ffprobe: Path | None, fallback: float) -> tuple[float, str, list[str]]:
    warnings: list[str] = []
    if not rows or not audio_path or not audio_path.exists():
        return fallback, "report_padding", warnings
    audio_duration = probe_duration(ffprobe, audio_path)
    if not audio_duration:
        return fallback, "report_padding", warnings
    durations: list[float] = []
    for row in rows:
        duration = probe_duration(ffprobe, Path(row["audio"])) or 0.0
        if duration <= 0.0:
            return fallback, "report_padding", [f"Could not infer current sequence padding because a segment duration is unreadable: {row['audio']}"]
        durations.append(duration)
    inferred = (audio_duration - sum(durations)) / (2 * len(durations))
    if -0.025 <= inferred <= 1.5:
        return max(0.0, inferred), "inferred_from_current_audio", warnings
    warnings.append(
        "Could not infer current sequence padding from final audio "
        f"(duration={audio_duration:.3f}s, segments={sum(durations):.3f}s); using report padding {fallback:.3f}s."
    )
    return fallback, "report_padding", warnings


def select_webui_audio(job: Path, override: Path | None) -> Path | None:
    if override:
        return override
    candidates = [
        job / "tts" / "tts_sentence_patch_applied.wav",
        job / "tts" / "tts_secondary_auto_patched.wav",
        job / "tts" / "tts_segmented_auto_repaired.wav",
        job / "tts" / "tts.wav",
    ]
    return next((path.resolve() for path in candidates if path.exists()), None)


def build_from_webui_job(args: argparse.Namespace, ffprobe: Path | None) -> TimelineBuild:
    job = resolve_path(args.webui_job)
    if not job.exists():
        raise StoryboardError(f"WebUI job folder does not exist: {job}")
    audio_path = select_webui_audio(job, resolve_path(args.audio) if args.audio else None)
    timing_mode = "webui_per_segment_padding"
    pad_source = "segmented_report"
    warnings: list[str] = []

    sequence_rows, sequence_warnings = load_webui_current_sequence(job)
    if sequence_rows:
        raw_rows = sequence_rows
        pad_seconds, pad_source, pad_warnings = infer_sequence_padding(raw_rows, audio_path, ffprobe, 0.0)
        timing_mode = "webui_current_sequence"
        warnings.extend(sequence_warnings)
        warnings.extend(pad_warnings)
    else:
        raw_rows, report_pad_seconds = parse_webui_segment_report(job / "checks" / "tts_segmented_auto_repair.md")
        pad_seconds = report_pad_seconds
        warnings.extend(sequence_warnings)
    id_map = parse_segment_id_map(resolve_path(args.segment_id_map) if args.segment_id_map else None, len(raw_rows))

    cursor = 0.0
    segments: list[SegmentRow] = []
    use_explicit_timing = bool(raw_rows) and all(row.get("start") is not None and row.get("end") is not None for row in raw_rows)
    if use_explicit_timing:
        timing_mode = "webui_current_sequence_explicit"
        pad_source = "current_sequence"
    for row in raw_rows:
        index = int(row["index"])
        audio = Path(row["audio"])
        source_duration = probe_duration(ffprobe, audio) or 0.0
        if use_explicit_timing:
            start = max(0.0, float(row["start"]))
            end = max(start, float(row["end"]))
            duration = end - start
        else:
            start = cursor + pad_seconds
            duration = source_duration
            end = start + source_duration
        if source_duration <= 0:
            warnings.append(f"Could not read segment duration: {audio}")
        segments.append(
            SegmentRow(
                index=index,
                id=id_map.get(index, str(row.get("id") or index)),
                text=str(row.get("text") or ""),
                audio=str(audio),
                start=start,
                end=end,
                duration=duration,
                source_duration=source_duration,
                status=str(row.get("status") or "UNKNOWN"),
                cer=row.get("cer"),
                repair_target=str(row.get("repair_target") or row.get("source_index") or index),
            )
        )
        cursor = max(cursor, end) if use_explicit_timing else end + pad_seconds

    computed_duration = cursor
    audio_duration = (probe_duration(ffprobe, audio_path) if audio_path else None) or computed_duration
    return TimelineBuild(
        source_kind="webui_job",
        source_path=job,
        audio_path=audio_path,
        segments=segments,
        audio_duration=audio_duration,
        computed_duration=computed_duration,
        timing={"mode": timing_mode, "pad_seconds": pad_seconds, "pad_source": pad_source},
        warnings=warnings,
    )


def build_from_segment_manifest(args: argparse.Namespace, ffprobe: Path | None) -> TimelineBuild:
    manifest = resolve_path(args.segments_manifest)
    if not manifest.exists():
        raise StoryboardError(f"Segment manifest does not exist: {manifest}")
    payload = read_json(manifest)
    chunks = list(payload.get("chunks") or [])
    segment_paths = [Path(value) for value in payload.get("segments") or []]
    if len(chunks) != len(segment_paths):
        raise StoryboardError(f"Manifest chunk/segment count mismatch: {len(chunks)} chunks, {len(segment_paths)} audio files")

    id_map = parse_segment_id_map(resolve_path(args.segment_id_map) if args.segment_id_map else None, len(chunks))
    checks_by_index: dict[int, dict[str, Any]] = {}
    for item in payload.get("segment_checks") or []:
        try:
            checks_by_index[int(item.get("index"))] = item
        except Exception:
            continue

    audio_path = resolve_path(args.audio) if args.audio else None
    if audio_path is None:
        for key in ("final", "raw_assembled"):
            value = payload.get(key)
            if value and Path(value).exists():
                audio_path = Path(value).resolve()
                break

    cursor = max(0.0, float(args.manifest_head))
    gap = max(0.0, float(args.manifest_gap))
    tail = max(0.0, float(args.manifest_tail))
    segments: list[SegmentRow] = []
    warnings: list[str] = []
    for offset, (text, audio_value) in enumerate(zip(chunks, segment_paths), start=1):
        audio = audio_value if audio_value.is_absolute() else (manifest.parent / audio_value).resolve()
        source_duration = probe_duration(ffprobe, audio) or 0.0
        if args.manifest_trim_mode == "silence":
            try:
                duration = trimmed_wav_duration(audio)
            except Exception:
                duration = source_duration
                warnings.append(f"Could not trim segment; using full duration: {audio}")
        else:
            duration = source_duration
        check = checks_by_index.get(offset, {})
        start = cursor
        end = start + duration
        segments.append(
            SegmentRow(
                index=offset,
                id=id_map.get(offset, str(offset)),
                text=str(text),
                audio=str(audio),
                start=start,
                end=end,
                duration=duration,
                source_duration=source_duration,
                status=str(check.get("status") or "UNKNOWN"),
                cer=None,
                repair_target=str(offset),
            )
        )
        cursor = end + (gap if offset < len(chunks) else 0.0)

    computed_duration = cursor + tail
    audio_duration = (probe_duration(ffprobe, audio_path) if audio_path else None) or computed_duration
    return TimelineBuild(
        source_kind="segments_manifest",
        source_path=manifest,
        audio_path=audio_path,
        segments=segments,
        audio_duration=audio_duration,
        computed_duration=computed_duration,
        timing={
            "mode": "manifest_head_gap_tail",
            "head_seconds": args.manifest_head,
            "gap_seconds": args.manifest_gap,
            "tail_seconds": args.manifest_tail,
            "trim_mode": args.manifest_trim_mode,
        },
        warnings=warnings,
    )


def load_existing_timeline(path: Path) -> TimelineBuild:
    payload = read_json(path)
    segments = [
        SegmentRow(
            index=int(item["index"]),
            id=str(item["id"]),
            text=str(item.get("text") or ""),
            audio=str(item.get("audio") or ""),
            start=float(item["start"]),
            end=float(item["end"]),
            duration=float(item.get("duration") or (float(item["end"]) - float(item["start"]))),
            source_duration=float(item.get("source_duration") or item.get("duration") or 0.0),
            status=str(item.get("status") or "UNKNOWN"),
            cer=item.get("cer"),
            repair_target=str(item.get("repair_target") or item.get("index")),
        )
        for item in payload.get("segments") or []
    ]
    audio_path = Path(payload.get("audio", {}).get("path") or "") if payload.get("audio") else None
    if audio_path and not audio_path.exists():
        audio_path = None
    audio_duration = float(payload.get("audio", {}).get("duration") or payload.get("duration") or 0.0)
    if audio_duration <= 0 and segments:
        audio_duration = max(item.end for item in segments)
    shots = [
        ShotRow(
            id=str(item.get("id") or f"shot_{offset:03d}"),
            image=str(item.get("image") or item.get("copied_image") or ""),
            segment_ids=[str(value) for value in item.get("segment_ids") or []],
            spoken_start=float(item.get("spoken_start") or 0.0),
            spoken_end=float(item.get("spoken_end") or 0.0),
            visual_start=float(item.get("visual_start") or 0.0),
            visual_end=float(item.get("visual_end") or 0.0),
            fit=str(item.get("fit") or "crop"),
            crop_x=float(item.get("crop_x") if item.get("crop_x") is not None else 0.5),
            crop_y=float(item.get("crop_y") if item.get("crop_y") is not None else 0.5),
            notes=str(item.get("notes") or ""),
            copied_image=item.get("copied_image"),
        )
        for offset, item in enumerate(payload.get("shots") or [], start=1)
    ]
    return TimelineBuild(
        source_kind=str(payload.get("source", {}).get("kind") or "timeline"),
        source_path=path,
        audio_path=audio_path,
        segments=segments,
        audio_duration=audio_duration,
        computed_duration=float(payload.get("audio", {}).get("computed_duration") or audio_duration),
        timing=dict(payload.get("timing") or {}),
        shots=shots,
        warnings=list(payload.get("warnings") or []),
    )


def load_timeline_build(args: argparse.Namespace, ffprobe: Path | None) -> TimelineBuild:
    if args.timeline:
        build = load_existing_timeline(resolve_path(args.timeline))
        if args.audio:
            build.audio_path = resolve_path(args.audio)
            build.audio_duration = probe_duration(ffprobe, build.audio_path) or build.audio_duration
        return build
    if args.webui_job:
        return build_from_webui_job(args, ffprobe)
    if args.segments_manifest:
        return build_from_segment_manifest(args, ffprobe)
    raise StoryboardError("Pass one source: --webui-job, --segments-manifest, or --timeline.")


def load_storyboard_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        raise StoryboardError(f"Storyboard file does not exist: {path}")
    payload = read_json(path)
    if isinstance(payload, list):
        return {"shots": payload}
    if isinstance(payload, dict):
        return payload
    raise StoryboardError(f"Unsupported storyboard JSON shape: {path}")


def id_order_map(segments: list[SegmentRow]) -> dict[str, int]:
    return {segment.id: offset for offset, segment in enumerate(segments)}


def expand_segment_tokens(tokens: list[Any], segments: list[SegmentRow]) -> list[str]:
    ordered_ids = [segment.id for segment in segments]
    order = id_order_map(segments)
    expanded: list[str] = []
    for raw in tokens:
        token = str(raw).strip()
        if not token:
            continue
        if token in ("*", "all"):
            expanded.extend(ordered_ids)
            continue
        if token in order:
            expanded.append(token)
            continue
        separator = ".." if ".." in token else "-" if "-" in token else None
        if separator:
            left, right = [part.strip() for part in token.split(separator, 1)]
            if left in order and right in order:
                start = order[left]
                end = order[right]
                if start > end:
                    start, end = end, start
                expanded.extend(ordered_ids[start : end + 1])
                continue
        raise StoryboardError(f"Storyboard references unknown segment id: {token}")

    seen: set[str] = set()
    result: list[str] = []
    for segment_id in expanded:
        if segment_id not in seen:
            seen.add(segment_id)
            result.append(segment_id)
    return result


def auto_storyboard_from_images(images: list[str] | None, segments: list[SegmentRow], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not images:
        return []
    if not segments:
        raise StoryboardError("Cannot auto-assign images without segments.")
    resolved = [str(resolve_path(image)) for image in images]
    if len(resolved) == 1:
        return [{"id": "shot_001", "image": resolved[0], "segments": ["*"], "fit": args.fit, "crop_x": args.crop_x, "crop_y": args.crop_y}]
    if len(resolved) == len(segments):
        return [
            {
                "id": f"shot_{index:03d}",
                "image": image,
                "segments": [segments[index - 1].id],
                "fit": args.fit,
                "crop_x": args.crop_x,
                "crop_y": args.crop_y,
            }
            for index, image in enumerate(resolved, start=1)
        ]

    shots: list[dict[str, Any]] = []
    count = len(resolved)
    for offset, image in enumerate(resolved):
        start = round(offset * len(segments) / count)
        end = round((offset + 1) * len(segments) / count)
        ids = [segment.id for segment in segments[start:end]]
        shots.append(
            {
                "id": f"shot_{offset + 1:03d}",
                "image": image,
                "segments": ids,
                "fit": args.fit,
                "crop_x": args.crop_x,
                "crop_y": args.crop_y,
            }
        )
    return shots


def build_shots(
    storyboard_payload: dict[str, Any] | None,
    segments: list[SegmentRow],
    audio_duration: float,
    args: argparse.Namespace,
) -> tuple[list[ShotRow], list[str]]:
    raw_shots = []
    if storyboard_payload:
        raw_shots = list(storyboard_payload.get("shots") or [])
    elif args.images:
        raw_shots = auto_storyboard_from_images(args.images, segments, args)
    if not raw_shots:
        return [], ["No storyboard shots were provided; package contains timeline/audio only."]

    by_id = {segment.id: segment for segment in segments}
    shots: list[ShotRow] = []
    warnings: list[str] = []
    assigned: list[str] = []
    for offset, item in enumerate(raw_shots, start=1):
        if not isinstance(item, dict):
            raise StoryboardError("Each storyboard shot must be an object.")
        image = resolve_path(str(item.get("image") or ""))
        if not image.exists():
            raise StoryboardError(f"Storyboard image does not exist: {image}")
        segment_ids = expand_segment_tokens(list(item.get("segments") or []), segments)
        if not segment_ids:
            raise StoryboardError(f"Storyboard shot has no segments: {item.get('id') or offset}")
        shot_segments = [by_id[segment_id] for segment_id in segment_ids]
        spoken_start = min(segment.start for segment in shot_segments)
        spoken_end = max(segment.end for segment in shot_segments)
        shots.append(
            ShotRow(
                id=str(item.get("id") or f"shot_{offset:03d}"),
                image=str(image),
                segment_ids=segment_ids,
                spoken_start=spoken_start,
                spoken_end=spoken_end,
                visual_start=spoken_start,
                visual_end=spoken_end,
                fit=str(item.get("fit") or args.fit),
                crop_x=float(item.get("crop_x", args.crop_x)),
                crop_y=float(item.get("crop_y", args.crop_y)),
                notes=str(item.get("notes") or ""),
            )
        )
        assigned.extend(segment_ids)

    shots.sort(key=lambda shot: (shot.spoken_start, shot.spoken_end, shot.id))
    for offset, shot in enumerate(shots):
        shot.visual_start = 0.0 if offset == 0 else max(0.0, shot.spoken_start)
        if offset + 1 < len(shots):
            shot.visual_end = max(shot.visual_start, shots[offset + 1].spoken_start)
        else:
            shot.visual_end = max(shot.visual_start, audio_duration)

    known = {segment.id for segment in segments}
    assigned_set = set(assigned)
    missing = [segment.id for segment in segments if segment.id not in assigned_set]
    if missing:
        warnings.append("Segments without explicit storyboard assignment: " + ", ".join(missing))
    duplicate = sorted({segment_id for segment_id in assigned if assigned.count(segment_id) > 1})
    if duplicate:
        warnings.append("Segments assigned to more than one shot: " + ", ".join(duplicate))
    unknown = sorted(assigned_set - known)
    if unknown:
        warnings.append("Unknown storyboard segment ids: " + ", ".join(unknown))
    return shots, warnings


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def is_punctuation(char: str) -> bool:
    return unicodedata.category(char).startswith("P")


def clean_subtitle_text(text: str, keep_punctuation: bool) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    output = []
    for char in normalized:
        if is_punctuation(char) and not keep_punctuation:
            continue
        output.append(char)
    return re.sub(r"\s+", " ", "".join(output)).strip()


def wrap_text(text: str, max_chars: int) -> str:
    max_chars = max(4, int(max_chars or 14))
    if len(text) <= max_chars:
        return text
    lines = [text[index : index + max_chars] for index in range(0, len(text), max_chars)]
    return "\n".join(lines)


def write_srt(segments: list[SegmentRow], path: Path, *, max_chars: int, keep_punctuation: bool) -> None:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = wrap_text(clean_subtitle_text(segment.text, keep_punctuation), max_chars)
        if not text:
            continue
        blocks.append("\n".join([str(index), f"{srt_time(segment.start)} --> {srt_time(segment.end)}", text]))
    if not blocks:
        raise StoryboardError("No subtitle text was produced.")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "item"


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise StoryboardError(f"Could not find unique target name for {path}")


def copy_file(source: Path, target: Path) -> Path:
    if not source.exists():
        raise StoryboardError(f"File does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target = unique_target(target)
    shutil.copy2(source, target)
    return target.resolve()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def seconds_to_timecode(seconds: float, fps: int) -> str:
    frames_total = int(round(max(0.0, seconds) * fps))
    frames = frames_total % fps
    seconds_total = frames_total // fps
    secs = seconds_total % 60
    minutes_total = seconds_total // 60
    mins = minutes_total % 60
    hours = minutes_total // 60
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


def image_fit_filters(width: int, height: int, fit: str, crop_x: float, crop_y: float) -> list[str]:
    if fit == "pad":
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:in_range=pc:out_range=tv:out_color_matrix=bt709",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            "format=yuv420p",
        ]
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=increase:in_range=pc:out_range=tv:out_color_matrix=bt709",
        f"crop={width}:{height}:(iw-ow)*{crop_x:.4f}:(ih-oh)*{crop_y:.4f}",
        "setsar=1",
        "format=yuv420p",
    ]


def run_ffmpeg(command: list[str], cwd: Path, log_path: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "COMMAND:\n" + subprocess.list2cmdline(command) + "\n\nSTDOUT:\n" + completed.stdout + "\n\nSTDERR:\n" + completed.stderr,
        encoding="utf-8",
    )
    return completed


def render_clip(ffmpeg: Path, shot: ShotRow, output: Path, width: int, height: int, fps: int, log_path: Path) -> Path:
    duration = max(0.04, shot.visual_end - shot.visual_start)
    vf = ",".join(image_fit_filters(width, height, shot.fit, shot.crop_x, shot.crop_y))
    command = [
        str(ffmpeg),
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-t",
        f"{duration:.3f}",
        "-i",
        str(Path(shot.copied_image or shot.image)),
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        str(output),
    ]
    result = run_ffmpeg(command, output.parent, log_path)
    if result.returncode != 0:
        raise StoryboardError("Failed to render storyboard clip with libx264:\n" + result.stderr[-3000:])
    return output


def render_preview(
    ffmpeg: Path,
    out_dir: Path,
    shots: list[ShotRow],
    audio: Path,
    srt_path: Path | None,
    args: argparse.Namespace,
) -> Path:
    if not shots:
        raise StoryboardError("--render-preview requires storyboard shots.")
    if not audio or not audio.exists():
        raise StoryboardError("--render-preview requires a full narration audio path.")
    width, height = parse_resolution(args.resolution)
    clip_dir = out_dir / "tmp" / "render_clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for offset, shot in enumerate(shots, start=1):
        rendered.append(
            render_clip(
                ffmpeg,
                shot,
                clip_dir / f"shot_{offset:03d}.mp4",
                width,
                height,
                args.fps,
                out_dir / "logs" / f"render_shot_{offset:03d}.txt",
            ).resolve()
        )

    concat_list = clip_dir / "concat_list.txt"
    concat_list.write_text("".join(f"file '{str(path).replace(chr(92), '/')}'\n" for path in rendered), encoding="utf-8")
    video_track = out_dir / "video_track.mp4"
    concat_command = [
        str(ffmpeg),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(video_track),
    ]
    concat_result = run_ffmpeg(concat_command, out_dir, out_dir / "logs" / "concat_video_track.txt")
    if concat_result.returncode != 0:
        raise StoryboardError(f"Failed to concat storyboard clips:\n{concat_result.stderr[-3000:]}")

    output = out_dir / "preview.mp4"
    command = [str(ffmpeg), "-y", "-i", str(video_track), "-i", str(audio)]
    if srt_path and args.subtitle_mode == "burn":
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
        command.extend(["-vf", f"subtitles={srt_path.name}:force_style='{style}'"])
    command.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output),
        ]
    )
    result = run_ffmpeg(command, out_dir, out_dir / "logs" / "render_preview.txt")
    if result.returncode != 0:
        raise StoryboardError(f"Failed to render preview with libx264:\n{result.stderr[-3000:]}")
    return output.resolve()


def write_handoff_notes(out_dir: Path, timeline_path: Path, segments_csv: Path, shots_csv: Path, markers_csv: Path) -> None:
    (out_dir / "premiere_import_notes.md").write_text(
        "\n".join(
            [
                "# Premiere Handoff",
                "",
                "Import the files under `assets/` into Premiere Pro.",
                "Use `timeline.json` as the exact source of truth for segment and shot timings.",
                "`markers.csv` contains one marker per numbered segment, with timecode and seconds.",
                "`shots.csv` contains image spans; place each image from `visual_start` to `visual_end`.",
                "",
                f"- Timeline: `{timeline_path.name}`",
                f"- Segments: `{segments_csv.name}`",
                f"- Shots: `{shots_csv.name}`",
                f"- Markers: `{markers_csv.name}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "capcut_import_notes.md").write_text(
        "\n".join(
            [
                "# CapCut Handoff",
                "",
                "Import the narration and images from `assets/`.",
                "Use `shots.csv` to place each image on the timeline.",
                "Use `segments.csv` or `markers.csv` when manually checking repair targets.",
                "If subtitles are enabled, import `subtitles.srt` as a sidecar subtitle file.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def package_outputs(
    build: TimelineBuild,
    shots: list[ShotRow],
    out_dir: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], Path | None]:
    assets_dir = out_dir / "assets"
    audio_assets = assets_dir / "audio"
    segment_assets = audio_assets / "segments"
    image_assets = assets_dir / "images"
    copied_audio: Path | None = None
    if build.audio_path:
        copied_audio = copy_file(build.audio_path, audio_assets / f"narration{build.audio_path.suffix.lower()}")

    copied_segment_by_index: dict[int, str] = {}
    for segment in build.segments:
        source = Path(segment.audio)
        if source.exists():
            target = segment_assets / f"{segment.index:03d}_{safe_stem(segment.id)}{source.suffix.lower()}"
            copied_segment_by_index[segment.index] = str(copy_file(source, target))

    for offset, shot in enumerate(shots, start=1):
        source = Path(shot.image)
        if not source.exists() and shot.copied_image:
            source = Path(shot.copied_image)
        copied = copy_file(source, image_assets / f"{offset:03d}_{safe_stem(shot.id)}{source.suffix.lower()}")
        shot.copied_image = str(copied)

    srt_path: Path | None = None
    if args.subtitle_mode != "off":
        srt_path = out_dir / "subtitles.srt"
        write_srt(
            build.segments,
            srt_path,
            max_chars=args.subtitle_max_chars,
            keep_punctuation=args.keep_subtitle_punctuation,
        )

    timeline_payload = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": {"kind": build.source_kind, "path": str(build.source_path)},
        "audio": {
            "path": str(build.audio_path) if build.audio_path else None,
            "package_path": str(copied_audio) if copied_audio else None,
            "duration": build.audio_duration,
            "computed_duration": build.computed_duration,
            "duration_delta": build.audio_duration - build.computed_duration,
        },
        "timing": build.timing,
        "subtitle_mode": args.subtitle_mode,
        "segments": [asdict(segment) | {"package_audio": copied_segment_by_index.get(segment.index)} for segment in build.segments],
        "shots": [asdict(shot) for shot in shots],
        "warnings": build.warnings,
    }
    timeline_path = out_dir / "timeline.json"
    timeline_path.write_text(json.dumps(timeline_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    segment_rows = [
        {
            "index": segment.index,
            "id": segment.id,
            "repair_target": segment.repair_target,
            "start_seconds": f"{segment.start:.3f}",
            "end_seconds": f"{segment.end:.3f}",
            "duration_seconds": f"{segment.duration:.3f}",
            "status": segment.status,
            "text": segment.text,
            "audio": segment.audio,
            "package_audio": copied_segment_by_index.get(segment.index, ""),
        }
        for segment in build.segments
    ]
    segments_csv = out_dir / "segments.csv"
    write_csv(
        segments_csv,
        segment_rows,
        ["index", "id", "repair_target", "start_seconds", "end_seconds", "duration_seconds", "status", "text", "audio", "package_audio"],
    )

    shot_rows = [
        {
            "id": shot.id,
            "image": shot.image,
            "package_image": shot.copied_image or "",
            "segments": " ".join(shot.segment_ids),
            "spoken_start": f"{shot.spoken_start:.3f}",
            "spoken_end": f"{shot.spoken_end:.3f}",
            "visual_start": f"{shot.visual_start:.3f}",
            "visual_end": f"{shot.visual_end:.3f}",
            "fit": shot.fit,
            "crop_x": shot.crop_x,
            "crop_y": shot.crop_y,
            "notes": shot.notes,
        }
        for shot in shots
    ]
    shots_csv = out_dir / "shots.csv"
    write_csv(
        shots_csv,
        shot_rows,
        ["id", "image", "package_image", "segments", "spoken_start", "spoken_end", "visual_start", "visual_end", "fit", "crop_x", "crop_y", "notes"],
    )

    markers_rows = [
        {
            "name": segment.id,
            "repair_target": segment.repair_target,
            "start_timecode": seconds_to_timecode(segment.start, args.fps),
            "end_timecode": seconds_to_timecode(segment.end, args.fps),
            "start_seconds": f"{segment.start:.3f}",
            "end_seconds": f"{segment.end:.3f}",
            "duration_seconds": f"{segment.duration:.3f}",
            "comment": segment.text,
        }
        for segment in build.segments
    ]
    markers_csv = out_dir / "markers.csv"
    write_csv(
        markers_csv,
        markers_rows,
        ["name", "repair_target", "start_timecode", "end_timecode", "start_seconds", "end_seconds", "duration_seconds", "comment"],
    )

    write_handoff_notes(out_dir, timeline_path, segments_csv, shots_csv, markers_csv)
    qa_lines = [
        "# Storyboard Package QA",
        "",
        f"- Segments: {len(build.segments)}",
        f"- Shots: {len(shots)}",
        f"- Audio duration: {build.audio_duration:.3f}s",
        f"- Computed timeline duration: {build.computed_duration:.3f}s",
        f"- Duration delta: {build.audio_duration - build.computed_duration:.3f}s",
        f"- Subtitle mode: {args.subtitle_mode}",
        "",
    ]
    if build.warnings:
        qa_lines.extend(["## Warnings", ""])
        qa_lines.extend(f"- {warning}" for warning in build.warnings)
        qa_lines.append("")
    (out_dir / "storyboard_qa.md").write_text("\n".join(qa_lines).rstrip() + "\n", encoding="utf-8")
    return timeline_payload, srt_path


def main() -> int:
    args = parse_args()
    try:
        if args.fps <= 0:
            raise StoryboardError("--fps must be greater than 0.")
        clamp_anchor(float(args.crop_x), "--crop-x")
        clamp_anchor(float(args.crop_y), "--crop-y")
        ffprobe = None
        if args.ffprobe or args.render_preview:
            ffprobe = find_runtime_tool("ffprobe", args.ffprobe)
        build = load_timeline_build(args, ffprobe)
        if not build.segments:
            raise StoryboardError("Timeline has no segments.")
        duration_delta = build.audio_duration - build.computed_duration
        if abs(duration_delta) > 0.5:
            build.warnings.append(
                f"Audio duration differs from computed timeline by {duration_delta:.3f}s; "
                "check postprocess tail/head settings if exact editor alignment looks off."
            )
        last_segment_end = max(segment.end for segment in build.segments)
        if last_segment_end > build.audio_duration + 0.1:
            build.warnings.append(
                f"Last segment ends at {last_segment_end:.3f}s, beyond audio duration {build.audio_duration:.3f}s."
            )

        storyboard_payload = load_storyboard_payload(resolve_path(args.storyboard) if args.storyboard else None)
        if storyboard_payload and "subtitle_mode" in storyboard_payload and args.subtitle_mode == "off":
            mode = str(storyboard_payload["subtitle_mode"]).strip()
            if mode in {"off", "sidecar", "burn"}:
                args.subtitle_mode = mode
        shots, shot_warnings = build_shots(storyboard_payload, build.segments, build.audio_duration, args)
        if not shots and build.shots:
            shots = build.shots
            shot_warnings = []
        build.warnings.extend(shot_warnings)

        out_dir = prepare_output_dir(resolve_path(args.output_dir), args.name, args.keep_existing)
        timeline_payload, srt_path = package_outputs(build, shots, out_dir, args)

        preview_path: Path | None = None
        if args.render_preview:
            ffmpeg = find_runtime_tool("ffmpeg", args.ffmpeg)
            audio_for_render = Path(timeline_payload["audio"]["package_path"] or build.audio_path or "")
            preview_path = render_preview(ffmpeg, out_dir, shots, audio_for_render, srt_path, args)
            timeline_payload["preview"] = str(preview_path)
            (out_dir / "timeline.json").write_text(json.dumps(timeline_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest = {
            "package": str(out_dir),
            "timeline": str(out_dir / "timeline.json"),
            "segments_csv": str(out_dir / "segments.csv"),
            "shots_csv": str(out_dir / "shots.csv"),
            "markers_csv": str(out_dir / "markers.csv"),
            "srt": str(srt_path) if srt_path else None,
            "preview": str(preview_path) if preview_path else None,
            "warnings": build.warnings,
        }
        (out_dir / "storyboard_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except StoryboardError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
