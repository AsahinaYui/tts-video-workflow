#!/usr/bin/env python3
"""Post-process generated WAV audio with deterministic silence edits."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SKILL_ROOT.parent.parent if SKILL_ROOT.parent.name == "skills" else Path.cwd()


class AudioPostprocessError(RuntimeError):
    """User-facing post-processing error."""


@dataclass(frozen=True)
class WavAudio:
    path: Path
    params: wave._wave_params
    frames: bytes

    @property
    def frame_size(self) -> int:
        return self.params.nchannels * self.params.sampwidth

    @property
    def frame_count(self) -> int:
        return self.params.nframes

    @property
    def duration(self) -> float:
        return self.frame_count / self.params.framerate


@dataclass(frozen=True)
class SilenceRegion:
    start_frame: int
    end_frame: int

    def duration(self, framerate: int) -> float:
        return (self.end_frame - self.start_frame) / framerate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Input WAV path.")
    parser.add_argument("--output", help="Output WAV path.")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "outputs" / "tts_final"),
        help="Output directory used when --output is omitted.",
    )
    parser.add_argument(
        "--internal-extra",
        type=float,
        default=0.0,
        help="Seconds of silence to insert into every internal pause.",
    )
    parser.add_argument(
        "--head-add",
        type=float,
        default=0.0,
        help="Seconds of silence to add at the very beginning.",
    )
    parser.add_argument(
        "--tail-min",
        type=float,
        default=0.0,
        help="Ensure the final trailing silence is at least this many seconds.",
    )
    parser.add_argument(
        "--tail-add",
        type=float,
        default=0.0,
        help="Seconds of silence to add at the end in addition to --tail-min.",
    )
    parser.add_argument(
        "--silence-threshold-dbfs",
        type=float,
        default=-45.0,
        help="Chunks quieter than this dBFS value count as silence.",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=0.16,
        help="Minimum seconds for a detected silence region.",
    )
    parser.add_argument(
        "--chunk-ms",
        type=float,
        default=10.0,
        help="Analysis chunk size in milliseconds.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    return parser.parse_args()


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def read_wav(path: Path) -> WavAudio:
    if not path.exists():
        raise AudioPostprocessError(f"Input audio does not exist: {path}")
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(params.nframes)
    if params.sampwidth not in (1, 2, 4):
        raise AudioPostprocessError(
            f"Unsupported WAV sample width {params.sampwidth}; expected 8, 16, or 32 bit PCM."
        )
    return WavAudio(path=path, params=params, frames=frames)


def write_wav(path: Path, audio: WavAudio, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    params = audio.params._replace(nframes=len(frames) // audio.frame_size)
    with wave.open(str(path), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)


def silence_bytes(audio: WavAudio, seconds: float) -> bytes:
    if seconds <= 0:
        return b""
    frames = int(round(seconds * audio.params.framerate))
    return b"\x00" * frames * audio.frame_size


def frame_slice(audio: WavAudio, start_frame: int, end_frame: int) -> bytes:
    start = max(0, start_frame) * audio.frame_size
    end = min(audio.frame_count, end_frame) * audio.frame_size
    return audio.frames[start:end]


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
    else:
        count = len(chunk) // 4
        values = struct.unpack("<" + "i" * count, chunk[: count * 4])
        max_amp = 2147483648.0

    if not values:
        return float("-inf")
    rms = math.sqrt(sum(float(value) * float(value) for value in values) / len(values))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / max_amp)


def detect_silences(
    audio: WavAudio,
    *,
    threshold_dbfs: float,
    min_silence: float,
    chunk_ms: float,
) -> list[SilenceRegion]:
    chunk_frames = max(1, int(round(audio.params.framerate * chunk_ms / 1000.0)))
    min_frames = max(1, int(round(audio.params.framerate * min_silence)))
    regions: list[SilenceRegion] = []
    active_start: int | None = None

    for start in range(0, audio.frame_count, chunk_frames):
        end = min(audio.frame_count, start + chunk_frames)
        dbfs = chunk_dbfs(frame_slice(audio, start, end), audio.params.sampwidth)
        is_silent = dbfs <= threshold_dbfs
        if is_silent and active_start is None:
            active_start = start
        elif not is_silent and active_start is not None:
            if start - active_start >= min_frames:
                regions.append(SilenceRegion(active_start, start))
            active_start = None

    if active_start is not None and audio.frame_count - active_start >= min_frames:
        regions.append(SilenceRegion(active_start, audio.frame_count))
    return regions


def internal_regions(regions: list[SilenceRegion], total_frames: int) -> list[SilenceRegion]:
    return [
        region
        for region in regions
        if region.start_frame > 0 and region.end_frame < total_frames
    ]


def trailing_silence_seconds(regions: list[SilenceRegion], audio: WavAudio) -> float:
    if not regions:
        return 0.0
    last = regions[-1]
    if last.end_frame != audio.frame_count:
        return 0.0
    return last.duration(audio.params.framerate)


def insert_internal_silence(
    audio: WavAudio,
    regions: list[SilenceRegion],
    extra_seconds: float,
) -> tuple[bytes, int]:
    if extra_seconds <= 0 or not regions:
        return audio.frames, 0

    addition = silence_bytes(audio, extra_seconds)
    pieces: list[bytes] = []
    cursor = 0
    for region in regions:
        point = region.end_frame
        pieces.append(frame_slice(audio, cursor, point))
        pieces.append(addition)
        cursor = point
    pieces.append(frame_slice(audio, cursor, audio.frame_count))
    return b"".join(pieces), len(regions)


def default_output_path(input_path: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    suffix_parts: list[str] = []
    if args.internal_extra:
        suffix_parts.append(f"pauseplus{int(round(args.internal_extra * 1000)):03d}ms")
    if args.head_add:
        suffix_parts.append(f"head{int(round(args.head_add * 1000)):03d}ms")
    if args.tail_min:
        suffix_parts.append(f"tailmin{int(round(args.tail_min * 1000)):03d}ms")
    if args.tail_add:
        suffix_parts.append(f"tailplus{int(round(args.tail_add * 1000)):03d}ms")
    suffix = "_".join(suffix_parts) or datetime.now().strftime("post_%Y%m%d_%H%M%S")
    return output_dir / f"{input_path.stem}_{suffix}.wav"


def main() -> int:
    args = parse_args()
    try:
        input_path = resolve_path(args.input)
        output_path = (
            resolve_path(args.output)
            if args.output
            else default_output_path(input_path, resolve_path(args.output_dir), args)
        )
        audio = read_wav(input_path)
        regions = detect_silences(
            audio,
            threshold_dbfs=args.silence_threshold_dbfs,
            min_silence=args.min_silence,
            chunk_ms=args.chunk_ms,
        )
        internal = internal_regions(regions, audio.frame_count)
        old_duration = audio.duration
        tail_before = trailing_silence_seconds(regions, audio)

        processed_frames, internal_count = insert_internal_silence(audio, internal, args.internal_extra)
        additions = [silence_bytes(audio, args.head_add), processed_frames]
        tail_needed = max(0.0, args.tail_min - tail_before)
        tail_added = tail_needed + max(0.0, args.tail_add)
        additions.append(silence_bytes(audio, tail_added))
        final_frames = b"".join(additions)
        write_wav(output_path, audio, final_frames)

        new_duration = len(final_frames) / audio.frame_size / audio.params.framerate
        payload = {
            "input": str(input_path),
            "output": str(output_path),
            "old_duration": old_duration,
            "new_duration": new_duration,
            "detected_silences": len(regions),
            "extended_internal_pauses": internal_count,
            "internal_extra": args.internal_extra,
            "head_added": args.head_add,
            "tail_before": tail_before,
            "tail_added": tail_added,
            "tail_target": args.tail_min,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Output: {output_path}")
            print(f"Old duration: {old_duration:.3f}s")
            print(f"New duration: {new_duration:.3f}s")
            print(f"Detected silences: {len(regions)}")
            print(f"Extended internal pauses: {internal_count}")
            print(f"Head added: {args.head_add:.3f}s")
            print(f"Tail before: {tail_before:.3f}s")
            print(f"Tail added: {tail_added:.3f}s")
        return 0
    except AudioPostprocessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
