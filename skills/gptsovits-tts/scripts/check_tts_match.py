#!/usr/bin/env python3
"""Check whether generated TTS audio matches the intended text."""

from __future__ import annotations

import argparse
import json
import os
import re
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
DEFAULT_CONFIG = SKILL_ROOT / "config" / "voice_default.json"


class CheckError(RuntimeError):
    """User-facing checker error."""


@dataclass(frozen=True)
class DiffSummary:
    distance: int
    cer: float
    missing: list[str]
    extra: list[str]
    replaced: list[tuple[str, str]]


@dataclass(frozen=True)
class ChunkMatch:
    text: str
    normalized: str
    score: float
    char_score: float
    pinyin_score: float
    start: int
    end: int
    matched_text: str
    pinyin_matched_text: str


@dataclass(frozen=True)
class EdgeOmission:
    chunk: str
    side: str
    text: str
    matched_text: str


@dataclass(frozen=True)
class ChunkCoverage:
    missing: list[ChunkMatch]
    weak: list[ChunkMatch]
    edge_omissions: list[EdgeOmission]
    repeated: list[tuple[str, int]]
    matches: list[ChunkMatch]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe TTS audio with local FunASR and compare it to input text."
    )
    parser.add_argument("--audio", required=True, help="Generated audio file to check.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", help="Expected text.")
    group.add_argument("--text-file", help="UTF-8 expected text file.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Voice config JSON.")
    parser.add_argument("--language", default="zh", choices=["zh", "yue"], help="ASR language.")
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "faster-whisper", "whisper", "funasr"],
        help=(
            "ASR backend. auto uses local Faster-Whisper/OpenAI-Whisper models only; "
            "FunASR is opt-in because it can be slow to initialize."
        ),
    )
    parser.add_argument("--model", help="ASR model directory, model file, or local model name.")
    parser.add_argument(
        "--asr-timeout",
        type=int,
        default=300,
        help="Seconds to wait for the ASR worker before failing.",
    )
    parser.add_argument(
        "--max-fragments",
        type=int,
        default=8,
        help="Maximum diff fragments to show per category.",
    )
    parser.add_argument(
        "--pass-cer",
        type=float,
        default=0.12,
        help="CER threshold at or below which the check is reported as PASS.",
    )
    parser.add_argument(
        "--chunk-threshold",
        type=float,
        default=0.7,
        help="Minimum fuzzy score for an expected sentence chunk to count as covered.",
    )
    parser.add_argument(
        "--weak-chunk-threshold",
        type=float,
        default=0.82,
        help="Chunks below this score but above --chunk-threshold are reported as weak.",
    )
    parser.add_argument(
        "--repeat-threshold",
        type=float,
        default=0.82,
        help="Fuzzy score used to flag likely repeated expected chunks in ASR output.",
    )
    parser.add_argument("--report", help="Markdown report path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help="Deprecated internal option kept for compatibility.",
    )
    parser.add_argument(
        "--asr-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CheckError(f"Config file does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(value: str | os.PathLike[str], base: Path = PROJECT_ROOT) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def read_expected_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        text = args.text
    else:
        path = resolve_path(args.text_file)
        if not path.exists():
            raise CheckError(f"Expected text file does not exist: {path}")
        text = path.read_text(encoding="utf-8-sig")
    text = text.strip()
    if not text:
        raise CheckError("Expected text is empty.")
    return text


def configured_runtime(config: dict[str, Any]) -> Path | None:
    raw = config.get("python_exe")
    if not raw:
        return None
    path = resolve_path(str(raw))
    return path if path.exists() else None


def runtime_python(config: dict[str, Any]) -> Path:
    configured = configured_runtime(config)
    return configured if configured is not None else Path(sys.executable).resolve()


def require_local_asr_models(gptsovits_root: Path, language: str) -> tuple[Path, Path | None, Path | None]:
    models_dir = gptsovits_root / "tools" / "asr" / "models"
    if language == "zh":
        asr = models_dir / "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
        vad = models_dir / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
        punc = models_dir / "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
    elif language == "yue":
        asr = models_dir / "speech_UniASR_asr_2pass-cantonese-CHS-16k-common-vocab1468-tensorflow1-online"
        vad = None
        punc = None
    else:
        raise CheckError(f"Unsupported language: {language}")

    missing = [path for path in (asr, vad, punc) if path is not None and not path.exists()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise CheckError(f"Local ASR model files are missing:\n{joined}")
    return asr, vad, punc


def transcribe_with_funasr(audio_path: Path, config: dict[str, Any], language: str) -> str:
    gptsovits_root = resolve_path(str(config.get("gptsovits_root") or ""))
    if not gptsovits_root.exists():
        raise CheckError(f"gptsovits_root does not exist: {gptsovits_root}")
    asr_path, vad_path, punc_path = require_local_asr_models(gptsovits_root, language)

    sys.path.insert(0, str(gptsovits_root))
    old_cwd = Path.cwd()
    try:
        os.chdir(gptsovits_root)
        from funasr import AutoModel  # type: ignore

        kwargs: dict[str, Any] = {"model": str(asr_path)}
        if vad_path is not None:
            kwargs["vad_model"] = str(vad_path)
        if punc_path is not None:
            kwargs["punc_model"] = str(punc_path)
        model = AutoModel(**kwargs)
        result = model.generate(input=str(audio_path))
    finally:
        os.chdir(old_cwd)

    if not result:
        raise CheckError("ASR returned no result.")
    text = str(result[0].get("text", "")).strip()
    if not text:
        raise CheckError("ASR transcription is empty.")
    return text


def local_faster_whisper_model(gptsovits_root: Path, model: str | None) -> Path | None:
    if model:
        path = resolve_path(model)
        return path if path.exists() else None

    models_dir = gptsovits_root / "tools" / "asr" / "models"
    candidates = [
        models_dir / "faster-whisper-large-v3-turbo",
        models_dir / "faster-whisper-large-v3",
        models_dir / "faster-whisper-large-v2",
        models_dir / "faster-whisper-medium",
        models_dir / "faster-whisper-small",
        models_dir / "faster-whisper-base",
        models_dir / "faster-whisper-tiny",
    ]
    for candidate in candidates:
        if (candidate / "model.bin").exists():
            return candidate
    return None


def transcribe_with_faster_whisper(
    audio_path: Path,
    config: dict[str, Any],
    language: str,
    model: str | None,
) -> str:
    gptsovits_root = resolve_path(str(config.get("gptsovits_root") or ""))
    model_path = local_faster_whisper_model(gptsovits_root, model)
    if model_path is None:
        raise CheckError(
            "No local Faster-Whisper model found. Pass --model <model_dir> or place "
            "a model directory such as tools/asr/models/faster-whisper-small."
        )

    from faster_whisper import WhisperModel  # type: ignore

    whisper = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    segments, _info = whisper.transcribe(str(audio_path), language=language, vad_filter=False)
    text = "".join(segment.text for segment in segments).strip()
    if not text:
        raise CheckError("Faster-Whisper transcription is empty.")
    return text


def local_openai_whisper_model(model: str | None) -> str | None:
    if model:
        path = resolve_path(model)
        if path.exists():
            return str(path)
        cache_path = Path.home() / ".cache" / "whisper" / f"{model}.pt"
        if cache_path.exists():
            return model
        return None

    for name in ("small", "base", "tiny"):
        cache_path = Path.home() / ".cache" / "whisper" / f"{name}.pt"
        if cache_path.exists():
            return name
    return None


def transcribe_with_openai_whisper(audio_path: Path, language: str, model: str | None) -> str:
    local_model = local_openai_whisper_model(model)
    if local_model is None:
        raise CheckError(
            "No local OpenAI-Whisper model found. Pass --model <model.pt> or put a "
            "cached model under ~/.cache/whisper."
        )

    import whisper  # type: ignore

    whisper_model = whisper.load_model(local_model, device="cpu")
    result = whisper_model.transcribe(str(audio_path), language=language, fp16=False)
    text = str(result.get("text", "")).strip()
    if not text:
        raise CheckError("Whisper transcription is empty.")
    return text


def transcribe_audio(
    audio_path: Path,
    config: dict[str, Any],
    language: str,
    backend: str,
    model: str | None,
) -> str:
    errors: list[str] = []
    if backend in ("auto", "faster-whisper"):
        try:
            return transcribe_with_faster_whisper(audio_path, config, language, model)
        except CheckError as exc:
            errors.append(f"faster-whisper: {exc}")
            if backend == "faster-whisper":
                raise

    if backend in ("auto", "whisper"):
        try:
            return transcribe_with_openai_whisper(audio_path, language, model)
        except CheckError as exc:
            errors.append(f"whisper: {exc}")
            if backend == "whisper":
                raise

    if backend == "funasr":
        return transcribe_with_funasr(audio_path, config, language)

    raise CheckError(
        "No usable local ASR backend was found.\n"
        + "\n".join(f"- {error}" for error in errors)
        + "\nFunASR is available only with --backend funasr because it can be slow to initialize."
    )


def run_asr_worker(args: argparse.Namespace, config: dict[str, Any]) -> str:
    python_exe = runtime_python(config)
    command = [
        str(python_exe),
        str(SCRIPT_PATH),
        "--asr-worker",
        "--audio",
        str(resolve_path(args.audio)),
        "--config",
        str(resolve_path(args.config)),
        "--language",
        args.language,
        "--backend",
        args.backend,
    ]
    if args.model:
        command.extend(["--model", args.model])

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=args.asr_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            f"ASR worker timed out after {args.asr_timeout}s. "
            "Try a smaller local Whisper model or run --backend funasr with a larger --asr-timeout."
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise CheckError(f"ASR worker failed:\n{detail}")

    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            return str(payload["transcript"])
    raise CheckError(f"ASR worker returned no JSON payload:\n{completed.stdout[-2000:]}")


def run_worker_mode(args: argparse.Namespace) -> int:
    try:
        config = load_json(resolve_path(args.config))
        audio_path = resolve_path(args.audio)
        transcript = transcribe_audio(audio_path, config, args.language, args.backend, args.model)
        print(json.dumps({"transcript": transcript}, ensure_ascii=False))
        return 0
    except CheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"<\|[^|]+?\|>", "", text)
    return "".join(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def pinyin_for_compare(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin  # type: ignore
    except ImportError:
        return ""

    normalized = normalize_for_compare(text)
    pieces: list[str] = []
    for char in normalized:
        if "\u4e00" <= char <= "\u9fff":
            pieces.extend(lazy_pinyin(char, errors="ignore"))
        else:
            pieces.append(char)
    return " ".join(piece for piece in pieces if piece)


def normalized_chars(text: str) -> list[str]:
    normalized = normalize_for_compare(text)
    return list(normalized)


def pinyin_tokens_for_chars(chars: list[str]) -> list[str]:
    try:
        from pypinyin import lazy_pinyin  # type: ignore
    except ImportError:
        return []

    tokens: list[str] = []
    for char in chars:
        if "\u4e00" <= char <= "\u9fff":
            pinyin = lazy_pinyin(char, errors="ignore")
            tokens.append(pinyin[0] if pinyin else char)
        else:
            tokens.append(char)
    return tokens


def edge_omissions_for_match(chunk: str, matched_pinyin: str) -> list[EdgeOmission]:
    chars = normalized_chars(chunk)
    expected_tokens = pinyin_tokens_for_chars(chars)
    matched_tokens = matched_pinyin.split()
    if len(chars) < 4 or not expected_tokens or not matched_tokens:
        return []

    omissions: list[EdgeOmission] = []
    important_prefixes = {"结果", "然后", "所以", "但是", "可是", "不过", "可能"}

    edge_len = 2
    if len(expected_tokens) > edge_len + 1 and len(matched_tokens) >= edge_len:
        prefix_text = "".join(chars[:edge_len])
        prefix_expected = " ".join(expected_tokens[:edge_len])
        prefix_matched = " ".join(matched_tokens[:edge_len])
        body_expected = " ".join(expected_tokens[edge_len:])
        body_score, *_ = best_partial_match(body_expected, matched_pinyin)
        if (
            prefix_text in important_prefixes
            and fuzzy_ratio(prefix_expected, prefix_matched) < 0.55
            and body_score >= 0.8
        ):
            omissions.append(
                EdgeOmission(
                    chunk=chunk,
                    side="prefix",
                    text=prefix_text,
                    matched_text=matched_pinyin,
                )
            )

        suffix_expected = " ".join(expected_tokens[-edge_len:])
        suffix_matched = " ".join(matched_tokens[-edge_len:])
        body_expected = " ".join(expected_tokens[:-edge_len])
        body_score, *_ = best_partial_match(body_expected, matched_pinyin)
        if fuzzy_ratio(suffix_expected, suffix_matched) < 0.55 and body_score >= 0.8:
            omissions.append(
                EdgeOmission(
                    chunk=chunk,
                    side="suffix",
                    text="".join(chars[-edge_len:]),
                    matched_text=matched_pinyin,
                )
            )

    return omissions


def split_sentence_chunks(text: str) -> list[str]:
    chunks = [
        chunk.strip()
        for chunk in re.split(r"[，,。！？!?；;、\r\n]+", text)
        if chunk.strip()
    ]
    return [chunk for chunk in chunks if len(normalize_for_compare(chunk)) >= 2]


def fuzzy_ratio(a: str, b: str) -> float:
    import difflib

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def best_partial_match(pattern: str, text: str) -> tuple[float, int, int, str]:
    if not pattern or not text:
        return 0.0, -1, -1, ""
    exact = text.find(pattern)
    if exact >= 0:
        return 1.0, exact, exact + len(pattern), text[exact : exact + len(pattern)]

    pattern_len = len(pattern)
    min_len = max(1, int(pattern_len * 0.65))
    max_len = min(len(text), max(pattern_len + 4, int(pattern_len * 1.45)))
    best_score = 0.0
    best_start = -1
    best_end = -1
    best_text = ""

    for window_len in range(min_len, max_len + 1):
        for start in range(0, len(text) - window_len + 1):
            candidate = text[start : start + window_len]
            score = fuzzy_ratio(pattern, candidate)
            if score > best_score:
                best_score = score
                best_start = start
                best_end = start + window_len
                best_text = candidate
    return best_score, best_start, best_end, best_text


def count_fuzzy_occurrences(pattern: str, text: str, threshold: float) -> int:
    if len(pattern) < 4 or not text:
        return 0
    count = 0
    cursor = 0
    while cursor < len(text):
        score, start, end, _matched = best_partial_match(pattern, text[cursor:])
        if score < threshold or start < 0 or end <= start:
            break
        count += 1
        cursor += end
    return count


def analyze_chunk_coverage(
    expected_text: str,
    transcript: str,
    *,
    chunk_threshold: float,
    weak_threshold: float,
    repeat_threshold: float,
) -> ChunkCoverage:
    transcript_norm = normalize_for_compare(transcript)
    transcript_pinyin = pinyin_for_compare(transcript)
    matches: list[ChunkMatch] = []
    missing: list[ChunkMatch] = []
    weak: list[ChunkMatch] = []
    edge_omissions: list[EdgeOmission] = []
    repeated: list[tuple[str, int]] = []

    for chunk in split_sentence_chunks(expected_text):
        chunk_norm = normalize_for_compare(chunk)
        char_score, start, end, matched_text = best_partial_match(chunk_norm, transcript_norm)
        chunk_pinyin = pinyin_for_compare(chunk)
        pinyin_score = 0.0
        pinyin_matched_text = ""
        if chunk_pinyin and transcript_pinyin:
            pinyin_score, _p_start, _p_end, pinyin_matched_text = best_partial_match(
                chunk_pinyin, transcript_pinyin
            )
        score = max(char_score, pinyin_score)
        match = ChunkMatch(
            text=chunk,
            normalized=chunk_norm,
            score=score,
            char_score=char_score,
            pinyin_score=pinyin_score,
            start=start,
            end=end,
            matched_text=matched_text,
            pinyin_matched_text=pinyin_matched_text,
        )
        matches.append(match)
        if score < chunk_threshold:
            missing.append(match)
        elif score < weak_threshold:
            weak.append(match)
        if score >= chunk_threshold:
            edge_omissions.extend(edge_omissions_for_match(chunk, pinyin_matched_text))

        occurrence_count = count_fuzzy_occurrences(chunk_norm, transcript_norm, repeat_threshold)
        if occurrence_count > 1:
            repeated.append((chunk, occurrence_count))

    return ChunkCoverage(
        missing=missing,
        weak=weak,
        edge_omissions=edge_omissions,
        repeated=repeated,
        matches=matches,
    )


def levenshtein_summary(expected: str, actual: str, max_fragments: int) -> DiffSummary:
    previous = list(range(len(actual) + 1))
    for i, exp_char in enumerate(expected, start=1):
        current = [i]
        for j, act_char in enumerate(actual, start=1):
            cost = 0 if exp_char == act_char else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    distance = previous[-1] if previous else len(actual)
    cer = distance / max(1, len(expected))

    import difflib

    missing: list[str] = []
    extra: list[str] = []
    replaced: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete" and len(missing) < max_fragments:
            missing.append(expected[i1:i2])
        elif tag == "insert" and len(extra) < max_fragments:
            extra.append(actual[j1:j2])
        elif tag == "replace" and len(replaced) < max_fragments:
            replaced.append((expected[i1:i2], actual[j1:j2]))

    return DiffSummary(distance=distance, cer=cer, missing=missing, extra=extra, replaced=replaced)


def default_report_path(audio_path: Path) -> Path:
    report_dir = PROJECT_ROOT / "outputs" / "checks"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return report_dir / f"{audio_path.stem}_check_{timestamp}.md"


def render_report(
    audio_path: Path,
    expected_text: str,
    transcript: str,
    expected_norm: str,
    transcript_norm: str,
    summary: DiffSummary,
    coverage: ChunkCoverage,
    pass_cer: float,
) -> str:
    status = "FAIL" if coverage.missing or coverage.edge_omissions or coverage.repeated else "PASS"
    lines = [
        "# TTS Match Report",
        "",
        f"- Audio: `{audio_path}`",
        f"- Status: **{status}**",
        f"- Missing chunks: **{len(coverage.missing)}**",
        f"- Edge omissions: **{len(coverage.edge_omissions)}**",
        f"- Repeated chunks: **{len(coverage.repeated)}**",
        f"- Weak chunks: {len(coverage.weak)}",
        f"- CER: **{summary.cer:.2%}** ({summary.distance}/{len(expected_norm)} chars)",
        f"- CER threshold: {pass_cer:.2%}",
        "",
        "## Expected",
        "",
        expected_text,
        "",
        "## ASR Transcript",
        "",
        transcript,
        "",
        "## Normalized",
        "",
        f"- Expected: `{expected_norm}`",
        f"- ASR: `{transcript_norm}`",
        "",
        "## Chunk Coverage",
        "",
    ]

    if (
        not coverage.missing
        and not coverage.edge_omissions
        and not coverage.repeated
        and not coverage.weak
    ):
        lines.append("All expected sentence chunks were covered with no likely repetition.")
    else:
        if coverage.missing:
            lines.append("Missing expected chunks:")
            lines.extend(
                f"- `{match.text}` (score {match.score:.2f}, char {match.char_score:.2f}, "
                f"pinyin {match.pinyin_score:.2f}, closest `{match.matched_text}`)"
                for match in coverage.missing
            )
            lines.append("")
        if coverage.edge_omissions:
            lines.append("Boundary omissions inside covered chunks:")
            lines.extend(
                f"- `{omission.text}` missing at {omission.side} of `{omission.chunk}` "
                f"(matched pinyin `{omission.matched_text}`)"
                for omission in coverage.edge_omissions
            )
            lines.append("")
        if coverage.repeated:
            lines.append("Likely repeated chunks:")
            lines.extend(f"- `{chunk}` appeared about {count} times" for chunk, count in coverage.repeated)
            lines.append("")
        if coverage.weak:
            lines.append("Weakly matched chunks:")
            lines.extend(
                f"- `{match.text}` (score {match.score:.2f}, char {match.char_score:.2f}, "
                f"pinyin {match.pinyin_score:.2f}, closest `{match.matched_text}`)"
                for match in coverage.weak
            )
            lines.append("")

    lines.extend(
        [
            "Chunk match details:",
            *[
                f"- `{match.text}` -> {match.score:.2f} "
                f"(char {match.char_score:.2f}, pinyin {match.pinyin_score:.2f}) "
                f"(`{match.matched_text}`)"
                for match in coverage.matches
            ],
            "",
        ]
    )

    lines.extend(
        [
        "## Differences",
        "",
        ]
    )

    if not summary.missing and not summary.extra and not summary.replaced:
        lines.append("No character-level differences after normalization.")
    else:
        if summary.missing:
            lines.extend(["Missing from ASR:", *[f"- `{item}`" for item in summary.missing], ""])
        if summary.extra:
            lines.extend(["Extra in ASR:", *[f"- `{item}`" for item in summary.extra], ""])
        if summary.replaced:
            lines.append("Replaced:")
            lines.extend(f"- `{old}` -> `{new}`" for old, new in summary.replaced)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    if args.asr_worker:
        return run_worker_mode(args)

    try:
        if args.text is None and args.text_file is None:
            raise CheckError("One of --text or --text-file is required.")

        config_path = resolve_path(args.config)
        config = load_json(config_path)

        audio_path = resolve_path(args.audio)
        if not audio_path.exists():
            raise CheckError(f"Audio file does not exist: {audio_path}")
        expected_text = read_expected_text(args)
        transcript = run_asr_worker(args, config)

        expected_norm = normalize_for_compare(expected_text)
        transcript_norm = normalize_for_compare(transcript)
        if not expected_norm:
            raise CheckError("Expected text is empty after normalization.")
        summary = levenshtein_summary(expected_norm, transcript_norm, args.max_fragments)
        coverage = analyze_chunk_coverage(
            expected_text,
            transcript,
            chunk_threshold=args.chunk_threshold,
            weak_threshold=args.weak_chunk_threshold,
            repeat_threshold=args.repeat_threshold,
        )
        report_path = resolve_path(args.report) if args.report else default_report_path(audio_path)
        report = render_report(
            audio_path,
            expected_text,
            transcript,
            expected_norm,
            transcript_norm,
            summary,
            coverage,
            args.pass_cer,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")

        payload = {
            "audio": str(audio_path),
            "report": str(report_path.resolve()),
            "cer": summary.cer,
            "distance": summary.distance,
            "expected_chars": len(expected_norm),
            "status": "FAIL"
            if coverage.missing or coverage.edge_omissions or coverage.repeated
            else "PASS",
            "transcript": transcript,
            "missing_chunks": [match.text for match in coverage.missing],
            "weak_chunks": [match.text for match in coverage.weak],
            "edge_omissions": [
                {
                    "chunk": omission.chunk,
                    "side": omission.side,
                    "text": omission.text,
                }
                for omission in coverage.edge_omissions
            ],
            "repeated_chunks": [
                {"text": chunk, "count": count} for chunk, count in coverage.repeated
            ],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Status: {payload['status']}")
            if coverage.missing:
                print("Missing chunks:")
                for match in coverage.missing:
                    print(
                        f"  - {match.text} "
                        f"(score {match.score:.2f}, char {match.char_score:.2f}, "
                        f"pinyin {match.pinyin_score:.2f})"
                    )
            if coverage.edge_omissions:
                print("Boundary omissions:")
                for omission in coverage.edge_omissions:
                    print(f"  - {omission.text} missing at {omission.side} of: {omission.chunk}")
            if coverage.repeated:
                print("Repeated chunks:")
                for chunk, count in coverage.repeated:
                    print(f"  - {chunk} (~{count}x)")
            if coverage.weak:
                print("Weak chunks:")
                for match in coverage.weak:
                    print(
                        f"  - {match.text} "
                        f"(score {match.score:.2f}, char {match.char_score:.2f}, "
                        f"pinyin {match.pinyin_score:.2f})"
                    )
            print(f"CER: {summary.cer:.2%} ({summary.distance}/{len(expected_norm)} chars)")
            print(f"ASR: {transcript}")
            print(f"Report: {report_path.resolve()}")
        return 0
    except CheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
