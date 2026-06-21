#!/usr/bin/env python3
"""Generate speech through a local GPT-SoVITS api_v2.py server."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SKILL_ROOT.parent.parent if SKILL_ROOT.parent.name == "skills" else Path.cwd()
DEFAULT_CONFIG = SKILL_ROOT / "config" / "voice_default.json"
TEXT_EXTENSIONS = (".txt", ".lab")
AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a")
LOGGER_NAME = "gptsovits_tts"
API_HOST = "127.0.0.1"
API_PORT = "9880"
MODEL_VERSIONS = {"v1", "v2", "v3", "v4", "v2Pro", "v2ProPlus"}
LOCAL_API_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
WEIGHT_ROOT_DIR_NAMES = {
    "GPT_weights",
    "GPT_weights_v2",
    "GPT_weights_v2Pro",
    "GPT_weights_v2ProPlus",
    "GPT_weights_v3",
    "GPT_weights_v4",
    "SoVITS_weights",
    "SoVITS_weights_v2",
    "SoVITS_weights_v2Pro",
    "SoVITS_weights_v2ProPlus",
    "SoVITS_weights_v3",
    "SoVITS_weights_v4",
    "GPT_SoVITS",
}


class TtsError(RuntimeError):
    """User-facing workflow error."""


class ApiHttpError(TtsError):
    def __init__(self, status: int, reason: str, body: bytes):
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason
        self.body = body


@dataclass(frozen=True)
class RuntimePaths:
    gptsovits_root: Path
    python_exe: Path
    tts_config_path: Path
    gpt_weights_path: Path
    sovits_weights_path: Path
    api_script: Path


@dataclass(frozen=True)
class ApiHealth:
    reachable: bool
    url: str
    status: int | None
    detail: str


@dataclass(frozen=True)
class ReferenceAssets:
    audio_path: Path
    text_path: Path
    text: str


@dataclass(frozen=True)
class ApiLogPaths:
    combined: Path
    stdout: Path
    stderr: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a timestamped audio file with GPT-SoVITS api_v2.py."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", help="Text to synthesize.")
    group.add_argument("--text-file", help="UTF-8 text file to synthesize.")
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Validate paths, start/check the API, confirm model switching, "
            "then exit without generating audio."
        ),
    )
    parser.add_argument(
        "--debug-start-api",
        action="store_true",
        help="Start the GPT-SoVITS API only, wait for health, then exit without TTS.",
    )
    parser.add_argument(
        "--foreground-start-api",
        action="store_true",
        help="Run api_v2.py in the foreground and stream stdout/stderr to the console.",
    )
    parser.add_argument(
        "--skip-model-switch",
        action="store_true",
        help=(
            "Do not call /set_gpt_weights or /set_sovits_weights; use the "
            "weights loaded from the startup tts_infer config."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to the voice config JSON.",
    )
    parser.add_argument("--output-dir", help="Override output directory.")
    parser.add_argument("--api-url", help="Override the GPT-SoVITS /tts URL.")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the generated audio file after saving.",
    )
    args = parser.parse_args()
    mode_count = sum(
        1
        for enabled in (args.test, args.debug_start_api, args.foreground_start_api)
        if enabled
    )
    if mode_count > 1:
        parser.error("use only one of --test, --debug-start-api, or --foreground-start-api")
    if mode_count == 0 and args.text is None and args.text_file is None:
        parser.error(
            "one of --text or --text-file is required unless a test/debug mode is used"
        )
    return args


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TtsError(f"Config file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TtsError(f"Config file is not valid JSON: {path} ({exc})") from exc


def setup_logging() -> tuple[logging.Logger, Path]:
    temp_dir = PROJECT_ROOT / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    log_path = temp_dir / "gsv_tts.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    return logger, log_path.resolve()


def log_path_check(logger: logging.Logger, label: str, path: Path) -> None:
    logger.info("%s: %s", label, path)


def make_api_log_paths() -> ApiLogPaths:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ApiLogPaths(
        combined=(logs_dir / f"api_start_{timestamp}.log").resolve(),
        stdout=(logs_dir / f"api_stdout_{timestamp}.log").resolve(),
        stderr=(logs_dir / f"api_stderr_{timestamp}.log").resolve(),
    )


def tail_lines(path: Path, count: int = 200) -> str:
    if not path.exists():
        return "(log file does not exist)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(could not read log file: {exc})"
    tail = lines[-count:]
    return "\n".join(tail) if tail else "(log file is empty)"


def command_to_text(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(command)


def is_unset_path(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lowered = text.lower()
    return (
        "path/to" in lowered
        or "change_me" in lowered
        or lowered.startswith("<")
        or lowered.endswith(">")
    )


def resolve_path(value: Any, base: Path = PROJECT_ROOT) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(value).strip()))
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def config_value(config: dict[str, Any], key: str, *legacy_paths: tuple[str, str]) -> Any:
    value = config.get(key)
    if not is_unset_path(value):
        return value
    for section_name, legacy_key in legacy_paths:
        section = config.get(section_name, {})
        if isinstance(section, dict):
            value = section.get(legacy_key)
            if not is_unset_path(value):
                return value
    return value


def required_path_value(
    config: dict[str, Any],
    key: str,
    *,
    base: Path = PROJECT_ROOT,
    legacy_paths: tuple[tuple[str, str], ...] = (),
) -> Path:
    raw_value = config_value(config, key, *legacy_paths)
    if is_unset_path(raw_value):
        raise TtsError(
            f"{key} is not configured. Edit "
            "skills/gptsovits-tts/config/voice_default.json."
        )
    return resolve_path(raw_value, base)


def resolve_weight_path_value(
    config: dict[str, Any],
    key: str,
    root: Path,
    *,
    legacy_paths: tuple[tuple[str, str], ...] = (),
) -> Path:
    raw_value = config_value(config, key, *legacy_paths)
    if is_unset_path(raw_value):
        raise TtsError(
            f"{key} is not configured. Edit "
            "skills/gptsovits-tts/config/voice_default.json."
        )
    text = os.path.expandvars(os.path.expanduser(str(raw_value).strip()))
    path = Path(text)
    if path.is_absolute():
        return path.resolve()
    first = text.replace("\\", "/").split("/", 1)[0]
    if first in WEIGHT_ROOT_DIR_NAMES:
        return (root / path).resolve()
    project_candidate = resolve_path(text, PROJECT_ROOT)
    if project_candidate.exists():
        return project_candidate
    root_candidate = (root / path).resolve()
    if root_candidate.exists():
        return root_candidate
    return project_candidate


def require_existing_file(label: str, path: Path) -> None:
    if not path.exists():
        raise TtsError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise TtsError(f"{label} is not a file: {path}")


def require_existing_dir(label: str, path: Path) -> None:
    if not path.exists():
        raise TtsError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise TtsError(f"{label} is not a directory: {path}")


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise TtsError(f"Output path is not a directory: {path}")
    probe = path / f".write_test_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        raise TtsError(f"Output directory is not writable: {path} ({exc})") from exc
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def first_supported_file(directory: Path, extensions: tuple[str, ...], label: str) -> Path:
    require_existing_dir(label, directory)
    candidates = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        ),
        key=lambda path: path.name.lower(),
    )
    if not candidates:
        suffixes = ", ".join(extensions)
        raise TtsError(f"No supported {label} file found in {directory} ({suffixes}).")
    return candidates[0].resolve()


def reference_path_from_file_or_dir(
    voice: dict[str, Any],
    file_key: str,
    dir_key: str,
    extensions: tuple[str, ...],
    label: str,
) -> Path:
    raw_file = voice.get(file_key)
    if not is_unset_path(raw_file):
        return resolve_path(raw_file)

    raw_dir = voice.get(dir_key)
    if not is_unset_path(raw_dir):
        return first_supported_file(resolve_path(raw_dir), extensions, dir_key)

    raise TtsError(
        f"voice.{file_key} is not configured. Set voice.{file_key} or "
        f"voice.{dir_key} in skills/gptsovits-tts/config/voice_default.json."
    )


def validate_runtime_paths(config: dict[str, Any]) -> RuntimePaths:
    root = required_path_value(
        config,
        "gptsovits_root",
        legacy_paths=(("api", "gptsovits_root"),),
    )
    require_existing_dir("gptsovits_root", root)

    python_exe = required_path_value(
        config,
        "python_exe",
        legacy_paths=(("api", "python_exe"), ("api", "python_executable")),
    )
    require_existing_file("python_exe", python_exe)

    tts_config_path = required_path_value(config, "tts_config_path")
    require_existing_file("tts_config_path", tts_config_path)

    if use_pretrained_base_model(config):
        default_version = model_switch_default_version(config)
        gpt_weights_path = base_weight_for_version(
            root,
            tts_config_path,
            default_version,
            "t2s_weights_path",
            "GPT",
        )
        sovits_weights_path = base_weight_for_version(
            root,
            tts_config_path,
            default_version,
            "vits_weights_path",
            "SoVITS",
        )
    else:
        gpt_weights_path = resolve_weight_path_value(
            config,
            "gpt_weights_path",
            root,
            legacy_paths=(("models", "gpt_weights_path"),),
        )
        require_existing_file("gpt_weights_path", gpt_weights_path)
        if gpt_weights_path.suffix.lower() != ".ckpt":
            raise TtsError(f"gpt_weights_path must point to a .ckpt file: {gpt_weights_path}")

        sovits_weights_path = resolve_weight_path_value(
            config,
            "sovits_weights_path",
            root,
            legacy_paths=(("models", "sovits_weights_path"),),
        )
        require_existing_file("sovits_weights_path", sovits_weights_path)
        if sovits_weights_path.suffix.lower() != ".pth":
            raise TtsError(
                f"sovits_weights_path must point to a .pth file: {sovits_weights_path}"
            )

    api_script = root / "api_v2.py"
    require_existing_file("api_v2.py", api_script)

    return RuntimePaths(
        gptsovits_root=root,
        python_exe=python_exe,
        tts_config_path=tts_config_path,
        gpt_weights_path=gpt_weights_path,
        sovits_weights_path=sovits_weights_path,
        api_script=api_script,
    )


def workflow_tts_config_path(gptsovits_root: Path) -> Path:
    return PROJECT_ROOT / "temp" / "tts_infer_workflow.yaml"


def with_tts_config_path(runtime_paths: RuntimePaths, tts_config_path: Path) -> RuntimePaths:
    return RuntimePaths(
        gptsovits_root=runtime_paths.gptsovits_root,
        python_exe=runtime_paths.python_exe,
        tts_config_path=tts_config_path,
        gpt_weights_path=runtime_paths.gpt_weights_path,
        sovits_weights_path=runtime_paths.sovits_weights_path,
        api_script=runtime_paths.api_script,
    )


def prepare_workflow_tts_config(
    runtime_paths: RuntimePaths, logger: logging.Logger
) -> RuntimePaths:
    target = workflow_tts_config_path(runtime_paths.gptsovits_root).resolve()
    source = runtime_paths.tts_config_path.resolve()
    if source == target:
        logger.info("Using workflow TTS config already at target: %s", target)
        return runtime_paths
    if target.exists():
        logger.info("Using existing workflow TTS config: %s", target)
        return with_tts_config_path(runtime_paths, target)

    logger.info("Generating workflow TTS config from %s to %s", source, target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise TtsError(
            "Failed to generate GPT-SoVITS workflow config at "
            f"{target}. The API startup command must use this file. "
            f"Original error: {exc}"
        ) from exc
    return with_tts_config_path(runtime_paths, target)


def read_input_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        text = args.text
    else:
        path = resolve_path(args.text_file)
        if not path.exists():
            raise TtsError(f"Input text file does not exist: {path}")
        text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise TtsError("Input text is empty.")
    return text.strip()


def read_reference_text(config: dict[str, Any]) -> str:
    path = validate_reference_text_path(config)
    voice = config.get("voice", {})
    encoding = voice.get("ref_text_encoding") or "utf-8-sig"
    try:
        text = path.read_text(encoding=encoding)
    except UnicodeDecodeError as exc:
        raise TtsError(
            f"Cannot read reference text with encoding '{encoding}': {path}"
        ) from exc
    if not text.strip():
        raise TtsError(f"Reference text file is empty: {path}")
    return text.strip()


def validate_reference_text_path(config: dict[str, Any]) -> Path:
    voice = config.get("voice", {})
    path = reference_path_from_file_or_dir(
        voice,
        "ref_text_path",
        "ref_text_dir",
        TEXT_EXTENSIONS,
        "reference text",
    )
    if not path.exists():
        raise TtsError(f"Reference text file does not exist: {path}")
    if not path.is_file():
        raise TtsError(f"Reference text path is not a file: {path}")
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        raise TtsError(f"Reference text file type is not supported: {path}")
    return path


def validate_reference_audio(config: dict[str, Any]) -> Path:
    voice = config.get("voice", {})
    path = reference_path_from_file_or_dir(
        voice,
        "ref_audio_path",
        "ref_audio_dir",
        AUDIO_EXTENSIONS,
        "reference audio",
    )
    if not path.exists():
        raise TtsError(f"Reference audio file does not exist: {path}")
    if not path.is_file():
        raise TtsError(f"Reference audio path is not a file: {path}")
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise TtsError(f"Reference audio file type is not supported: {path}")
    return path


def validate_reference_assets(config: dict[str, Any]) -> ReferenceAssets:
    audio_path = validate_reference_audio(config)
    text_path = validate_reference_text_path(config)
    text = read_reference_text(config)
    return ReferenceAssets(audio_path=audio_path, text_path=text_path, text=text)


def wav_duration_seconds(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as audio:
        return audio.getnframes() / float(audio.getframerate())


def make_reference_clip_path(source: Path, seconds: float) -> Path:
    stat = source.stat()
    digest_source = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{seconds}"
    digest = hashlib.sha1(digest_source.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    clip_dir = PROJECT_ROOT / "temp" / "ref_audio_clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    return (clip_dir / f"ref_{digest}_{int(seconds)}s.wav").resolve()


def trim_wav_reference(source: Path, target: Path, seconds: float) -> None:
    with contextlib.closing(wave.open(str(source), "rb")) as src:
        params = src.getparams()
        frames = int(src.getframerate() * seconds)
        data = src.readframes(frames)
    with contextlib.closing(wave.open(str(target), "wb")) as dst:
        dst.setparams(params)
        dst.writeframes(data)


def prepare_reference_assets_for_api(
    reference_assets: ReferenceAssets,
    logger: logging.Logger,
) -> ReferenceAssets:
    audio_path = reference_assets.audio_path
    if audio_path.suffix.lower() != ".wav":
        return reference_assets
    try:
        duration = wav_duration_seconds(audio_path)
    except (wave.Error, OSError) as exc:
        logger.warning("Could not inspect WAV duration for %s: %s", audio_path, exc)
        return reference_assets

    logger.info("Reference audio duration: %.2fs", duration)
    if duration < 3:
        raise TtsError(
            "Reference audio is shorter than GPT-SoVITS requires "
            f"(3-10 seconds): {audio_path} ({duration:.2f}s)"
        )
    if duration <= 10:
        return reference_assets

    clip_seconds = 6.0
    clip_path = make_reference_clip_path(audio_path, clip_seconds)
    if not clip_path.exists():
        trim_wav_reference(audio_path, clip_path, clip_seconds)
    logger.info(
        "Reference audio is %.2fs; using temporary %.1fs clip for API: %s",
        duration,
        clip_seconds,
        clip_path,
    )
    return ReferenceAssets(
        audio_path=clip_path,
        text_path=reference_assets.text_path,
        text=reference_assets.text,
    )


def build_api_command(runtime_paths: RuntimePaths) -> list[str]:
    return [
        str(runtime_paths.python_exe),
        "-u",
        "api_v2.py",
        "-a",
        API_HOST,
        "-p",
        API_PORT,
        "-c",
        str(runtime_paths.tts_config_path),
    ]


def build_api_env(runtime_paths: RuntimePaths) -> dict[str, str]:
    env = os.environ.copy()
    runtime_dir = str(runtime_paths.python_exe.parent)
    temp_dir = PROJECT_ROOT / "temp"
    numba_cache_dir = temp_dir / "numba_cache"
    temp_dir.mkdir(parents=True, exist_ok=True)
    numba_cache_dir.mkdir(parents=True, exist_ok=True)
    env["PATH"] = runtime_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["TEMP"] = str(temp_dir.resolve())
    env["TMP"] = str(temp_dir.resolve())
    env["NUMBA_CACHE_DIR"] = str(numba_cache_dir.resolve())
    return env


def print_api_start_details(
    runtime_paths: RuntimePaths,
    command: list[str],
    log_paths: ApiLogPaths,
) -> None:
    print("API start details:")
    print(f"  command: {command_to_text(command)}")
    print(f"  cwd: {runtime_paths.gptsovits_root}")
    print(f"  python_exe: {runtime_paths.python_exe}")
    print(f"  tts_config_path: {runtime_paths.tts_config_path}")
    print(f"  api_combined_log: {log_paths.combined}")
    print(f"  api_stdout_log: {log_paths.stdout}")
    print(f"  api_stderr_log: {log_paths.stderr}")


def parse_simple_tts_yaml(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            current = line[:-1].strip()
            sections[current] = {}
            continue
        if current and line.startswith("  ") and ":" in line:
            key, value = line.strip().split(":", 1)
            sections[current][key.strip()] = value.strip().strip("'\"")
    return sections


def infer_model_version_from_path(path: Path) -> str | None:
    lowered = str(path).lower()
    if "v2proplus" in lowered:
        return "v2ProPlus"
    if "v2pro" in lowered:
        return "v2Pro"
    if "v4" in lowered:
        return "v4"
    if "v3" in lowered:
        return "v3"
    if "v2" in lowered:
        return "v2"
    if "v1" in lowered:
        return "v1"
    return None


def resolve_gptsovits_relative(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def inspect_tts_config(
    runtime_paths: RuntimePaths,
    logger: logging.Logger,
    *,
    strict: bool = True,
) -> list[str]:
    sections = parse_simple_tts_yaml(runtime_paths.tts_config_path)
    custom = sections.get("custom", {})
    selected_gpt_version = infer_model_version_from_path(runtime_paths.gpt_weights_path)
    selected_sovits_version = infer_model_version_from_path(runtime_paths.sovits_weights_path)
    active_version = custom.get("version")
    expected = sections.get(selected_sovits_version or selected_gpt_version or "", {})

    lines = [
        "TTS config inspection:",
        f"  config: {runtime_paths.tts_config_path}",
        f"  active section: custom",
        f"  custom.version: {active_version or '(missing)'}",
        f"  selected GPT weight inferred version: {selected_gpt_version or '(unknown)'}",
        f"  selected SoVITS weight inferred version: {selected_sovits_version or '(unknown)'}",
        f"  custom.t2s_weights_path: {custom.get('t2s_weights_path', '(missing)')}",
        f"  custom.vits_weights_path: {custom.get('vits_weights_path', '(missing)')}",
        f"  custom.bert_base_path: {custom.get('bert_base_path', '(missing)')}",
        f"  custom.cnhuhbert_base_path: {custom.get('cnhuhbert_base_path', '(missing)')}",
    ]
    missing_paths: list[str] = []

    keys_to_check = (
        "t2s_weights_path",
        "vits_weights_path",
        "bert_base_path",
        "cnhuhbert_base_path",
        "cnhubert_base_path",
        "ssl_path",
        "hubert_path",
    )

    lines.append("  selected.gpt_weights_path: " + str(runtime_paths.gpt_weights_path))
    lines.append("  selected.sovits_weights_path: " + str(runtime_paths.sovits_weights_path))
    for label, path in (
        ("selected.gpt_weights_path", runtime_paths.gpt_weights_path),
        ("selected.sovits_weights_path", runtime_paths.sovits_weights_path),
    ):
        exists = path.exists()
        lines.append(f"  exists {label}: {exists} ({path})")
        if not exists:
            missing_paths.append(f"{label}: {path}")

    for section_name, section in sections.items():
        lines.append(f"  section [{section_name}] path checks:")
        for key in keys_to_check:
            if key not in section:
                continue
            resolved = resolve_gptsovits_relative(runtime_paths.gptsovits_root, section.get(key))
            if resolved is not None:
                exists = resolved.exists()
                lines.append(f"    exists {section_name}.{key}: {exists} ({resolved})")
                if not exists:
                    missing_paths.append(f"{section_name}.{key}: {resolved}")

    inferred_versions = {v for v in (selected_gpt_version, selected_sovits_version) if v}
    if len(inferred_versions) == 1:
        inferred_version = next(iter(inferred_versions))
        if active_version and active_version != inferred_version:
            lines.append(
                "  WARNING: custom.version does not match selected model version "
                f"({active_version} != {inferred_version})."
            )
        if expected:
            lines.append(f"  expected section for selected model: {inferred_version}")
            lines.append(
                f"  expected.t2s_weights_path: {expected.get('t2s_weights_path', '(missing)')}"
            )
            lines.append(
                f"  expected.vits_weights_path: {expected.get('vits_weights_path', '(missing)')}"
            )
    elif len(inferred_versions) > 1:
        lines.append(
            "  WARNING: selected GPT and SoVITS weights appear to use different versions."
        )
    else:
        lines.append("  WARNING: could not infer selected model version from weight paths.")

    for line in lines:
        logger.info(line)
    if strict and missing_paths:
        raise TtsError(
            "tts_infer yaml contains missing model/base-model paths:\n"
            + "\n".join(f"- {item}" for item in missing_paths)
        )
    return lines


def print_tts_config_inspection(lines: list[str]) -> None:
    for line in lines:
        print(line)


def probe_url(url: str, timeout: float = 2.0) -> ApiHealth:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            return ApiHealth(True, url, status, "ok")
    except urllib.error.HTTPError as exc:
        detail = format_error_body(exc.read(), limit=500)
        return ApiHealth(exc.code < 500, url, exc.code, detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ApiHealth(False, url, None, str(exc))


def check_api_health(config: dict[str, Any], tts_url: str, timeout: float = 2.0) -> ApiHealth:
    api = config.get("api", {})
    urls = []
    health_url = api.get("health_url")
    if health_url:
        urls.append(str(health_url))
    urls.append(tts_url)

    last_result = ApiHealth(False, tts_url, None, "not checked")
    for url in dict.fromkeys(urls):
        result = probe_url(url, timeout=timeout)
        if result.reachable:
            return result
        last_result = result
    return last_result


def api_reachable(config: dict[str, Any], tts_url: str, timeout: float = 2.0) -> bool:
    return check_api_health(config, tts_url, timeout=timeout).reachable


def format_error_body(body: bytes, limit: int = 2000) -> str:
    if not body:
        return "(empty response body)"
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pass
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


def write_api_log_headers(
    log_paths: ApiLogPaths,
    mode: str,
    runtime_paths: RuntimePaths,
    command: list[str],
) -> None:
    header = (
        f"\n\n[{datetime.now().isoformat(timespec='seconds')}] {mode}\n"
        f"command: {command_to_text(command)}\n"
        f"cwd: {runtime_paths.gptsovits_root}\n"
        f"python_exe: {runtime_paths.python_exe}\n"
        f"tts_config_path: {runtime_paths.tts_config_path}\n"
        + "-" * 80
        + "\n"
    )
    for path in (log_paths.combined, log_paths.stdout, log_paths.stderr):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(header)


def stream_pipe_to_logs(
    pipe: Any,
    own_log_path: Path,
    combined_log_path: Path,
    stream_name: str,
    combined_lock: threading.Lock,
    *,
    mirror_to_console: bool = False,
) -> None:
    with own_log_path.open("a", encoding="utf-8", errors="replace") as own_log:
        for line in iter(pipe.readline, ""):
            own_log.write(line)
            own_log.flush()
            with combined_lock:
                with combined_log_path.open("a", encoding="utf-8", errors="replace") as combined:
                    combined.write(f"[{stream_name}] {line}")
                    combined.flush()
            if mirror_to_console:
                target = sys.stderr if stream_name == "stderr" else sys.stdout
                print(line, end="", file=target)
    pipe.close()


def start_stream_threads(
    process: subprocess.Popen[str],
    log_paths: ApiLogPaths,
    *,
    mirror_to_console: bool = False,
) -> list[threading.Thread]:
    lock = threading.Lock()
    threads: list[threading.Thread] = []
    streams = [
        (process.stdout, log_paths.stdout, "stdout"),
        (process.stderr, log_paths.stderr, "stderr"),
    ]
    for pipe, path, name in streams:
        if pipe is None:
            continue
        thread = threading.Thread(
            target=stream_pipe_to_logs,
            args=(pipe, path, log_paths.combined, name, lock),
            kwargs={"mirror_to_console": mirror_to_console},
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def join_stream_threads(threads: list[threading.Thread], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)


def record_api_returncode(log_paths: ApiLogPaths, exit_code: int | None) -> None:
    line = (
        f"\n[{datetime.now().isoformat(timespec='seconds')}] "
        f"api_v2.py returncode={exit_code}\n"
    )
    for path in (log_paths.combined, log_paths.stderr):
        try:
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(line)
        except OSError:
            pass


def append_new_log_content(
    log_paths: ApiLogPaths,
    offsets: dict[Path, int],
) -> None:
    for stream_name, path in (("stdout", log_paths.stdout), ("stderr", log_paths.stderr)):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        offset = offsets.get(path, 0)
        if len(data) <= offset:
            continue
        chunk = data[offset:].decode("utf-8", errors="replace")
        offsets[path] = len(data)
        with log_paths.combined.open("a", encoding="utf-8", errors="replace") as combined:
            for line in chunk.splitlines(keepends=True):
                combined.write(f"[{stream_name}] {line}")


def api_exit_error(exit_code: int | None, log_paths: ApiLogPaths) -> TtsError:
    return TtsError(
        "api_v2.py exited before the API became reachable. "
        f"Return code: {exit_code}.\n"
        f"Combined log: {log_paths.combined}\n"
        f"Stdout log: {log_paths.stdout}\n"
        f"Stderr log: {log_paths.stderr}\n"
        f"Last 200 stdout lines:\n{tail_lines(log_paths.stdout, 200)}\n"
        f"Last 200 stderr lines:\n{tail_lines(log_paths.stderr, 200)}"
    )


def start_api_if_needed(
    config: dict[str, Any],
    tts_url: str,
    runtime_paths: RuntimePaths,
    logger: logging.Logger,
) -> ApiHealth:
    api = config.get("api", {})
    health = check_api_health(config, tts_url)
    logger.info(
        "API health before start: reachable=%s url=%s status=%s detail=%s",
        health.reachable,
        health.url,
        health.status,
        health.detail,
    )
    if health.reachable:
        return health

    log_paths = make_api_log_paths()
    command = build_api_command(runtime_paths)
    logger.info("Starting GPT-SoVITS API in %s", runtime_paths.gptsovits_root)
    logger.info("API command: %s", command_to_text(command))
    logger.info("API combined log: %s", log_paths.combined)
    logger.info("API stdout log: %s", log_paths.stdout)
    logger.info("API stderr log: %s", log_paths.stderr)
    print_api_start_details(runtime_paths, command, log_paths)
    env = build_api_env(runtime_paths)
    write_api_log_headers(log_paths, "background-start-api", runtime_paths, command)
    offsets = {
        log_paths.stdout: log_paths.stdout.stat().st_size if log_paths.stdout.exists() else 0,
        log_paths.stderr: log_paths.stderr.stat().st_size if log_paths.stderr.exists() else 0,
    }
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stdout_handle = log_paths.stdout.open("ab", buffering=0)
    stderr_handle = log_paths.stderr.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(runtime_paths.gptsovits_root),
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        stdout_handle.close()
        stderr_handle.close()
        raise TtsError(f"Failed to start GPT-SoVITS API: {exc}") from exc

    timeout = float(api.get("startup_timeout_seconds", 90))
    deadline = time.monotonic() + timeout
    last_health = health
    while time.monotonic() < deadline:
        append_new_log_content(log_paths, offsets)
        exit_code = process.poll()
        if exit_code is not None:
            logger.error("API process exited early with returncode=%s", exit_code)
            append_new_log_content(log_paths, offsets)
            stdout_handle.close()
            stderr_handle.close()
            record_api_returncode(log_paths, exit_code)
            raise api_exit_error(exit_code, log_paths)
        last_health = check_api_health(config, tts_url)
        if last_health.reachable:
            append_new_log_content(log_paths, offsets)
            stdout_handle.close()
            stderr_handle.close()
            logger.info(
                "API became reachable: url=%s status=%s detail=%s",
                last_health.url,
                last_health.status,
                last_health.detail,
            )
            return last_health
        time.sleep(1)

    exit_code = process.poll()
    append_new_log_content(log_paths, offsets)
    stdout_handle.close()
    stderr_handle.close()
    if exit_code is not None:
        logger.error("API process exited at startup timeout with returncode=%s", exit_code)
        record_api_returncode(log_paths, exit_code)
        raise api_exit_error(exit_code, log_paths)
    logger.warning(
        "API startup timed out but process is still running: pid=%s. Not killing it.",
        process.pid,
    )
    raise TtsError(
        "Timed out waiting for GPT-SoVITS API to start. "
        f"Last health check: {last_health.url} {last_health.status} "
        f"{last_health.detail}. Process pid={process.pid} is still running and was not "
        "killed. Check old Python processes and GPU VRAM usage. "
        f"Combined log: {log_paths.combined}. "
        f"Stdout log: {log_paths.stdout}. Stderr log: {log_paths.stderr}.\n"
        f"Last 200 stdout lines:\n{tail_lines(log_paths.stdout, 200)}\n"
        f"Last 200 stderr lines:\n{tail_lines(log_paths.stderr, 200)}"
    )


def call_endpoint_get(url: str, params: dict[str, Any], timeout: float) -> bytes:
    request = urllib.request.Request(build_url_with_query(url, params), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ApiHttpError(exc.code, exc.reason, exc.read()) from exc
    except urllib.error.URLError as exc:
        raise TtsError(f"Failed to call GPT-SoVITS endpoint: {exc}") from exc


def query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def call_endpoint_post(url: str, payload: dict[str, Any], timeout: float) -> tuple[bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "audio/*, application/octet-stream, */*",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.read(), content_type
    except urllib.error.HTTPError as exc:
        raise ApiHttpError(exc.code, exc.reason, exc.read()) from exc
    except urllib.error.URLError as exc:
        raise TtsError(f"Failed to call GPT-SoVITS /tts endpoint: {exc}") from exc


def build_url_with_query(url: str, params: dict[str, Any]) -> str:
    query = urllib.parse.urlencode(
        {key: query_value(value) for key, value in params.items() if value is not None},
        quote_via=urllib.parse.quote,
    )
    separator = "&" if "?" in url else "?"
    return url + separator + query


def ensure_local_api_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise TtsError(f"GPT-SoVITS API URL must be http(s), got: {url}")
    host = parsed.hostname
    if host not in LOCAL_API_HOSTS:
        raise TtsError(
            "Refusing to call a non-local GPT-SoVITS API URL. "
            f"Allowed hosts: {', '.join(sorted(LOCAL_API_HOSTS))}. Got: {url}"
        )


def require_success_response(endpoint: str, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise TtsError(f"/{endpoint} returned an empty success response.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if text.strip('"').lower() == "success":
            return text
        raise TtsError(f"/{endpoint} returned an unexpected response: {text}")

    if isinstance(parsed, dict) and str(parsed.get("message", "")).lower() == "success":
        return text
    raise TtsError(
        f"/{endpoint} did not confirm success:\n"
        f"{json.dumps(parsed, ensure_ascii=False, indent=2)}"
    )


def contains_non_ascii(text: str) -> bool:
    return any(ord(char) > 127 for char in text)


def alias_name_for_weight(label: str, path: Path) -> str:
    digest_source = f"{path.resolve()}|{path.stat().st_size}|{path.stat().st_mtime_ns}"
    digest = hashlib.sha1(digest_source.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    suffix = path.suffix.lower()
    safe_label = "gpt" if "gpt" in label else "sovits"
    return f"{safe_label}_{digest}{suffix}"


def ensure_ascii_weight_alias(label: str, path: Path, logger: logging.Logger) -> Path:
    path = path.resolve()
    if os.name != "nt" or not contains_non_ascii(str(path)):
        return path

    alias_dir = PROJECT_ROOT / "temp" / "model_alias"
    alias_dir.mkdir(parents=True, exist_ok=True)
    alias_path = (alias_dir / alias_name_for_weight(label, path)).resolve()
    source_size = path.stat().st_size

    if alias_path.exists() and alias_path.stat().st_size == source_size:
        logger.info("Using existing ASCII alias for %s: %s", label, alias_path)
        return alias_path

    if alias_path.exists() or alias_path.is_symlink():
        alias_path.unlink()

    logger.info("Creating ASCII alias copy for %s: %s -> %s", label, path, alias_path)
    try:
        shutil.copy2(path, alias_path)
    except OSError as exc:
        raise TtsError(
            f"Failed to create ASCII alias for {label}: {alias_path}\n"
            f"Original path: {path}\n"
            f"Original error: {exc}"
        ) from exc
    if not alias_path.exists() or alias_path.stat().st_size != source_size:
        raise TtsError(f"ASCII alias copy failed verification for {label}: {alias_path}")
    return alias_path


def apply_model_weights(
    runtime_paths: RuntimePaths,
    tts_url: str,
    config: dict[str, Any],
    timeout: float,
    logger: logging.Logger,
) -> None:
    base_url = tts_url.rsplit("/", 1)[0]
    default_version = model_switch_default_version(config)
    base_sovits_path = base_sovits_weight_for_version(runtime_paths, default_version)
    if use_pretrained_base_model(config):
        base_gpt_path = base_gpt_weight_for_version(runtime_paths, default_version)
        logger.info(
            "Using pretrained base model only; custom GPT / SoVITS weights are skipped."
        )
        apply_single_model_weight(
            f"{default_version}_base_sovits_before_gpt",
            "set_sovits_weights",
            base_sovits_path,
            base_url,
            timeout,
            logger,
        )
        apply_single_model_weight(
            f"{default_version}_base_gpt",
            "set_gpt_weights",
            base_gpt_path,
            base_url,
            timeout,
            logger,
        )
        apply_single_model_weight(
            f"{default_version}_base_sovits",
            "set_sovits_weights",
            base_sovits_path,
            base_url,
            timeout,
            logger,
        )
        return

    apply_single_model_weight(
        f"{default_version}_base_sovits_before_gpt",
        "set_sovits_weights",
        base_sovits_path,
        base_url,
        timeout,
        logger,
    )
    apply_single_model_weight(
        "gpt_weights_path",
        "set_gpt_weights",
        runtime_paths.gpt_weights_path,
        base_url,
        timeout,
        logger,
    )
    apply_single_model_weight(
        "sovits_weights_path",
        "set_sovits_weights",
        runtime_paths.sovits_weights_path,
        base_url,
        timeout,
        logger,
    )


def use_pretrained_base_model(config: dict[str, Any]) -> bool:
    model_switch = config.get("model_switch", {})
    return isinstance(model_switch, dict) and bool(model_switch.get("use_pretrained_base"))


def model_switch_default_version(config: dict[str, Any]) -> str:
    model_switch = config.get("model_switch", {})
    raw_version = "v2"
    if isinstance(model_switch, dict):
        raw_version = str(model_switch.get("default_version") or raw_version)
    version = raw_version.strip()
    if version not in MODEL_VERSIONS:
        raise TtsError(
            "model_switch.default_version must be one of "
            f"{', '.join(sorted(MODEL_VERSIONS))}; got {version!r}."
        )
    return version


def base_weight_for_version(
    root: Path,
    tts_config_path: Path,
    version: str,
    key: str,
    label: str,
) -> Path:
    sections = parse_simple_tts_yaml(tts_config_path)
    section = sections.get(version)
    if not section:
        raise TtsError(
            f"TTS config does not define a [{version}] section for model switching: "
            f"{tts_config_path}"
        )
    raw_path = section.get(key)
    if not raw_path:
        raise TtsError(
            f"TTS config section [{version}] does not define {key}: "
            f"{tts_config_path}"
        )
    path = resolve_gptsovits_relative(root, raw_path)
    if path is None:
        raise TtsError(f"Could not resolve {version}.{key}.")
    require_existing_file(f"{version}.{key}", path)
    expected_suffix = ".ckpt" if key == "t2s_weights_path" else ".pth"
    if path.suffix.lower() != expected_suffix:
        raise TtsError(
            f"Base {label} weight for {version} must point to a {expected_suffix} file: {path}"
        )
    return path


def base_gpt_weight_for_version(runtime_paths: RuntimePaths, version: str) -> Path:
    return base_weight_for_version(
        runtime_paths.gptsovits_root,
        runtime_paths.tts_config_path,
        version,
        "t2s_weights_path",
        "GPT",
    )


def base_sovits_weight_for_version(runtime_paths: RuntimePaths, version: str) -> Path:
    return base_weight_for_version(
        runtime_paths.gptsovits_root,
        runtime_paths.tts_config_path,
        version,
        "vits_weights_path",
        "SoVITS",
    )


def apply_single_model_weight(
    key: str,
    endpoint: str,
    weight_path: Path,
    base_url: str,
    timeout: float,
    logger: logging.Logger,
) -> None:
    api_weight_path = ensure_ascii_weight_alias(key, weight_path, logger)
    logger.info("Applying %s: %s", key, weight_path)
    if api_weight_path != weight_path:
        logger.info("API-compatible %s alias: %s", key, api_weight_path)
    url = build_url_with_query(
        f"{base_url}/{endpoint}",
        {"weights_path": str(api_weight_path)},
    )
    logger.info("Calling /%s with URL-encoded weights_path: %s", endpoint, url)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            detail = require_success_response(endpoint, body)
            logger.info("/%s confirmed: %s", endpoint, detail)
    except urllib.error.HTTPError as exc:
        raise TtsError(
            f"Failed to apply {key} through /{endpoint}: "
            f"HTTP {exc.code} {exc.reason}\n{format_error_body(exc.read())}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TtsError(f"Failed to apply {key} through /{endpoint}: {exc}") from exc


def build_payload(
    config: dict[str, Any],
    text: str,
    reference_audio: Path,
    reference_text: str,
    media_type: str,
) -> dict[str, Any]:
    voice = config.get("voice", {})
    payload: dict[str, Any] = {
        "text": text,
        "text_lang": voice.get("text_lang", "zh"),
        "ref_audio_path": str(reference_audio),
        "prompt_text": reference_text,
        "prompt_lang": voice.get("prompt_lang", "zh"),
        "media_type": media_type,
        "streaming_mode": False,
    }
    payload.update(config.get("sampling", {}))
    payload.update(config.get("extra_request_params", {}))
    return payload


def request_tts(config: dict[str, Any], tts_url: str, payload: dict[str, Any]) -> bytes:
    timeout = float(config.get("api", {}).get("request_timeout_seconds", 300))
    try:
        audio, content_type = call_endpoint_post(tts_url, payload, timeout)
    except ApiHttpError as exc:
        if exc.status != 405:
            raise TtsError(
                f"GPT-SoVITS /tts returned HTTP {exc.status} {exc.reason}.\n"
                f"{format_error_body(exc.body)}"
            ) from exc
        try:
            audio = call_endpoint_get(tts_url, payload, timeout)
            content_type = ""
        except ApiHttpError as get_exc:
            raise TtsError(
                f"GPT-SoVITS /tts rejected POST and GET.\n"
                f"POST: HTTP {exc.status} {exc.reason}\n"
                f"GET: HTTP {get_exc.status} {get_exc.reason}\n"
                f"{format_error_body(get_exc.body)}"
            ) from get_exc

    if not audio:
        raise TtsError("GPT-SoVITS returned an empty audio response.")
    if "application/json" in content_type.lower():
        raise TtsError(
            "GPT-SoVITS returned JSON instead of audio:\n"
            f"{format_error_body(audio)}"
        )
    return audio


def save_audio(audio: bytes, output_dir: Path, media_type: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = media_type.lower().lstrip(".")
    path = output_dir / f"tts_{timestamp}.{suffix}"
    counter = 1
    while path.exists():
        path = output_dir / f"tts_{timestamp}_{counter}.{suffix}"
        counter += 1
    path.write_bytes(audio)
    return path.resolve()


def open_audio(path: Path, logger: logging.Logger) -> bool:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        logger.info("Opened audio with system default player: %s", path)
        return True
    except Exception as exc:  # noqa: BLE001 - opening is best effort only.
        logger.warning("Could not open audio automatically: %s", exc)
        print(f"Warning: audio was saved, but could not be opened automatically: {exc}")
        return False


def log_validated_paths(
    logger: logging.Logger,
    config_path: Path,
    runtime_paths: RuntimePaths,
    reference_assets: ReferenceAssets,
    output_dir: Path,
) -> None:
    log_path_check(logger, "Config", config_path)
    log_path_check(logger, "GPT-SoVITS root", runtime_paths.gptsovits_root)
    log_path_check(logger, "Python executable", runtime_paths.python_exe)
    log_path_check(logger, "TTS config", runtime_paths.tts_config_path)
    log_path_check(logger, "GPT weights", runtime_paths.gpt_weights_path)
    log_path_check(logger, "SoVITS weights", runtime_paths.sovits_weights_path)
    log_path_check(logger, "Reference audio", reference_assets.audio_path)
    log_path_check(logger, "Reference text", reference_assets.text_path)
    log_path_check(logger, "Output dir", output_dir)
    logger.info("Reference text length: %s chars", len(reference_assets.text))


def run_test_mode(
    config: dict[str, Any],
    tts_url: str,
    runtime_paths: RuntimePaths,
    reference_assets: ReferenceAssets,
    output_dir: Path,
    logger: logging.Logger,
    log_path: Path,
    *,
    skip_model_switch: bool = False,
) -> int:
    print("Path validation: OK")
    print(f"Reference audio: {reference_assets.audio_path}")
    print(f"Reference text: {reference_assets.text_path}")
    print(f"Output dir: {output_dir}")

    health = start_api_if_needed(config, tts_url, runtime_paths, logger)
    print(f"API health: OK ({health.url}, status={health.status})")

    if skip_model_switch:
        logger.info("Model switch skipped by --skip-model-switch")
        print("Model switch: skipped")
    else:
        apply_model_weights(
            runtime_paths,
            tts_url,
            config,
            float(config.get("api", {}).get("request_timeout_seconds", 300)),
            logger,
        )
        print("Model switch: OK")
    print("Test mode completed. No audio was generated.")
    print(f"Workflow log: {log_path}")
    return 0


def run_debug_start_api(
    config: dict[str, Any],
    tts_url: str,
    runtime_paths: RuntimePaths,
    logger: logging.Logger,
    log_path: Path,
) -> int:
    print_tts_config_inspection(inspect_tts_config(runtime_paths, logger))
    health = start_api_if_needed(config, tts_url, runtime_paths, logger)
    print(f"API health: OK ({health.url}, status={health.status})")
    print("Debug start mode completed. No TTS request or model switch was executed.")
    print(f"Workflow log: {log_path}")
    return 0


def run_foreground_start_api(
    runtime_paths: RuntimePaths,
    logger: logging.Logger,
    workflow_log_path: Path,
) -> int:
    command = build_api_command(runtime_paths)
    log_paths = make_api_log_paths()
    print_tts_config_inspection(inspect_tts_config(runtime_paths, logger))
    print_api_start_details(runtime_paths, command, log_paths)
    print("Running api_v2.py in foreground. Press Ctrl+C to stop it.")

    env = build_api_env(runtime_paths)
    write_api_log_headers(log_paths, "foreground-start-api", runtime_paths, command)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(runtime_paths.gptsovits_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise TtsError(f"Failed to start GPT-SoVITS API: {exc}") from exc

    logger.info(
        "Foreground API pid=%s combined_log=%s stdout_log=%s stderr_log=%s",
        process.pid,
        log_paths.combined,
        log_paths.stdout,
        log_paths.stderr,
    )
    threads = start_stream_threads(process, log_paths, mirror_to_console=True)
    exit_code: int | None = None
    try:
        exit_code = process.wait()
    except KeyboardInterrupt:
        print("\nInterrupted. Terminating foreground API process...")
        logger.warning("Foreground API interrupted; terminating pid=%s", process.pid)
        process.terminate()
        exit_code = process.wait()
    finally:
        join_stream_threads(threads)
        record_api_returncode(log_paths, exit_code)

    print(f"api_v2.py exited with code {exit_code}")
    print(f"API combined log: {log_paths.combined}")
    print(f"API stdout log: {log_paths.stdout}")
    print(f"API stderr log: {log_paths.stderr}")
    print(f"Workflow log: {workflow_log_path}")
    return int(exit_code or 0)


def main() -> int:
    args = parse_args()
    logger, log_path = setup_logging()
    logger.info("==== GPT-SoVITS TTS workflow started ====")
    try:
        config_path = resolve_path(args.config)
        logger.info("Loading config: %s", config_path)
        config = load_json(config_path)
        runtime_paths = validate_runtime_paths(config)
        runtime_paths = prepare_workflow_tts_config(runtime_paths, logger)
        tts_url = args.api_url or config.get("api", {}).get("tts_url") or "http://127.0.0.1:9880/tts"
        ensure_local_api_url(tts_url)
        logger.info("TTS URL: %s", tts_url)

        if args.foreground_start_api:
            return run_foreground_start_api(runtime_paths, logger, log_path)

        if args.debug_start_api:
            return run_debug_start_api(config, tts_url, runtime_paths, logger, log_path)

        reference_assets = validate_reference_assets(config)
        reference_assets = prepare_reference_assets_for_api(reference_assets, logger)

        output_cfg = config.get("output", {})
        media_type = str(output_cfg.get("format") or "wav").lower().lstrip(".")
        output_dir = resolve_path(args.output_dir or output_cfg.get("dir", "outputs/tts"))
        ensure_output_dir(output_dir)
        log_validated_paths(
            logger,
            config_path,
            runtime_paths,
            reference_assets,
            output_dir,
        )

        if args.test:
            print_tts_config_inspection(inspect_tts_config(runtime_paths, logger))
            return run_test_mode(
                config,
                tts_url,
                runtime_paths,
                reference_assets,
                output_dir,
                logger,
                log_path,
                skip_model_switch=args.skip_model_switch,
            )

        text = read_input_text(args)
        logger.info("Input text length: %s chars", len(text))

        inspect_tts_config(runtime_paths, logger)
        health = start_api_if_needed(config, tts_url, runtime_paths, logger)
        logger.info(
            "API health ready: reachable=%s url=%s status=%s detail=%s",
            health.reachable,
            health.url,
            health.status,
            health.detail,
        )
        if args.skip_model_switch:
            logger.info("Model switch skipped by --skip-model-switch")
            print("Model switch: skipped")
        else:
            apply_model_weights(
                runtime_paths,
                tts_url,
                config,
                float(config.get("api", {}).get("request_timeout_seconds", 300)),
                logger,
            )

        payload = build_payload(
            config,
            text,
            reference_assets.audio_path,
            reference_assets.text,
            media_type,
        )
        logger.info("Calling /tts")
        audio = request_tts(config, tts_url, payload)
        logger.info("Received audio bytes: %s", len(audio))
        output_path = save_audio(audio, output_dir, media_type)
        logger.info("Saved audio: %s", output_path)

        print(f"Saved audio: {output_path}")
        print(f"Workflow log: {log_path}")
        if output_cfg.get("open_after_generate", False) and not args.no_open:
            open_audio(output_path, logger)
        return 0
    except TtsError as exc:
        logger.exception("Workflow failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        print(f"Workflow log: {log_path}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        print("Error: interrupted by user.", file=sys.stderr)
        print(f"Workflow log: {log_path}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
