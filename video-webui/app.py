#!/usr/bin/env python3
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_ROOT / "config.json"
BOOT_LOG = APP_ROOT / "server_boot.log"
GPT_WEIGHT_DIR_NAMES = (
    "GPT_weights",
    "GPT_weights_v2",
    "GPT_weights_v2Pro",
    "GPT_weights_v2ProPlus",
    "GPT_weights_v3",
    "GPT_weights_v4",
)
SOVITS_WEIGHT_DIR_NAMES = (
    "SoVITS_weights",
    "SoVITS_weights_v2",
    "SoVITS_weights_v2Pro",
    "SoVITS_weights_v2ProPlus",
    "SoVITS_weights_v3",
    "SoVITS_weights_v4",
)
LANGUAGE_OPTIONS = [
    ("中文", "zh"),
    ("英文", "en"),
    ("日文", "ja"),
    ("韩文", "ko"),
    ("粤语", "yue"),
]
TEXT_SPLIT_OPTIONS = [
    ("不切 cut0", "cut0"),
    ("凑四句一切 cut1", "cut1"),
    ("凑50字一切 cut2", "cut2"),
    ("按中文句号切 cut3", "cut3"),
    ("按英文句号切 cut4", "cut4"),
    ("按标点符号切 cut5", "cut5"),
]
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"}
BASE_MODEL_VALUE = "__use_pretrained_base__"
BASE_MODEL_LABEL = "不使用模型（底模推理）"
BROWSER_HEARTBEAT_INTERVAL_SECONDS = 5.0
BROWSER_UNLOAD_EXIT_DELAY_SECONDS = 20.0
BROWSER_HEARTBEAT_LOCK = threading.Lock()
BROWSER_HEARTBEAT_LAST = 0.0
BROWSER_HEARTBEAT_COUNTER = 0
APP_CSS = """
#video-webui .gradio-container {
    max-width: 1680px !important;
    padding-top: 10px !important;
}
#video-webui h1 {
    font-size: 22px !important;
    line-height: 1.15 !important;
    margin: 0 0 2px 0 !important;
}
#video-webui .prose {
    margin-bottom: 0 !important;
}
#video-webui .app-header {
    align-items: end !important;
    gap: 10px !important;
}
#video-webui .main-grid,
#video-webui .command-bar,
#video-webui .dense-row {
    gap: 8px !important;
}
#video-webui .tabs {
    margin-top: 0 !important;
}
#video-webui .tabitem {
    padding: 8px !important;
}
#video-webui .form {
    gap: 6px !important;
}
#video-webui .block {
    border-radius: 8px !important;
}
#video-webui label,
#video-webui .label-wrap {
    font-size: 12px !important;
}
#video-webui button {
    min-height: 34px !important;
}
#video-webui textarea {
    min-height: 68px !important;
}
#video-webui .source-text textarea {
    min-height: 260px !important;
}
#video-webui .reference-text textarea {
    min-height: 96px !important;
}
#video-webui .status-box textarea {
    min-height: 168px !important;
    font-family: Consolas, "Microsoft YaHei UI", monospace !important;
    font-size: 12px !important;
}
#video-webui .srt-editor textarea {
    min-height: 240px !important;
    font-family: Consolas, "Microsoft YaHei UI", monospace !important;
    font-size: 12px !important;
}
#video-webui .preview-media img,
#video-webui .preview-media video {
    max-height: 360px !important;
    object-fit: contain !important;
}
"""


def boot_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with BOOT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def configure_local_proxy_bypass() -> None:
    local_hosts = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        parts = [item.strip() for item in existing.split(",") if item.strip()]
        for host in local_hosts:
            if host not in parts:
                parts.append(host)
        os.environ[key] = ",".join(parts)
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")


def patch_gradio_local_url_check() -> None:
    """Avoid Gradio startup failure when a system proxy intercepts localhost checks."""
    try:
        import gradio.networking as networking  # type: ignore
    except Exception as exc:
        boot_log(f"could not import gradio.networking for patch: {exc}")
        return

    original_url_ok = networking.url_ok

    def url_ok(url: str) -> bool:
        if url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return True
        return original_url_ok(url)

    networking.url_ok = url_ok
    boot_log("patched gradio localhost url_ok check")


def browser_unload_exit_worker(counter_at_unload: int) -> None:
    boot_log("browser unload observed; waiting before exit")
    time.sleep(BROWSER_UNLOAD_EXIT_DELAY_SECONDS)
    with BROWSER_HEARTBEAT_LOCK:
        still_unloaded = BROWSER_HEARTBEAT_COUNTER <= counter_at_unload
    if still_unloaded:
        boot_log("browser unload confirmed; exiting WebUI process")
        os._exit(0)


def note_browser_heartbeat() -> str:
    global BROWSER_HEARTBEAT_LAST, BROWSER_HEARTBEAT_COUNTER
    with BROWSER_HEARTBEAT_LOCK:
        BROWSER_HEARTBEAT_LAST = time.monotonic()
        BROWSER_HEARTBEAT_COUNTER += 1
    return ""


def request_browser_unload_exit() -> None:
    with BROWSER_HEARTBEAT_LOCK:
        counter_at_unload = BROWSER_HEARTBEAT_COUNTER
    thread = threading.Thread(target=browser_unload_exit_worker, args=(counter_at_unload,), daemon=True)
    thread.start()


@dataclass(frozen=True)
class AppConfig:
    path: Path
    raw: dict[str, Any]

    @property
    def jobs_dir(self) -> Path:
        value = str(self.raw.get("jobs_dir") or "jobs")
        return resolve_path(value, self.path.parent)

    @property
    def project_root(self) -> Path:
        return resolve_path(str(self.raw["project_root"]), self.path.parent)

    @property
    def python_exe(self) -> Path:
        return resolve_path(str(self.raw.get("python_exe") or sys.executable), self.path.parent)

    @property
    def gptsovits_root(self) -> Path:
        raw_root = self.raw.get("gptsovits_root")
        if raw_root:
            return resolve_path(str(raw_root), self.path.parent)
        runtime_parent = self.python_exe.parent
        if runtime_parent.name.lower() == "runtime":
            return runtime_parent.parent.resolve()
        return self.project_root

    @property
    def gsv_tts_script(self) -> Path:
        return resolve_path(str(self.raw["gsv_tts_script"]), self.path.parent)

    @property
    def tts_checker_script(self) -> Path:
        return resolve_path(str(self.raw["tts_checker_script"]), self.path.parent)

    @property
    def video_script(self) -> Path:
        return resolve_path(str(self.raw["video_script"]), self.path.parent)

    @property
    def ffmpeg(self) -> Path:
        return resolve_path(str(self.raw["ffmpeg"]), self.path.parent)

    @property
    def ffprobe(self) -> Path:
        return resolve_path(str(self.raw["ffprobe"]), self.path.parent)

    @property
    def asr_model(self) -> Path:
        return resolve_path(str(self.raw["asr_model"]), self.path.parent)

    @property
    def models(self) -> list[dict[str, Any]]:
        return list(self.raw.get("models") or [])

    @property
    def gpt_weights_dirs(self) -> list[Path]:
        configured = self.raw.get("gpt_weights_dirs")
        if configured:
            return [resolve_path(str(value), self.path.parent) for value in configured]
        return [(self.gptsovits_root / name).resolve() for name in GPT_WEIGHT_DIR_NAMES]

    @property
    def sovits_weights_dirs(self) -> list[Path]:
        configured = self.raw.get("sovits_weights_dirs")
        if configured:
            return [resolve_path(str(value), self.path.parent) for value in configured]
        return [(self.gptsovits_root / name).resolve() for name in SOVITS_WEIGHT_DIR_NAMES]

    @property
    def reference_assets(self) -> list[dict[str, Any]]:
        assets = list(self.raw.get("reference_assets") or [])
        seen = {str(asset.get("id") or "") for asset in assets}
        seen_audio = {str(asset.get("ref_audio_path") or "") for asset in assets}
        for model in self.models:
            audio_path = model.get("ref_audio_path")
            text_path = model.get("ref_text_path")
            if not audio_path:
                continue
            asset_id = str(model.get("reference_id") or f"{model.get('id') or 'model'}_reference")
            if asset_id in seen or str(audio_path) in seen_audio:
                continue
            assets.append(
                {
                    "id": asset_id,
                    "name": f"{model.get('name') or model.get('id')} 默认参考",
                    "ref_audio_path": audio_path,
                    "ref_text_path": text_path,
                    "prompt_lang": model.get("prompt_lang", "zh"),
                }
            )
            seen.add(asset_id)
            seen_audio.add(str(audio_path))
        return assets

    @property
    def host(self) -> str:
        return str(self.raw.get("host") or "127.0.0.1")

    @property
    def port(self) -> int:
        return int(self.raw.get("port") or 7860)


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def is_weight_relative_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return False
    path = Path(text)
    if path.is_absolute():
        return False
    first = text.split("/", 1)[0]
    return first in set(GPT_WEIGHT_DIR_NAMES + SOVITS_WEIGHT_DIR_NAMES + ("GPT_SoVITS",))


def resolve_weight_path(config: AppConfig, value: Any) -> Path:
    text = str(value or "").strip()
    if is_base_model_choice(text):
        return Path("")
    if not text:
        return Path("")
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    if is_weight_relative_path(text):
        return (config.gptsovits_root / path).resolve()
    project_candidate = resolve_path(text, config.path.parent)
    if project_candidate.exists():
        return project_candidate
    root_candidate = (config.gptsovits_root / path).resolve()
    if root_candidate.exists():
        return root_candidate
    return project_candidate


def resolve_optional_weight_path(config: AppConfig, value: Any) -> Path | None:
    if value is None or str(value).strip() == "" or is_base_model_choice(value):
        return None
    return resolve_weight_path(config, value)


def is_base_model_choice(value: Any) -> bool:
    text = str(value or "").strip()
    return text == BASE_MODEL_VALUE or text == BASE_MODEL_LABEL


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        example = path.with_name("config.example.json")
        if example.exists():
            shutil.copy2(example, path)
        else:
            raise FileNotFoundError(f"Config not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return AppConfig(path=path.resolve(), raw=raw)


def save_config(config: AppConfig) -> None:
    config.path.write_text(json.dumps(config.raw, ensure_ascii=False, indent=2), encoding="utf-8")


def reload_config(config: AppConfig) -> None:
    fresh = json.loads(config.path.read_text(encoding="utf-8-sig"))
    config.raw.clear()
    config.raw.update(fresh)


def now_slug() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def safe_name(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", value)
    return value[:80] or "job"


def short_digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:10]


def ensure_job(config: AppConfig, job_name: str | None = None) -> Path:
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    suffix = safe_name(job_name or "video")
    job = config.jobs_dir / f"{now_slug()}_{suffix}"
    counter = 1
    while job.exists():
        job = config.jobs_dir / f"{now_slug()}_{suffix}_{counter}"
        counter += 1
    for child in ("input", "tts", "asr", "render", "checks", "logs", "tmp"):
        (job / child).mkdir(parents=True, exist_ok=True)
    return job


def copy_upload(file_obj: Any, target: Path) -> Path | None:
    if file_obj is None:
        return None
    source = Path(getattr(file_obj, "name", str(file_obj)))
    if not source.exists():
        raise FileNotFoundError(f"Uploaded file does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == "":
        target = target.with_suffix(source.suffix.lower())
    shutil.copy2(source, target)
    return target.resolve()


def write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value or "", encoding="utf-8-sig")
    return path.resolve()


def process_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def config_int(config: AppConfig, key: str, default: int) -> int:
    try:
        return int(config.raw.get(key) or default)
    except (TypeError, ValueError):
        return default


def config_float(config: AppConfig, key: str, default: float) -> float:
    try:
        return float(config.raw.get(key) or default)
    except (TypeError, ValueError):
        return default


def run_command(command: list[str], cwd: Path, log_path: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    header = "Started: " + started + "\nCommand: " + subprocess.list2cmdline(command) + "\n\n"
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = process_output_text(exc.stdout)
        stderr = process_output_text(exc.stderr)
        timeout_message = f"Timed out after {timeout} seconds."
        if stderr:
            stderr += "\n" + timeout_message
        else:
            stderr = timeout_message
        completed = subprocess.CompletedProcess(command, 124, stdout, stderr)
    log_path.write_text(
        header
        + "Return code: "
        + str(completed.returncode)
        + "\n\n"
        + "=== STDOUT ===\n"
        + completed.stdout
        + "\n=== STDERR ===\n"
        + completed.stderr,
        encoding="utf-8",
    )
    return completed


def require_paths(config: AppConfig) -> list[str]:
    checks = {
        "python_exe": config.python_exe,
        "gsv_tts_script": config.gsv_tts_script,
        "tts_checker_script": config.tts_checker_script,
        "video_script": config.video_script,
        "ffmpeg": config.ffmpeg,
        "ffprobe": config.ffprobe,
        "asr_model": config.asr_model,
    }
    missing = [f"{name}: {path}" for name, path in checks.items() if not path.exists()]
    return missing


def model_label(model: dict[str, Any]) -> str:
    return f"{model.get('name') or model.get('id')} [{model.get('id')}]"


def model_by_label(config: AppConfig, label: str) -> dict[str, Any]:
    for model in config.models:
        if model_label(model) == label:
            return model
    if config.models:
        return config.models[0]
    raise ValueError("No models configured in config.json")


def update_model_inference_preset(
    config: AppConfig,
    label: str,
    text_split_method: str,
    speed_factor: float,
    fragment_interval: float,
    top_k: float,
    top_p: float,
    temperature: float,
) -> str:
    selected = model_by_label(config, label)
    selected_id = str(selected.get("id") or "")
    models = list(config.raw.get("models") or [])
    updated = False
    for model in models:
        if selected_id and str(model.get("id") or "") == selected_id:
            model["text_split_method"] = text_split_method or "cut1"
            model["speed_factor"] = float(speed_factor or 1.0)
            model["fragment_interval"] = float(fragment_interval or 0.3)
            model["top_k"] = int(float(top_k or 15))
            model["top_p"] = float(top_p or 1.0)
            model["temperature"] = float(temperature or 1.0)
            updated = True
            break
    if not updated:
        raise ValueError(f"Could not find model preset to update: {label}")
    config.raw["models"] = models
    save_config(config)
    reload_config(config)
    return "\n".join(
        [
            "推理参数已保存到当前角色预设",
            f"Preset: {label}",
            f"text_split_method: {text_split_method or 'cut1'}",
            f"speed_factor: {float(speed_factor or 1.0):.2f}",
            f"fragment_interval: {float(fragment_interval or 0.3):.2f}",
            f"top_k: {int(float(top_k or 15))}",
            f"top_p: {float(top_p or 1.0):.2f}",
            f"temperature: {float(temperature or 1.0):.2f}",
            f"Config: {config.path}",
        ]
    )


def language_choices() -> list[tuple[str, str]]:
    return [(f"{name} ({code})", code) for name, code in LANGUAGE_OPTIONS]


def text_split_choices() -> list[tuple[str, str]]:
    return list(TEXT_SPLIT_OPTIONS)


def asr_language(code: str) -> str:
    return "zh" if code == "yue" else (code or "zh")


def resolve_optional_path(value: Any, base: Path) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return resolve_path(str(value), base)


def unique_existing_files(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    output: list[Path] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(path.resolve())
    return sorted(output, key=lambda item: str(item).lower())


def discover_weight_files(config: AppConfig, dirs: list[Path], suffix: str, model_key: str) -> list[Path]:
    found: list[Path] = []
    for folder in dirs:
        if not folder.exists():
            continue
        found.extend(path for path in folder.rglob(f"*{suffix}") if path.is_file())
    for model in config.models:
        path = resolve_optional_weight_path(config, model.get(model_key))
        if path:
            found.append(path)
    return unique_existing_files(found)


def gpt_weight_files(config: AppConfig) -> list[Path]:
    return discover_weight_files(config, config.gpt_weights_dirs, ".ckpt", "gpt_weights_path")


def sovits_weight_files(config: AppConfig) -> list[Path]:
    return discover_weight_files(config, config.sovits_weights_dirs, ".pth", "sovits_weights_path")


def file_choice_label(config: AppConfig, path: Path) -> str:
    try:
        return path.relative_to(config.gptsovits_root).as_posix()
    except ValueError:
        return str(path)


def file_choices(config: AppConfig, paths: list[Path]) -> list[tuple[str, str]]:
    return [(BASE_MODEL_LABEL, BASE_MODEL_VALUE), *[(file_choice_label(config, path), str(path)) for path in paths]]


def first_choice_value(choices: list[tuple[str, str]]) -> str:
    return choices[0][1] if choices else ""


def existing_or_first(value: Any, choices: list[tuple[str, str]], base: Path) -> str:
    path = resolve_optional_path(value, base)
    if path and path.exists():
        return str(path)
    return first_choice_value(choices)


def existing_weight_or_first(config: AppConfig, value: Any, choices: list[tuple[str, str]]) -> str:
    if is_base_model_choice(value):
        return BASE_MODEL_VALUE
    path = resolve_optional_weight_path(config, value)
    if path and path.exists():
        return str(path)
    return first_choice_value(choices)


def matching_sovits_for_gpt(config: AppConfig, gpt_weights_path: str) -> Path | None:
    if is_base_model_choice(gpt_weights_path):
        return None
    gpt_path = resolve_optional_weight_path(config, gpt_weights_path)
    if not gpt_path:
        return None
    target_name = gpt_path.with_suffix(".pth").name.lower()
    for path in sovits_weight_files(config):
        if path.name.lower() == target_name:
            return path
    return None


def reference_label(asset: dict[str, Any]) -> str:
    return f"{asset.get('name') or asset.get('id')} [{asset.get('id')}]"


def reference_choices(config: AppConfig) -> list[tuple[str, str]]:
    return [(reference_label(asset), str(asset.get("id"))) for asset in config.reference_assets]


def reference_by_id(config: AppConfig, ref_id: str) -> dict[str, Any] | None:
    for asset in config.reference_assets:
        if str(asset.get("id")) == str(ref_id):
            return asset
    return config.reference_assets[0] if config.reference_assets else None


def normalized_path_key(value: Any, base: Path) -> str:
    path = resolve_optional_path(value, base)
    if path:
        return str(path).replace("\\", "/").lower()
    return str(value or "").replace("\\", "/").lower()


def model_by_gpt_weights(config: AppConfig, gpt_weights_path: str) -> dict[str, Any] | None:
    if is_base_model_choice(gpt_weights_path):
        return None
    target_path = resolve_optional_weight_path(config, gpt_weights_path)
    target = str(target_path).replace("\\", "/").lower() if target_path else normalized_path_key(gpt_weights_path, config.path.parent)
    if not target:
        return None
    for model in config.models:
        model_path = resolve_optional_weight_path(config, model.get("gpt_weights_path"))
        model_key = str(model_path).replace("\\", "/").lower() if model_path else normalized_path_key(model.get("gpt_weights_path"), config.path.parent)
        if model_key == target:
            return model
    return None


def default_reference_id(config: AppConfig, model: dict[str, Any]) -> str:
    explicit = model.get("reference_id")
    if explicit and reference_by_id(config, str(explicit)):
        return str(explicit)
    audio = str(model.get("ref_audio_path") or "")
    for asset in config.reference_assets:
        if audio and str(asset.get("ref_audio_path") or "") == audio:
            return str(asset.get("id"))
    return str(config.reference_assets[0].get("id")) if config.reference_assets else ""


def read_asset_reference_text(config: AppConfig, asset: dict[str, Any] | None) -> str:
    if not asset:
        return ""
    path = resolve_optional_path(asset.get("ref_text_path"), config.path.parent)
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def reference_audio_path_for_asset(config: AppConfig, asset: dict[str, Any] | None) -> Path | None:
    if not asset:
        return None
    return resolve_optional_path(asset.get("ref_audio_path"), config.path.parent)


def save_reference_model_preset(
    config: AppConfig,
    preset_name: str,
    gpt_weights_path: str,
    sovits_weights_path: str,
    reference_id: str,
    uploaded_ref_audio: Any,
    reference_text: str,
    prompt_lang: str,
    text_lang: str,
    text_split_method: str,
    speed_factor: float,
    fragment_interval: float,
    top_k: float,
    top_p: float,
    temperature: float,
) -> tuple[str, str, str]:
    base_mode = is_base_model_choice(gpt_weights_path) or is_base_model_choice(sovits_weights_path)
    if base_mode:
        gpt_path: Path | None = None
        sovits_path: Path | None = None
    else:
        gpt_path = resolve_weight_path(config, gpt_weights_path)
        sovits_path = resolve_weight_path(config, sovits_weights_path)
        if not gpt_path.exists():
            raise FileNotFoundError(f"GPT weights not found: {gpt_path}")
        if not sovits_path.exists():
            raise FileNotFoundError(f"SoVITS weights not found: {sovits_path}")

    clean_name = (preset_name or (gpt_path.stem if gpt_path else "base_model")).strip()
    safe = safe_name(clean_name)
    digest = short_digest((str(gpt_path.resolve()).lower() if gpt_path else f"base:{clean_name}"))
    model_id = f"preset_{safe}_{digest}"
    asset_id = f"ref_{safe}_{digest}"

    preset_dir = config.path.parent / "presets" / "reference_assets" / safe
    preset_dir.mkdir(parents=True, exist_ok=True)

    source_audio: Path | None = None
    if uploaded_ref_audio is not None:
        source_audio = Path(getattr(uploaded_ref_audio, "name", str(uploaded_ref_audio)))
    if source_audio is None or not source_audio.exists():
        current_asset = reference_by_id(config, reference_id)
        source_audio = reference_audio_path_for_asset(config, current_asset)
    if source_audio is None or not source_audio.exists():
        raise FileNotFoundError("No reference audio available. Upload one or choose an existing reference preset.")

    audio_target = preset_dir / f"reference_audio{source_audio.suffix.lower() or '.wav'}"
    shutil.copy2(source_audio, audio_target)

    clean_text = (reference_text or "").strip()
    if not clean_text:
        current_asset = reference_by_id(config, reference_id)
        clean_text = read_asset_reference_text(config, current_asset)
    if not clean_text:
        raise ValueError("Reference text is empty. Fill it before saving the preset.")
    text_target = write_text(preset_dir / "reference_text.txt", clean_text)

    rel_audio = audio_target.resolve().as_posix()
    rel_text = text_target.resolve().as_posix()
    rel_gpt = BASE_MODEL_VALUE if base_mode else gpt_path.resolve().as_posix()
    rel_sovits = BASE_MODEL_VALUE if base_mode else sovits_path.resolve().as_posix()

    assets = list(config.raw.get("reference_assets") or [])
    asset_payload = {
        "id": asset_id,
        "name": clean_name,
        "ref_audio_path": rel_audio,
        "ref_text_path": rel_text,
        "prompt_lang": prompt_lang or "zh",
    }
    replaced_asset = False
    for index, asset in enumerate(assets):
        if str(asset.get("id")) == asset_id:
            assets[index] = asset_payload
            replaced_asset = True
            break
    if not replaced_asset:
        assets.append(asset_payload)
    config.raw["reference_assets"] = assets

    base_config = ""
    if config.models:
        base_config = str(config.models[0].get("base_config") or "")
    model_payload = {
        "id": model_id,
        "name": clean_name,
        "base_config": base_config,
        "default_version": "v2",
        "gpt_weights_path": rel_gpt,
        "sovits_weights_path": rel_sovits,
        "use_pretrained_base": bool(base_mode),
        "reference_id": asset_id,
        "ref_audio_path": rel_audio,
        "ref_text_path": rel_text,
        "prompt_lang": prompt_lang or "zh",
        "text_lang": text_lang or "zh",
        "speed_factor": float(speed_factor),
        "fragment_interval": float(fragment_interval),
        "text_split_method": text_split_method or "cut1",
        "top_k": int(float(top_k)),
        "top_p": float(top_p),
        "temperature": float(temperature),
    }

    models = list(config.raw.get("models") or [])
    target_key = normalized_path_key(rel_gpt, config.path.parent)
    replaced_model = False
    for index, model in enumerate(models):
        matches_model = (
            str(model.get("id") or "") == model_id
            if base_mode
            else normalized_path_key(model.get("gpt_weights_path"), config.path.parent) == target_key
        )
        if matches_model:
            model_payload["id"] = str(model.get("id") or model_id)
            models[index] = model_payload
            replaced_model = True
            break
    if not replaced_model:
        models.append(model_payload)
    config.raw["models"] = models

    save_config(config)
    reload_config(config)
    return model_label(model_payload), asset_id, f"已保存参考模型预设: {clean_name}\nAudio: {audio_target}\nText: {text_target}\nConfig: {config.path}"


def load_base_voice_config(config: AppConfig, model: dict[str, Any]) -> dict[str, Any]:
    base_config = resolve_path(str(model.get("base_config") or ""), config.path.parent)
    if not base_config.exists() or not base_config.is_file():
        return {}
    return json.loads(base_config.read_text(encoding="utf-8-sig"))


def model_setting(config: AppConfig, model: dict[str, Any], key: str, default: Any) -> Any:
    if model.get(key) is not None:
        return model[key]
    base = load_base_voice_config(config, model)
    if key in ("prompt_lang", "text_lang", "ref_audio_path", "ref_text_path"):
        return base.get("voice", {}).get(key, default)
    if key in ("top_k", "top_p", "temperature", "repetition_penalty", "sample_steps"):
        return base.get("sampling", {}).get(key, default)
    if key in ("speed_factor", "fragment_interval", "text_split_method"):
        return base.get("extra_request_params", {}).get(key, default)
    return default


def build_tts_overrides(
    config: AppConfig,
    job: Path,
    model: dict[str, Any],
    gpt_weights_path: str,
    sovits_weights_path: str,
    reference_id: str,
    uploaded_ref_audio: Any,
    reference_text: str,
    no_ref_text: bool,
    prompt_lang: str,
    text_lang: str,
    text_split_method: str,
    speed_factor: float,
    fragment_interval: float,
    top_k: float,
    top_p: float,
    temperature: float,
) -> dict[str, Any]:
    default_version = str(model.get("default_version") or "v2")
    base_mode = (
        is_base_model_choice(gpt_weights_path)
        or is_base_model_choice(sovits_weights_path)
        or bool(model.get("use_pretrained_base"))
    )
    if base_mode:
        gpt_path: Path | None = None
        sovits_path: Path | None = None
    else:
        gpt_path = resolve_weight_path(config, gpt_weights_path)
        sovits_path = resolve_weight_path(config, sovits_weights_path)
        if not gpt_path.exists():
            raise FileNotFoundError(f"GPT weights not found: {gpt_path}")
        if not sovits_path.exists():
            raise FileNotFoundError(f"SoVITS weights not found: {sovits_path}")

    asset = reference_by_id(config, reference_id)
    ref_audio_path: Path | None = None
    if uploaded_ref_audio is not None:
        ref_audio_path = copy_upload(uploaded_ref_audio, job / "input" / "reference_audio")
    elif asset:
        ref_audio_path = resolve_optional_path(asset.get("ref_audio_path"), config.path.parent)
    if ref_audio_path is None:
        ref_audio_path = resolve_optional_path(model.get("ref_audio_path"), config.path.parent)
    if ref_audio_path is None or not ref_audio_path.exists():
        raise ValueError("请先选择或上传参考音频。")

    ref_text_path = resolve_optional_path(model.get("ref_text_path"), config.path.parent)
    if asset and asset.get("ref_text_path"):
        ref_text_path = resolve_optional_path(asset.get("ref_text_path"), config.path.parent)

    extra: dict[str, Any] = {
        "speed_factor": float(speed_factor or 1.0),
        "fragment_interval": float(fragment_interval or 0.3),
        "text_split_method": text_split_method or "cut1",
    }
    if no_ref_text:
        version_key = default_version.lower() if base_mode else str(sovits_path).lower()
        if "v3" in version_key or "v4" in version_key:
            raise ValueError("v3 / v4 vocoder 模型不支持无参考文本模式，请填写参考文本。")
        extra["prompt_text"] = ""
    else:
        clean_ref_text = (reference_text or "").strip()
        if not clean_ref_text:
            raise ValueError("请填写参考音频文本，或开启无参考文本模式。")
        ref_text_path = write_text(job / "input" / "reference_text.txt", clean_ref_text)

    return {
        "gpt_weights_path": None if base_mode else str(gpt_path),
        "sovits_weights_path": None if base_mode else str(sovits_path),
        "ref_audio_path": str(ref_audio_path),
        "ref_text_path": str(ref_text_path) if ref_text_path else None,
        "prompt_lang": prompt_lang or "zh",
        "text_lang": text_lang or "zh",
        "model_switch": {
            "default_version": default_version,
            "use_pretrained_base": bool(base_mode),
        },
        "sampling": {
            "top_k": int(top_k or 15),
            "top_p": float(top_p or 1.0),
            "temperature": float(temperature or 1.0),
        },
        "extra_request_params": extra,
    }


def merge_voice_config(
    config: AppConfig,
    model: dict[str, Any],
    output: Path,
    overrides: dict[str, Any] | None = None,
) -> Path:
    base_config = resolve_path(str(model.get("base_config") or ""), config.path.parent)
    if not base_config.exists():
        raise FileNotFoundError(f"Base voice config not found: {base_config}")
    payload = json.loads(base_config.read_text(encoding="utf-8-sig"))
    overrides = overrides or {}
    model_switch = payload.setdefault("model_switch", {})
    if isinstance(overrides.get("model_switch"), dict):
        for key, value in overrides["model_switch"].items():
            if value is not None:
                model_switch[key] = value
    if model.get("default_version"):
        model_switch["default_version"] = model["default_version"]
    use_base = bool(model.get("use_pretrained_base")) or bool(model_switch.get("use_pretrained_base"))
    if use_base:
        model_switch["use_pretrained_base"] = True
        payload.pop("gpt_weights_path", None)
        payload.pop("sovits_weights_path", None)
    else:
        for key in ("gpt_weights_path", "sovits_weights_path"):
            value = overrides.get(key) or model.get(key)
            if value:
                payload[key] = str(resolve_weight_path(config, value))
    voice = payload.setdefault("voice", {})
    for model_key, voice_key in (
        ("ref_audio_path", "ref_audio_path"),
        ("ref_text_path", "ref_text_path"),
        ("prompt_lang", "prompt_lang"),
        ("text_lang", "text_lang"),
    ):
        value = overrides.get(model_key) or model.get(model_key)
        if value:
            voice[voice_key] = value
    extra = payload.setdefault("extra_request_params", {})
    for model_key in ("speed_factor", "fragment_interval", "text_split_method"):
        value = overrides.get("extra_request_params", {}).get(model_key)
        if value is None:
            value = model.get(model_key)
        if value is not None:
            extra[model_key] = value
    for key, value in overrides.get("extra_request_params", {}).items():
        if value is not None:
            extra[key] = value
    sampling = payload.setdefault("sampling", {})
    for key, value in overrides.get("sampling", {}).items():
        if value is not None:
            sampling[key] = value
    payload.setdefault("output", {})["open_after_generate"] = False
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output.resolve()


def parse_saved_audio(stdout: str, output_dir: Path) -> Path | None:
    match = re.search(r"Saved audio:\s*(.+)", stdout)
    if match:
        path = Path(match.group(1).strip().strip('"'))
        if path.exists():
            return path.resolve()
    audio_files = sorted(
        [p for p in output_dir.glob("*") if p.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return audio_files[0].resolve() if audio_files else None


def parse_json_from_stdout(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start >= 0 and end >= start:
        return json.loads(stdout[start : end + 1])
    return {}


def srt_timestamp(seconds: float) -> str:
    millis = int(max(0.0, seconds) * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt_timestamp(value: str) -> float:
    match = re.match(r"\s*(\d+):(\d+):(\d+),(\d+)\s*$", value)
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = [int(part) for part in match.groups()]
    return (hours * 3600) + (minutes * 60) + seconds + (millis / 1000.0)


def make_srt_block(index: int, start: float, end: float, text: str) -> str:
    return "\n".join([str(index), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", text.strip()])


def strip_srt_text(srt_text: str) -> str:
    lines = []
    for line in srt_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        lines.append(stripped)
    return "".join(lines)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def local_srt_report(source_text: str, srt_text: str) -> str:
    expected = normalize_text(source_text)
    actual = normalize_text(strip_srt_text(srt_text))
    distance = levenshtein(expected, actual) if expected else 0
    cer = distance / max(1, len(expected))
    status = "PASS" if cer <= 0.08 else "REVIEW"
    return "\n".join(
        [
            f"Local SRT check: {status}",
            f"CER: {cer:.2%} ({distance}/{len(expected)} chars)",
            f"Expected chars: {len(expected)}",
            f"SRT chars: {len(actual)}",
        ]
    )


def parse_srt_blocks(srt_text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    chunks = re.split(r"\n\s*\n", (srt_text or "").strip())
    for chunk in chunks:
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        index = lines[0] if lines[0].isdigit() else str(len(blocks) + 1)
        time_line = ""
        text_lines: list[str] = []
        for line in lines[1:] if lines[0].isdigit() else lines:
            if "-->" in line and not time_line:
                time_line = line
            elif "-->" not in line:
                text_lines.append(line)
        text = "\n".join(text_lines).strip()
        if text:
            blocks.append({"index": index, "time": time_line, "text": text})
    return blocks


def srt_segments_from_text(srt_text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for fallback_index, block in enumerate(parse_srt_blocks(srt_text), start=1):
        match = re.match(
            r"\s*(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)",
            str(block.get("time") or ""),
        )
        if not match:
            continue
        start = parse_srt_timestamp(match.group(1))
        end = parse_srt_timestamp(match.group(2))
        text = str(block.get("text") or "").strip()
        if text and end > start:
            segments.append({"index": fallback_index, "start": start, "end": end, "text": text})
    return segments


def split_review_chunks(text: str) -> list[str]:
    return split_tts_chunks(text, 90)


def best_fuzzy_window(expected: str, actual: str) -> tuple[float, float, int, int, str]:
    expected = normalize_text(expected)
    actual = normalize_text(actual)
    if not expected:
        return 1.0, 1.0, 0, 0, ""
    if not actual:
        return 0.0, 0.0, 0, 0, ""
    direct = actual.find(expected)
    if direct >= 0:
        return 1.0, 1.0, direct, direct + len(expected), actual[direct : direct + len(expected)]

    target = len(expected)
    max_window = min(len(actual), max(target + 16, int(target * 1.55)))
    step = max(1, target // 4)
    best_score = -1.0
    best_coverage = 0.0
    best_start = 0
    best_end = min(len(actual), max_window)
    best_window = actual[best_start:best_end]
    for start in range(0, max(1, len(actual) - 1), step):
        end = min(len(actual), start + max_window)
        if end <= start:
            continue
        window = actual[start:end]
        matcher = SequenceMatcher(None, expected, window)
        ratio = matcher.ratio()
        match = matcher.find_longest_match(0, len(expected), 0, len(window))
        coverage = match.size / max(1, len(expected))
        score = (ratio * 0.55) + (coverage * 0.45)
        if score > best_score:
            best_score = score
            best_coverage = coverage
            best_start = start
            best_end = end
            best_window = window
    return max(0.0, best_score), best_coverage, best_start, best_end, best_window


def local_second_review(source_text: str, srt_text: str) -> tuple[str, str]:
    expected = normalize_text(source_text)
    actual_text = strip_srt_text(srt_text)
    actual = normalize_text(actual_text)
    distance = levenshtein(expected, actual) if expected else 0
    cer = distance / max(1, len(expected))
    chunks = split_review_chunks(source_text)
    blocks = parse_srt_blocks(srt_text)

    issues: list[dict[str, Any]] = []
    weak_count = 0
    missing_count = 0
    for index, chunk in enumerate(chunks, start=1):
        score, coverage, _start, _end, matched = best_fuzzy_window(chunk, actual_text)
        if score < 0.55 or coverage < 0.42:
            status = "MISSING"
            missing_count += 1
        elif score < 0.78 or coverage < 0.70:
            status = "WEAK"
            weak_count += 1
        else:
            status = "OK"
        if status != "OK":
            issues.append(
                {
                    "index": index,
                    "status": status,
                    "score": score,
                    "coverage": coverage,
                    "expected": chunk,
                    "matched": matched,
                }
            )

    edge_warnings: list[str] = []
    if chunks:
        first_score, first_cov, *_ = best_fuzzy_window(chunks[0], actual_text)
        last_score, last_cov, *_ = best_fuzzy_window(chunks[-1], actual_text)
        if first_score < 0.78 or first_cov < 0.70:
            edge_warnings.append("开头片段弱匹配，可能存在掐头或首句漏读。")
        if last_score < 0.78 or last_cov < 0.70:
            edge_warnings.append("结尾片段弱匹配，可能存在去尾或末句漏读。")

    if missing_count or cer > 0.18:
        status = "FAIL"
    elif weak_count or edge_warnings or cer > 0.08:
        status = "REVIEW"
    else:
        status = "PASS"

    summary = "\n".join(
        [
            f"Second review: {status}",
            f"CER: {cer:.2%} ({distance}/{len(expected)} chars)",
            f"Expected chars: {len(expected)}",
            f"SRT chars: {len(actual)}",
            f"Chunks: {len(chunks)}",
            f"Missing chunks: {missing_count}",
            f"Weak chunks: {weak_count}",
        ]
    )

    lines = [
        "# TTS Secondary Review",
        "",
        "## Summary",
        "",
        f"- Status: **{status}**",
        f"- CER: **{cer:.2%}** ({distance}/{len(expected)} chars)",
        f"- Expected chars: {len(expected)}",
        f"- SRT chars: {len(actual)}",
        f"- Expected chunks: {len(chunks)}",
        f"- SRT blocks: {len(blocks)}",
        f"- Missing chunks: {missing_count}",
        f"- Weak chunks: {weak_count}",
        "",
    ]
    if edge_warnings:
        lines.extend(["## Edge Warnings", "", *[f"- {item}" for item in edge_warnings], ""])
    if issues:
        lines.extend(["## Chunk Issues", ""])
        for issue in issues:
            lines.extend(
                [
                    f"### Chunk {issue['index']:03d} - {issue['status']}",
                    "",
                    f"- Score: {issue['score']:.2f}",
                    f"- Coverage: {issue['coverage']:.2f}",
                    f"- Expected: `{issue['expected']}`",
                    f"- Closest ASR: `{issue['matched']}`",
                    "",
                ]
            )
    else:
        lines.extend(["## Chunk Issues", "", "No missing or weak chunks detected.", ""])
    lines.extend(
        [
            "## Suggested Action",
            "",
            "- `FAIL`: 建议回到分段 TTS 自动修复，降低分段字数，增加重试次数，或单独重录问题片段。",
            "- `REVIEW`: 建议人工听查弱匹配片段，必要时只重试对应片段。",
            "- `PASS`: 可以进入视频渲染或最终人工抽查。",
            "",
            "## SRT Blocks",
            "",
        ]
    )
    for block in blocks:
        lines.append(f"- {block['index']} {block['time']} `{block['text']}`")
    return summary, "\n".join(lines).strip() + "\n"


def call_agent_review(
    config: AppConfig,
    source_text: str,
    srt_text: str,
    local_report: str,
    api_url: str,
    model: str,
    api_key_value: str = "",
) -> str:
    api_url = (api_url or "").strip()
    model = (model or "").strip()
    if not api_url:
        return "Agent API 未配置，已跳过。"
    api_url = normalize_chat_api_url(api_url)
    agent_cfg = config.raw.get("agent_review", {}) if isinstance(config.raw.get("agent_review", {}), dict) else {}
    api_key_env = str(agent_cfg.get("api_key_env") or "AGENT_REVIEW_API_KEY")
    api_key = (api_key_value or "").strip() or os.environ.get(api_key_env) or str(agent_cfg.get("api_key") or "")
    timeout = int(agent_cfg.get("timeout") or 120)
    payload = {
        "model": model or agent_cfg.get("model") or "local-reviewer",
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是本地 TTS 成片质检 Agent。请基于原文、ASR 字幕和本地差异报告，"
                    "判断音频是否有漏句、重复、错读、掐头去尾、顺序错乱。输出中文，"
                    "必须给出 PASS/REVIEW/FAIL、问题片段、建议动作。不要改写原文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "## 原文\n"
                    + (source_text or "")
                    + "\n\n## ASR/SRT 文本\n"
                    + strip_srt_text(srt_text)
                    + "\n\n## 本地审核报告\n"
                    + local_report[:12000]
                ),
            },
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "Agent API 调用失败: 401 Unauthorized。请检查 Chat API Key 是否已填写，且与服务端要求的 key/LiteLLM master key 一致。"
        return f"Agent API 调用失败: HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return f"Agent API 调用失败: {exc}"
    try:
        parsed = json.loads(body)
        return str(parsed["choices"][0]["message"]["content"]).strip()
    except Exception:
        return body.strip()


def normalize_chat_api_url(api_url: str) -> str:
    api_url = api_url.strip().rstrip("/")
    if not api_url:
        return ""
    parsed = urllib.parse.urlparse(api_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return api_url
    if path.endswith("/v1"):
        return api_url + "/chat/completions"
    return api_url + "/v1/chat/completions"


def transcribe_to_srt(audio_path: Path, model_path: Path, language: str) -> str:
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    return transcribe_model_to_srt(audio_path, model, language)


def wav_duration_seconds(audio_path: Path) -> float | None:
    if audio_path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if rate > 0:
                return frames / float(rate)
    except Exception:
        return None
    return None


def audio_duration_seconds(config: AppConfig, audio_path: Path) -> float | None:
    duration = wav_duration_seconds(audio_path)
    if duration is not None:
        return duration
    command = [
        str(config.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = run_command(command, config.project_root, APP_ROOT / "ffprobe_duration.txt")
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def transcribe_model_to_segments(audio_path: Path, model: Any, language: str) -> list[dict[str, Any]]:
    duration = wav_duration_seconds(audio_path)
    segments, _info = model.transcribe(str(audio_path), language=language, vad_filter=False)
    parsed: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        text = str(segment.text).strip()
        start = float(segment.start)
        end = float(segment.end)
        if duration is not None:
            if start >= duration + 0.25:
                continue
            end = min(end, duration)
        if text and end > start:
            parsed.append({"index": index, "start": start, "end": end, "text": text})
    return parsed


def transcribe_model_to_segments_and_char_timeline(
    audio_path: Path,
    model: Any,
    language: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    duration = wav_duration_seconds(audio_path)
    try:
        segment_iter, _info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=False,
            word_timestamps=True,
        )
    except TypeError:
        segment_iter, _info = model.transcribe(str(audio_path), language=language, vad_filter=False)

    parsed: list[dict[str, Any]] = []
    char_timeline: list[dict[str, Any]] = []
    for index, segment in enumerate(segment_iter, start=1):
        text = str(segment.text).strip()
        start = float(segment.start)
        end = float(segment.end)
        if duration is not None:
            if start >= duration + 0.25:
                continue
            end = min(end, duration)
        if not text or end <= start:
            continue
        parsed.append({"index": index, "start": start, "end": end, "text": text})

        words = list(getattr(segment, "words", None) or [])
        if words:
            for word in words:
                word_text = str(getattr(word, "word", "") or "").strip()
                normalized = normalize_text(word_text)
                if not normalized:
                    continue
                word_start = max(start, float(getattr(word, "start", start) or start))
                word_end = min(end, float(getattr(word, "end", end) or end))
                if word_end <= word_start:
                    word_start, word_end = start, end
                step = (word_end - word_start) / max(1, len(normalized))
                for offset, char in enumerate(normalized):
                    char_timeline.append(
                        {
                            "char": char,
                            "start": word_start + (step * offset),
                            "end": word_start + (step * (offset + 1)),
                        }
                    )
        else:
            normalized = normalize_text(text)
            step = (end - start) / max(1, len(normalized))
            for offset, char in enumerate(normalized):
                char_timeline.append(
                    {
                        "char": char,
                        "start": start + (step * offset),
                        "end": start + (step * (offset + 1)),
                    }
                )
    return parsed, char_timeline


def patch_unit_intervals_from_timeline(
    config: AppConfig,
    audio_path: Path,
    segments: list[dict[str, Any]],
    units: list[dict[str, Any]],
    char_timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unit_norms = [normalize_text(str(unit.get("text") or "")) for unit in units]
    expected = "".join(unit_norms)
    actual = "".join(str(item.get("char") or "") for item in char_timeline)
    expected_ranges: list[tuple[int, int]] = []
    cursor = 0
    for normalized in unit_norms:
        start = cursor
        cursor += len(normalized)
        expected_ranges.append((start, cursor))

    expected_to_actual: dict[int, int] = {}
    if expected and actual and char_timeline:
        matcher = SequenceMatcher(None, expected, actual, autojunk=False)
        for tag, expected_start, expected_end, actual_start, _actual_end in matcher.get_opcodes():
            if tag != "equal":
                continue
            for offset, expected_index in enumerate(range(expected_start, expected_end)):
                expected_to_actual[expected_index] = actual_start + offset

    fallback_srt = script_aligned_srt_from_blocks(config, audio_path, segments, [str(unit.get("text") or "") for unit in units])
    fallback_blocks = parse_srt_blocks(fallback_srt)
    duration = audio_duration_seconds(config, audio_path) or float(segments[-1]["end"] if segments else len(units))

    intervals: list[dict[str, Any]] = []
    for unit_index, ((expected_start, expected_end), unit) in enumerate(zip(expected_ranges, units), start=1):
        mapped = [
            expected_to_actual[index]
            for index in range(expected_start, expected_end)
            if index in expected_to_actual and 0 <= expected_to_actual[index] < len(char_timeline)
        ]
        confidence = 0.0 if expected_end <= expected_start else len(mapped) / max(1, expected_end - expected_start)
        if mapped and confidence >= 0.35:
            start = float(char_timeline[min(mapped)]["start"])
            end = float(char_timeline[max(mapped)]["end"])
            source = "word_timestamps"
        else:
            block = fallback_blocks[unit_index - 1] if unit_index - 1 < len(fallback_blocks) else {}
            match = re.match(r"\s*(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", str(block.get("time") or ""))
            if match:
                start = parse_srt_timestamp(match.group(1))
                end = parse_srt_timestamp(match.group(2))
            else:
                ratio_start = (unit_index - 1) / max(1, len(units))
                ratio_end = unit_index / max(1, len(units))
                start = duration * ratio_start
                end = duration * ratio_end
            source = "estimated"

        start = max(0.0, min(duration, start))
        end = max(start + 0.04, min(duration, end))
        intervals.append(
            {
                "index": unit_index,
                "start": start,
                "end": end,
                "confidence": confidence,
                "source": source,
            }
        )

    for index in range(1, len(intervals)):
        previous = intervals[index - 1]
        current = intervals[index]
        if float(current["start"]) < float(previous["end"]):
            boundary = (float(current["start"]) + float(previous["end"])) / 2.0
            previous["end"] = max(float(previous["start"]) + 0.04, boundary)
            current["start"] = min(float(current["end"]) - 0.04, boundary)

    return intervals


def segments_to_srt(segments: list[dict[str, Any]]) -> str:
    blocks = []
    for segment in segments:
        blocks.append(make_srt_block(len(blocks) + 1, float(segment["start"]), float(segment["end"]), str(segment["text"])))
    return "\n\n".join(blocks).strip() + "\n"


def text_display_width(text: str) -> int:
    width = 0
    for char in text:
        if char == "\n":
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def strip_subtitle_punctuation(text: str) -> str:
    kept: list[str] = []
    for char in text:
        if char == "\n" or char.isspace():
            kept.append(char)
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        kept.append(char)
    cleaned = "".join(kept)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def split_subtitle_source_units(text: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
    if not cleaned:
        return []
    return [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;，,])\s+|(?<=[。！？!?；;，,])|[\r\n]+", cleaned)
        if normalize_text(part)
    ]


def split_long_subtitle_unit(unit: str, max_width: int) -> list[str]:
    unit = unit.strip()
    if text_display_width(unit) <= max_width:
        return [unit]
    words = unit.split()
    if len(words) > 1:
        chunks: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            if text_display_width(candidate) <= max_width:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = word
        if current:
            chunks.append(current)
        return chunks

    chunks = []
    current = ""
    for char in unit:
        candidate = current + char
        if current and text_display_width(candidate) > max_width:
            chunks.append(current)
            current = char
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def subtitle_blocks_from_script(config: AppConfig, source_text: str) -> list[str]:
    line_width = config_int(config, "script_subtitle_line_chars", 34)
    max_lines = max(1, min(2, config_int(config, "script_subtitle_max_lines", 2)))
    max_width = line_width * max_lines
    merge_limit = min(max_width, config_int(config, "script_subtitle_merge_line_chars", 30))
    units = split_subtitle_source_units(source_text)
    blocks: list[str] = []
    current = ""
    for unit in units:
        if current and re.search(r"[。！？!?；;]\s*$", current):
            blocks.append(current)
            current = ""
        if current and text_display_width(strip_subtitle_punctuation(current)) >= merge_limit:
            blocks.append(current)
            current = ""
        sep = tts_join_separator(current, unit)
        candidate = (current + sep + unit).strip() if current else unit
        if current and text_display_width(candidate) > max_width:
            blocks.append(current)
            current = ""
        if text_display_width(unit) > max_width:
            if current:
                blocks.append(current)
                current = ""
            blocks.extend(split_long_subtitle_unit(unit, max_width))
        elif current:
            current = candidate
        else:
            current = unit
    if current:
        blocks.append(current)
    return [format_subtitle_block(config, block) for block in blocks if normalize_text(block)]


def cjk_boundary_candidates(text: str) -> list[tuple[float, int]]:
    candidates: list[tuple[float, int]] = []
    boundary_before_phrases = [
        "但如果",
        "而非",
        "却又",
        "更不想",
        "不想",
        "妄图",
        "请不要",
        "我可以",
        "而对",
        "但是",
        "不过",
        "因为",
        "所以",
        "而且",
    ]
    for phrase in boundary_before_phrases:
        start = 0
        while True:
            pos = text.find(phrase, start)
            if pos < 0:
                break
            candidates.append((-8.0, pos))
            start = pos + 1

    strong_phrases = [
        "认为",
        "觉得",
        "知道",
        "相信",
        "希望",
        "明白",
        "心思",
        "如果",
        "因为",
        "所以",
        "但是",
        "不过",
        "而且",
        "只是",
    ]
    for phrase in strong_phrases:
        start = 0
        while True:
            pos = text.find(phrase, start)
            if pos < 0:
                break
            candidates.append((-4.0, pos + len(phrase)))
            start = pos + 1

    for pos, char in enumerate(text[:-1], start=1):
        if char in "但而却并和与或":
            candidates.append((-2.0, pos - 1))
        if char in "时后前中里内上下了着过就才会要想让把被给对向":
            candidates.append((-1.0, pos))
    return candidates


def adjust_subtitle_cjk_cut(text: str, cut: int) -> int:
    protected_previous = "不没无非很太更最第"
    protected_next = "的地得们"
    protected_pairs = {
        "浪漫",
        "信任",
        "不足",
        "外人",
        "乞丐",
        "心思",
        "负担",
        "累赘",
        "温柔",
        "请求",
        "哭泣",
    }
    if 0 < cut < len(text) and text[cut - 1 : cut + 1] in protected_pairs:
        return min(len(text), cut + 1)
    if 0 < cut < len(text) and text[cut - 1] in protected_previous:
        return min(len(text), cut + 1)
    if 0 < cut < len(text) and text[cut] in protected_next:
        return min(len(text), cut + 1)
    return cut


def wrap_cjk_subtitle_text(config: AppConfig, text: str, line_width: int, max_lines: int) -> list[str] | None:
    if max_lines < 2 or not re.search(r"[\u4e00-\u9fff]", text):
        return None
    single_line_width = min(line_width, config_int(config, "script_subtitle_single_line_chars", 24))
    total_width = text_display_width(text)
    if total_width <= single_line_width:
        return [text]

    min_side = max(4, config_int(config, "script_subtitle_min_line_chars", 4) * 2)
    best: tuple[float, int] | None = None
    target = total_width / 2
    for bonus, cut in cjk_boundary_candidates(text):
        cut = adjust_subtitle_cjk_cut(text, cut)
        left = text[:cut].strip()
        right = text[cut:].strip()
        left_width = text_display_width(left)
        right_width = text_display_width(right)
        if not left or not right:
            continue
        if left_width < min_side or right_width < min_side:
            continue
        if left_width > line_width or right_width > line_width:
            continue
        score = abs(left_width - target) + abs(right_width - target) + bonus
        candidate = (score, cut)
        if best is None or candidate < best:
            best = candidate

    if best is None:
        target_chars = max(1, round(len(text) / 2))
        cut = adjust_subtitle_cjk_cut(text, norm_cut_index(text, target_chars))
    else:
        cut = best[1]
    left = text[:cut].strip()
    right = text[cut:].strip()
    if left and right and text_display_width(left) <= line_width and text_display_width(right) <= line_width:
        return [left, right]
    return None


def wrap_subtitle_text(config: AppConfig, text: str) -> list[str]:
    line_width = config_int(config, "script_subtitle_line_chars", 34)
    max_lines = max(1, min(2, config_int(config, "script_subtitle_max_lines", 2)))
    cleaned = strip_subtitle_punctuation((text or "").replace("\r\n", "\n"))
    if not cleaned:
        return []
    manual_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if 1 < len(manual_lines) <= max_lines and all(text_display_width(line) <= line_width for line in manual_lines):
        return manual_lines

    cjk_lines = wrap_cjk_subtitle_text(config, cleaned.replace("\n", ""), line_width, max_lines)
    if cjk_lines:
        return cjk_lines
    if len(manual_lines) == 1 and text_display_width(manual_lines[0]) <= line_width:
        return manual_lines

    words = cleaned.replace("\n", " ").split()
    if len(words) > 1:
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            if text_display_width(candidate) <= line_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    else:
        lines = []
        current = ""
        for char in cleaned.replace("\n", ""):
            candidate = current + char
            if current and text_display_width(candidate) > line_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)

    if len(lines) <= max_lines:
        return lines
    first = lines[: max_lines - 1]
    tail = " ".join(lines[max_lines - 1 :]) if words else "".join(lines[max_lines - 1 :])
    return [*first, tail.strip()]


def format_subtitle_block(config: AppConfig, text: str) -> str:
    return "\n".join(wrap_subtitle_text(config, text))


def format_srt_for_editor(config: AppConfig, srt_text: str) -> str:
    blocks = parse_srt_blocks(srt_text)
    if not blocks:
        return srt_text or ""
    output: list[str] = []
    for fallback_index, block in enumerate(blocks, start=1):
        text = format_subtitle_block(config, str(block.get("text") or ""))
        if not normalize_text(text):
            continue
        index = str(block.get("index") or fallback_index)
        time_line = str(block.get("time") or "").strip()
        if time_line:
            output.append("\n".join([index, time_line, text]))
        else:
            output.append(text)
    return "\n\n".join(output).strip() + ("\n" if output else "")


def editor_subtitle_blocks(config: AppConfig, editor_text: str) -> list[str]:
    text = (editor_text or "").strip()
    if not text:
        return []
    if "-->" in text:
        raw_blocks = [block["text"] for block in parse_srt_blocks(text)]
    else:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if normalize_text(part)]
        if len(paragraphs) > 1:
            raw_blocks = paragraphs
        else:
            raw_blocks = [line.strip() for line in text.splitlines() if normalize_text(line)]
    return [format_subtitle_block(config, block) for block in raw_blocks if normalize_text(block)]


def subtitle_blocks_to_plain_text(blocks: list[str]) -> str:
    return "".join(block.replace("\n", "") for block in blocks)


def subtitle_text_matches_source(source_text: str, blocks: list[str]) -> bool:
    if not (source_text or "").strip() or not blocks:
        return False
    return normalize_text(source_text) == normalize_text(subtitle_blocks_to_plain_text(blocks))


def final_subtitle_blocks_from_source_or_editor(
    config: AppConfig,
    source_text: str,
    editor_text: str,
) -> tuple[list[str], str]:
    source = (source_text or "").strip()
    editor_blocks = editor_subtitle_blocks(config, editor_text)
    if not source:
        return editor_blocks, "editor"

    if subtitle_text_matches_source(source, editor_blocks):
        return editor_blocks, "editor_line_breaks"

    source_blocks = subtitle_blocks_from_script(config, source)
    return source_blocks, "source_text"


def srt_from_final_subtitle_text(
    config: AppConfig,
    audio_path: Path,
    source_text: str,
    editor_text: str,
    segments: list[dict[str, Any]],
) -> tuple[str, str]:
    blocks, source_mode = final_subtitle_blocks_from_source_or_editor(config, source_text, editor_text)
    if not blocks:
        return "", source_mode
    return script_aligned_srt_from_blocks(config, audio_path, segments, blocks), source_mode


def editor_subtitle_pages_preserve_lines(editor_text: str) -> list[str]:
    text = (editor_text or "").strip()
    if not text:
        return []
    if "-->" in text:
        return [str(block.get("text") or "").strip() for block in parse_srt_blocks(text) if normalize_text(str(block.get("text") or ""))]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if normalize_text(part)]
    if len(paragraphs) > 1:
        return paragraphs
    return [line.strip() for line in text.splitlines() if normalize_text(line)]


def time_at_segment_units(segments: list[dict[str, Any]], target_units: float, total_units: float, duration: float) -> float:
    if not segments or total_units <= 0:
        ratio = 0.0 if total_units <= 0 else target_units / total_units
        return max(0.0, min(duration, duration * ratio))
    running = 0.0
    for segment in segments:
        units = max(1, len(normalize_text(str(segment.get("text") or ""))))
        next_running = running + units
        if target_units <= next_running:
            ratio = 0.0 if units <= 0 else (target_units - running) / units
            start = float(segment["start"])
            end = float(segment["end"])
            return max(0.0, min(duration, start + ((end - start) * ratio)))
        running = next_running
    return max(0.0, min(duration, float(segments[-1]["end"])))


def script_aligned_srt_from_blocks(config: AppConfig, audio_path: Path, segments: list[dict[str, Any]], subtitle_blocks: list[str]) -> str:
    blocks_text = [block for block in subtitle_blocks if normalize_text(block)]
    if not blocks_text:
        return segments_to_srt(segments)

    duration = wav_duration_seconds(audio_path)
    if duration is None:
        duration = float(segments[-1]["end"]) if segments else float(len(blocks_text))
    duration = max(0.1, duration)

    expected_units = [max(1, len(normalize_text(block))) for block in blocks_text]
    total_expected = max(1, sum(expected_units))
    total_actual = max(1, sum(max(1, len(normalize_text(str(segment.get("text") or "")))) for segment in segments))

    blocks = []
    cumulative_expected = 0
    previous_end = 0.0
    for index, (block_text, units) in enumerate(zip(blocks_text, expected_units), start=1):
        start_ratio = cumulative_expected / total_expected
        cumulative_expected += units
        end_ratio = cumulative_expected / total_expected
        start = time_at_segment_units(segments, start_ratio * total_actual, total_actual, duration)
        end = time_at_segment_units(segments, end_ratio * total_actual, total_actual, duration)
        start = max(previous_end, start)
        if index == len(blocks_text):
            end = duration
        if end <= start:
            end = min(duration, start + max(0.1, duration / max(1, len(blocks_text))))
        previous_end = end
        blocks.append(make_srt_block(index, start, end, block_text))
    return "\n\n".join(blocks).strip() + "\n"


def script_aligned_srt_from_segments(config: AppConfig, audio_path: Path, segments: list[dict[str, Any]], source_text: str) -> str:
    return script_aligned_srt_from_blocks(config, audio_path, segments, subtitle_blocks_from_script(config, source_text))


def subtitle_srt_from_model(config: AppConfig, audio_path: Path, model: Any, language: str, source_text: str) -> tuple[str, str, bool]:
    segments = transcribe_model_to_segments(audio_path, model, language)
    raw_srt = segments_to_srt(segments)
    if (source_text or "").strip():
        return script_aligned_srt_from_segments(config, audio_path, segments, source_text), raw_srt, True
    return raw_srt, raw_srt, False


def subtitle_srt_after_audio_edit(
    config: AppConfig,
    audio_path: Path,
    model: Any,
    language: str,
    source_text: str,
    editor_text: str,
) -> tuple[str, str, bool, str]:
    segments = transcribe_model_to_segments(audio_path, model, language)
    raw_srt = segments_to_srt(segments)
    if (source_text or "").strip():
        display_srt, mode = srt_from_final_subtitle_text(config, audio_path, source_text or "", editor_text or "", segments)
        if display_srt.strip():
            return display_srt, raw_srt, True, mode

    editor_blocks = editor_subtitle_blocks(config, editor_text or "")
    if editor_blocks:
        return script_aligned_srt_from_blocks(config, audio_path, segments, editor_blocks), raw_srt, False, "editor_line_breaks"
    return raw_srt, raw_srt, False, "raw_asr"


def latest_raw_asr_srt(job: Path) -> Path | None:
    asr_dir = job / "asr"
    if not asr_dir.exists():
        return None
    candidates = sorted(asr_dir.glob("*.raw_asr.srt"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def triad_report(source_text: str, display_srt: str, raw_srt: str, aligned_to_script: bool) -> str:
    lines = [
        "Subtitle mode: " + ("文案对齐字幕（字幕文本以文案为准，ASR 只提供时间轴）" if aligned_to_script else "ASR 原始字幕"),
    ]
    if source_text:
        lines.extend(
            [
                "",
                "Audio/raw ASR vs script:",
                local_srt_report(source_text, raw_srt),
                "",
                "Final subtitle vs script:",
                local_srt_report(source_text, display_srt),
            ]
        )
    return "\n".join(lines)


def transcribe_model_to_srt(audio_path: Path, model: Any, language: str) -> str:
    return segments_to_srt(transcribe_model_to_segments(audio_path, model, language))


def transcribe_model_to_text(audio_path: Path, model: Any, language: str) -> str:
    segments, _info = model.transcribe(str(audio_path), language=language, vad_filter=False)
    return "".join(str(segment.text).strip() for segment in segments).strip()


def tts_join_separator(left: str, right: str) -> str:
    if not left or not right:
        return ""
    if re.search(r"[A-Za-z0-9][\"')\]]?$", left) and re.match(r"[A-Za-z0-9]", right):
        return " "
    if re.search(r"[.!?,;:][\"')\]]?$", left) and re.match(r"[A-Za-z0-9]", right):
        return " "
    return ""


def split_long_tts_unit(unit: str, max_chars: int) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[，,、：:])\s*", unit) if part.strip()]
    if not parts:
        parts = [unit.strip()]

    chunks: list[str] = []
    current = ""
    for part in parts:
        sep = tts_join_separator(current, part)
        candidate = (current + sep + part).strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current.strip())
            current = ""
        if len(part) <= max_chars:
            current = part
            continue

        words = part.split()
        if len(words) > 1:
            word_chunk = ""
            for word in words:
                candidate = f"{word_chunk} {word}".strip() if word_chunk else word
                if len(candidate) <= max_chars:
                    word_chunk = candidate
                else:
                    if word_chunk:
                        chunks.append(word_chunk.strip())
                    word_chunk = word
            if word_chunk:
                current = word_chunk
        else:
            for start in range(0, len(part), max_chars):
                chunks.append(part[start : start + max_chars].strip())
            current = ""
    if current:
        chunks.append(current.strip())
    return chunks


def norm_char_len(text: str) -> int:
    return len(normalize_text(text))


def norm_cut_index(text: str, target_chars: int) -> int:
    seen = 0
    for pos, char in enumerate(text):
        if normalize_text(char):
            seen += 1
        if seen >= target_chars:
            return pos + 1
    return len(text)


def adjust_cjk_micro_cut(text: str, cut: int, min_chars: int, max_chars: int) -> int:
    if 0 < cut < len(text) and text[cut - 1] in "不没无非很太更最":
        shifted = cut + 1
        if norm_char_len(text[:shifted]) <= max_chars and norm_char_len(text[shifted:]) >= min_chars:
            return shifted
    return cut


def cjk_micro_cut_index(text: str, min_chars: int, max_chars: int) -> int:
    norm_len = norm_char_len(text)
    if norm_len <= max_chars:
        return len(text)

    parts_needed = max(2, (norm_len + max_chars - 1) // max_chars)
    target = max(min_chars, min(max_chars, round(norm_len / parts_needed)))
    hard_boundaries = "，,、：:；;。！？!?"
    soft_boundaries = "时后前中里内外上下了着过而并但却就才会要想让把被给对向和与或"
    best: tuple[float, int] | None = None

    for pos, char in enumerate(text):
        left_norm = norm_char_len(text[: pos + 1])
        right_norm = norm_char_len(text[pos + 1 :])
        if left_norm < min_chars or left_norm > max_chars:
            continue
        if right_norm and right_norm < min_chars:
            continue
        score = abs(left_norm - target)
        if char in hard_boundaries:
            score -= 4
        elif char in soft_boundaries:
            score -= 1
        candidate = (score, pos + 1)
        if best is None or candidate < best:
            best = candidate

    if best is not None:
        return adjust_cjk_micro_cut(text, best[1], min_chars, max_chars)
    return adjust_cjk_micro_cut(text, norm_cut_index(text, target), min_chars, max_chars)


def split_cjk_balanced_text(text: str, min_chars: int = 10, max_chars: int = 20) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while norm_char_len(remaining) > max_chars:
        cut = cjk_micro_cut_index(remaining, min_chars, max_chars)
        head = remaining[:cut].strip()
        tail = remaining[cut:].strip()
        if not head or not tail:
            break
        chunks.append(head)
        remaining = tail
    if normalize_text(remaining):
        chunks.append(remaining)
    return chunks


def rebalance_cjk_micro_chunks(chunks: list[str], min_chars: int = 10, max_chars: int = 20) -> list[str]:
    items = [item for item in chunks if normalize_text(item)]
    balanced: list[str] = []
    index = 0
    while index < len(items):
        chunk = items[index]
        if norm_char_len(chunk) < min_chars and index + 1 < len(items):
            next_chunk = items[index + 1]
            combined_next = (chunk + tts_join_separator(chunk, next_chunk) + next_chunk).strip()
            if norm_char_len(combined_next) <= max_chars:
                balanced.append(combined_next)
                index += 2
                continue
            if norm_char_len(combined_next) <= max_chars * 2:
                balanced.extend(split_cjk_balanced_text(combined_next, min_chars, max_chars))
                index += 2
                continue
        if balanced and norm_char_len(chunk) < min_chars:
            previous = balanced.pop()
            combined = (previous + tts_join_separator(previous, chunk) + chunk).strip()
            if norm_char_len(combined) <= max_chars:
                balanced.append(combined)
            elif norm_char_len(combined) <= max_chars * 2:
                balanced.extend(split_cjk_balanced_text(combined, min_chars, max_chars))
            else:
                balanced.append(previous)
                balanced.append(chunk)
        else:
            balanced.append(chunk)
        index += 1
    return [chunk for chunk in balanced if normalize_text(chunk)]


def split_cjk_micro_unit(unit: str, min_chars: int = 10, max_chars: int = 20) -> list[str]:
    pieces = [part.strip() for part in re.split(r"(?<=[，,、：:；;])\s*", unit) if part.strip()]
    if not pieces:
        pieces = [unit.strip()]

    expanded: list[str] = []
    for piece in pieces:
        while norm_char_len(piece) > max_chars:
            cut = cjk_micro_cut_index(piece, min_chars, max_chars)
            head = piece[:cut].strip()
            tail = piece[cut:].strip()
            if not head or not tail:
                break
            expanded.append(head)
            piece = tail
        if normalize_text(piece):
            expanded.append(piece.strip())

    merged: list[str] = []
    current = ""
    for piece in expanded:
        if not current:
            current = piece
            continue
        candidate = (current + tts_join_separator(current, piece) + piece).strip()
        if (norm_char_len(current) < min_chars or norm_char_len(piece) < min_chars) and norm_char_len(candidate) <= max_chars:
            current = candidate
        else:
            merged.append(current)
            current = piece
    if current:
        merged.append(current)
    return rebalance_cjk_micro_chunks(merged, min_chars, max_chars)


def split_tts_chunks(text: str, max_chars: int) -> list[str]:
    max_chars = max(20, min(300, int(max_chars or 80)))
    cleaned = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
    if not cleaned:
        return []

    units = [part.strip() for part in re.split(r"(?<=[。！？!?；;\.])\s+|(?<=[。！？!?；;\.])|[\r\n]+", cleaned) if part.strip()]
    expanded: list[str] = []
    for unit in units:
        if len(unit) <= max_chars:
            expanded.append(unit)
        else:
            expanded.extend(split_long_tts_unit(unit, max_chars))

    chunks: list[str] = []
    current = ""
    for unit in expanded:
        if not normalize_text(unit):
            continue
        sep = tts_join_separator(current, unit)
        candidate = (current + sep + unit).strip() if current else unit
        if not current:
            current = unit
        elif len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current.strip())
            current = unit
    if current.strip():
        chunks.append(current.strip())
    return chunks


def segment_match_report(expected: str, actual: str, pass_cer: float) -> tuple[bool, float, str]:
    expected_norm = normalize_text(expected)
    actual_norm = normalize_text(actual)
    if not expected_norm:
        return True, 0.0, "empty expected text"
    distance = levenshtein(expected_norm, actual_norm)
    cer = distance / max(1, len(expected_norm))
    length_ratio = len(actual_norm) / max(1, len(expected_norm))
    too_short = len(expected_norm) >= 12 and length_ratio < 0.72
    ok = cer <= pass_cer and not too_short
    detail = (
        f"CER={cer:.2%}, expected={len(expected_norm)} chars, "
        f"asr={len(actual_norm)} chars, length_ratio={length_ratio:.2f}"
    )
    if too_short:
        detail += ", possible clipping/omission"
    return ok, cer, detail


def tts_text_for_attempt(chunk: str, index: int, attempt: int) -> str:
    if attempt == 2:
        return ", " + chunk.lstrip()
    if attempt >= 3:
        return ". " + chunk.lstrip()
    return chunk


def tts_attempt_count(index: int, retries: int) -> int:
    attempts = max(1, int(retries) + 1)
    if index == 1:
        return max(attempts, 3)
    return attempts


def split_micro_tts_chunks(text: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
    if not cleaned:
        return []
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", cleaned))
    target = 20 if has_cjk else 38
    units: list[str] = []
    buf: list[str] = []
    for char in cleaned:
        buf.append(char)
        if char in "。！？!?；;，,.":
            unit = "".join(buf).strip()
            if normalize_text(unit):
                units.append(unit)
            buf = []
    tail = "".join(buf).strip()
    if normalize_text(tail):
        units.append(tail)
    if len(units) <= 1 and not has_cjk:
        units = split_long_tts_unit(cleaned, target)
    if has_cjk:
        expanded: list[str] = []
        for unit in units:
            expanded.extend(split_cjk_micro_unit(unit, 10, target))
        return rebalance_cjk_micro_chunks(expanded, 10, target)

    chunks: list[str] = []
    current = ""
    for unit in units:
        sep = tts_join_separator(current, unit)
        candidate = (current + sep + unit).strip() if current else unit
        if current and len(normalize_text(candidate)) > target:
            chunks.append(current)
            current = ""
        if len(normalize_text(unit)) > target:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(split_long_tts_unit(unit, target))
        elif current:
            current = candidate
        else:
            current = unit
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if normalize_text(chunk)]


def strip_patch_boundary_punctuation(text: str) -> str:
    return re.sub(r"^[\s，,、；;：:。！？!?.]+|[\s，,、；;：:。！？!?.]+$", "", text or "").strip()


def split_patch_sentence_units(text: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
    if not cleaned:
        return []
    delimiters = "。！？!?；;."
    units: list[str] = []
    buf: list[str] = []
    for char in cleaned:
        if char == "\n":
            unit = strip_patch_boundary_punctuation("".join(buf))
            if normalize_text(unit):
                units.append(unit)
            buf = []
            continue
        buf.append(char)
        if char in delimiters:
            unit = strip_patch_boundary_punctuation("".join(buf))
            if normalize_text(unit):
                units.append(unit)
            buf = []
    tail = strip_patch_boundary_punctuation("".join(buf))
    if normalize_text(tail):
        units.append(tail)
    return units or ([strip_patch_boundary_punctuation(cleaned)] if normalize_text(cleaned) else [])


def split_patch_clause_units(text: str) -> list[str]:
    cleaned = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
    cleaned = strip_patch_boundary_punctuation(cleaned)
    if not cleaned:
        return []
    delimiters = "，,、：:"
    units: list[str] = []
    buf: list[str] = []
    for char in cleaned:
        if char in delimiters:
            unit = strip_patch_boundary_punctuation("".join(buf))
            if normalize_text(unit):
                units.append(unit)
            buf = []
            continue
        buf.append(char)
    tail = strip_patch_boundary_punctuation("".join(buf))
    if normalize_text(tail):
        units.append(tail)
    return units or [cleaned]


def split_patch_minimal_units(text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for sentence_index, sentence in enumerate(split_patch_sentence_units(text or ""), start=1):
        clauses = split_patch_clause_units(sentence)
        if not clauses:
            continue
        for clause_index, clause in enumerate(clauses, start=1):
            units.append(
                {
                    "sequence_index": len(units) + 1,
                    "sentence_index": sentence_index,
                    "clause_index": clause_index,
                    "locator": f"{sentence_index}.{clause_index}",
                    "text": clause,
                    "sentence": sentence,
                }
            )
    return units


def minimal_patch_chunks(text: str) -> list[str]:
    return [str(unit["text"]) for unit in split_patch_minimal_units(text or "")]


def manual_patch_listing(text: str, chunk_chars: int) -> str:
    chunks = split_patch_sentence_units(text or "")
    if not chunks:
        return "没有可列出的文案分段。"
    lines = [
        "格式：3 表示第 3 个句号大分段；3.2 表示第 3 个大分段里的第 2 个逗号小分段。",
        "连续小分段可一起生成，例如 3.1,3.2,3.3。",
        "",
    ]
    for index, chunk in enumerate(chunks, start=1):
        lines.append(f"{index}: {chunk}")
        clauses = split_patch_clause_units(chunk)
        for clause_index, clause in enumerate(clauses, start=1):
            lines.append(f"  {index}.{clause_index}: {clause}")
    return "\n".join(lines)


def parse_manual_patch_targets(
    target_text: str,
    chunks: list[str],
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    raw = (target_text or "").strip()
    if not raw:
        return [], {}

    tokens = [token for token in re.split(r"[\s,，;；、]+", raw) if token.strip()]
    patch_rows_by_index: dict[int, dict[str, Any]] = {}
    clause_rows_by_index: dict[int, list[dict[str, Any]]] = {}
    invalid: list[str] = []

    for token in tokens:
        normalized = token.strip().upper().replace("Ｓ", "S").replace("Ｃ", "C")
        match = re.match(r"^S?0*(\d+)(?:[.。:_-]C?0*(\d+))?$", normalized)
        if not match:
            invalid.append(token)
            continue
        chunk_index = int(match.group(1))
        clause_index = int(match.group(2)) if match.group(2) else None
        if chunk_index < 1 or chunk_index > len(chunks):
            invalid.append(token)
            continue

        chunk = chunks[chunk_index - 1]
        patch_rows_by_index.setdefault(
            chunk_index,
            {
                "index": chunk_index,
                "status": "MANUAL",
                "score": 0.0,
                "coverage": 0.0,
                "expected": chunk,
                "matched": "",
            },
        )

        if clause_index is not None:
            clauses = split_patch_clause_units(chunk)
            if clause_index < 1 or clause_index > len(clauses):
                invalid.append(token)
                continue
            rows = clause_rows_by_index.setdefault(chunk_index, [])
            if not any(int(row["index"]) == clause_index for row in rows):
                rows.append(
                    {
                        "index": clause_index,
                        "status": "MANUAL",
                        "score": 0.0,
                        "coverage": 0.0,
                        "expected": clauses[clause_index - 1],
                        "matched": "",
                    }
                )

    if invalid:
        raise ValueError("手动补修编号无效: " + ", ".join(invalid))
    if not patch_rows_by_index:
        raise ValueError("没有解析到有效的手动补修编号。")

    for rows in clause_rows_by_index.values():
        rows.sort(key=lambda row: int(row["index"]))
    return [patch_rows_by_index[index] for index in sorted(patch_rows_by_index)], clause_rows_by_index


def chunk_review_rows(chunks: list[str], srt_text: str) -> list[dict[str, Any]]:
    actual_text = strip_srt_text(srt_text)
    rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        score, coverage, start, end, matched = best_fuzzy_window(chunk, actual_text)
        if score < 0.55 or coverage < 0.42:
            status = "MISSING"
        elif score < 0.78 or coverage < 0.70:
            status = "WEAK"
        else:
            status = "OK"
        rows.append(
            {
                "index": index,
                "status": status,
                "score": score,
                "coverage": coverage,
                "start": start,
                "end": end,
                "expected": chunk,
                "matched": matched,
            }
        )
    return rows


def rows_requiring_audio_patch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing = [row for row in rows if row["status"] == "MISSING"]
    if missing:
        return missing
    return [row for row in rows if row["status"] == "WEAK" and (row["score"] < 0.68 or row["coverage"] < 0.55)]


def parse_segmented_repair_segments(job: Path) -> list[dict[str, Any]]:
    report_path = job / "checks" / "tts_segmented_auto_repair.md"
    if not report_path.exists():
        return []

    segments: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
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
        result = re.match(r"^- Result:\s+(PASS|RETRY)\s+\((.*)\)", line)
        if result:
            current["passed"] = result.group(1) == "PASS"
            cer_match = re.search(r"CER=([0-9.]+)%", result.group(2))
            if cer_match:
                current["cer"] = float(cer_match.group(1)) / 100.0

    parsed: list[dict[str, Any]] = []
    for index in sorted(segments):
        accepted_audio = segments[index].get("accepted_audio")
        if accepted_audio and Path(accepted_audio).exists():
            attempts = segments[index]["attempts"]
            expected = str((attempts[0].get("expected") if attempts else "") or "")
            if not normalize_text(expected):
                return []
            parsed.append(
                {
                    "index": index,
                    "expected": expected,
                    "audio": Path(accepted_audio).resolve(),
                    "passed": bool(segments[index].get("accepted_passed", False)),
                    "cer": 0.0 if segments[index].get("accepted_passed", False) else 999.0,
                }
            )
            continue
        attempts = segments[index]["attempts"]
        usable = [attempt for attempt in attempts if attempt.get("audio") and Path(attempt["audio"]).exists()]
        if not usable:
            return []
        accepted = next((attempt for attempt in usable if attempt.get("passed")), None)
        if accepted is None:
            accepted = min(usable, key=lambda item: float(item.get("cer", 999.0)))
        expected = str(accepted.get("expected") or usable[0].get("expected") or "")
        if not normalize_text(expected):
            return []
        parsed.append(
            {
                "index": index,
                "expected": expected,
                "audio": Path(accepted["audio"]).resolve(),
                "passed": bool(accepted.get("passed")),
                "cer": float(accepted.get("cer", 999.0)),
            }
        )
    return parsed


def sentence_patch_sequence_file(job: Path) -> Path:
    return job / "tts" / "sentence_patch" / "current_sequence.json"


def chunks_signature(chunks: list[str]) -> str:
    payload = "\n".join(normalize_text(chunk) for chunk in chunks)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def consecutive_int_groups(values: list[int]) -> list[list[int]]:
    ordered = sorted(dict.fromkeys(values))
    if not ordered:
        return []
    groups: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value == groups[-1][-1] + 1:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def patch_locator_text(chunk_index: int, clause_indices: list[int] | None = None, clause_index: int | None = None) -> str:
    indices = list(clause_indices or [])
    if not indices and clause_index:
        indices = [clause_index]
    if not indices:
        return str(chunk_index)
    return ",".join(f"{chunk_index}.{index}" for index in indices)


def parse_patch_target_tokens(hint: str, chunk_count: int) -> list[tuple[int, int | None]]:
    raw = (hint or "").strip()
    if not raw:
        return []
    targets: list[tuple[int, int | None]] = []
    invalid: list[str] = []
    for token in [part for part in re.split(r"[\s,，;；、]+", raw) if part.strip()]:
        normalized = token.strip().upper().replace("Ｓ", "S").replace("Ｃ", "C")
        match = re.match(r"^S?0*(\d+)(?:[.。:_-]C?0*(\d+))?$", normalized)
        if not match:
            invalid.append(token)
            continue
        chunk_index = int(match.group(1))
        clause_index = int(match.group(2)) if match.group(2) else None
        if chunk_index < 1 or chunk_index > chunk_count:
            invalid.append(token)
            continue
        targets.append((chunk_index, clause_index))
    if invalid:
        raise ValueError("目标编号格式应为 3、3.2，或连续小分段 3.1,3.2,3.3。无效项: " + ", ".join(invalid))
    return targets


def parse_patch_target_hint(hint: str, chunk_count: int) -> tuple[int | None, int | None]:
    targets = parse_patch_target_tokens(hint, chunk_count)
    if not targets:
        return None, None
    if len(targets) > 1:
        raise ValueError("此处只接受一个目标编号；连续小分段请在单句补漏/替换试听中使用。")
    return targets[0]


def patch_units_from_sentence_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for sentence_index, sentence in enumerate(chunks, start=1):
        clauses = split_patch_clause_units(sentence)
        if not clauses:
            continue
        for clause_index, clause in enumerate(clauses, start=1):
            units.append(
                {
                    "sequence_index": len(units) + 1,
                    "sentence_index": sentence_index,
                    "clause_index": clause_index,
                    "locator": f"{sentence_index}.{clause_index}",
                    "text": clause,
                    "sentence": sentence,
                }
            )
    return units


def join_patch_unit_texts(units: list[dict[str, Any]]) -> str:
    if not units:
        return ""
    output = str(units[0].get("text") or "")
    previous_sentence = int(units[0].get("sentence_index") or 0)
    for unit in units[1:]:
        current_sentence = int(unit.get("sentence_index") or 0)
        separator = "。" if current_sentence != previous_sentence else "，"
        output += separator + str(unit.get("text") or "")
        previous_sentence = current_sentence
    return output


def patch_target_from_tokens(chunks: list[str], targets: list[tuple[int, int | None]], requested: str) -> dict[str, Any]:
    if not targets:
        raise ValueError("没有解析到有效目标编号。")
    all_units = patch_units_from_sentence_chunks(chunks)
    selected_units: list[dict[str, Any]] = []
    invalid: list[str] = []

    for chunk_index, clause_index in targets:
        if chunk_index < 1 or chunk_index > len(chunks):
            invalid.append(str(chunk_index))
            continue
        chunk_text = chunks[chunk_index - 1]
        clauses = split_patch_clause_units(chunk_text)
        if clause_index is None:
            selected_units.extend(
                unit
                for unit in all_units
                if int(unit.get("sentence_index") or 0) == chunk_index
            )
            continue
        if clause_index < 1 or clause_index > len(clauses):
            invalid.append(f"{chunk_index}.{clause_index}")
            continue
        selected_units.extend(
            unit
            for unit in all_units
            if int(unit.get("sentence_index") or 0) == chunk_index
            and int(unit.get("clause_index") or 0) == clause_index
        )

    if invalid:
        raise ValueError("目标小分段超出范围: " + ", ".join(invalid))
    if not selected_units:
        raise ValueError("没有解析到有效目标编号。")

    selected_units = sorted(
        {int(unit["sequence_index"]): unit for unit in selected_units}.values(),
        key=lambda unit: int(unit["sequence_index"]),
    )
    sequence_indices = [int(unit["sequence_index"]) for unit in selected_units]
    groups = consecutive_int_groups(sequence_indices)
    if len(groups) != 1:
        raise ValueError("多个小分段必须在全文最小分段序列里连续，例如 3.2,4.1 或 3.1,3.2。")

    first_unit = selected_units[0]
    last_unit = selected_units[-1]
    first_sentence_index = int(first_unit["sentence_index"])
    first_clause_index = int(first_unit["clause_index"])
    chunk_text = chunks[first_sentence_index - 1]
    source_target_text = join_patch_unit_texts(selected_units)
    target_locator = ",".join(str(unit["locator"]) for unit in selected_units)
    clause_indices = [
        int(unit["clause_index"])
        for unit in selected_units
        if int(unit["sentence_index"]) == first_sentence_index
    ]

    if len(selected_units) == len(split_patch_clause_units(chunk_text)) and first_sentence_index == int(last_unit["sentence_index"]):
        return {
            "chunk_index": first_sentence_index,
            "clause_index": None,
            "clause_indices": [],
            "target_text": requested or source_target_text,
            "source_chunk": chunk_text,
            "source_target_text": source_target_text,
            "target_units": selected_units,
            "target_locator": target_locator,
            "match_score": 1.0,
            "has_target_hint": True,
        }

    return {
        "chunk_index": first_sentence_index,
        "clause_index": first_clause_index,
        "clause_indices": clause_indices,
        "target_text": requested or source_target_text,
        "source_chunk": chunk_text,
        "source_target_text": source_target_text,
        "target_units": selected_units,
        "target_locator": target_locator,
        "match_score": 1.0,
        "has_target_hint": True,
    }


def find_clause_indices_for_text(chunk: str, target_text: str) -> list[int]:
    clauses = split_patch_clause_units(chunk)
    target_norm = normalize_text(target_text)
    if not clauses or not target_norm:
        return []
    for index, clause in enumerate(clauses, start=1):
        clause_norm = normalize_text(clause)
        if clause_norm == target_norm or target_norm in clause_norm or clause_norm in target_norm:
            return [index]
    for start in range(len(clauses)):
        combined = ""
        for end in range(start, len(clauses)):
            combined += clauses[end]
            combined_norm = normalize_text(combined)
            if combined_norm == target_norm or target_norm in combined_norm or combined_norm in target_norm:
                return list(range(start + 1, end + 2))
    return []


def sentence_candidate_score(target: str, candidate: str) -> float:
    target_norm = normalize_text(target)
    candidate_norm = normalize_text(candidate)
    if not target_norm or not candidate_norm:
        return 0.0
    if target_norm == candidate_norm:
        return 1.0
    if target_norm in candidate_norm or candidate_norm in target_norm:
        return 0.92
    return SequenceMatcher(None, target_norm, candidate_norm).ratio()


def resolve_sentence_patch_target(
    source_text: str,
    current_srt: str,
    patch_text: str,
    target_hint: str,
    chunk_chars: int,
) -> dict[str, Any]:
    chunks = split_patch_sentence_units(source_text or "")
    if not chunks:
        raise ValueError("没有可用的句号大分段，请先填写文案。")

    requested = (patch_text or "").strip()
    hint_targets = parse_patch_target_tokens(target_hint, len(chunks))
    if hint_targets:
        target = patch_target_from_tokens(chunks, hint_targets, requested)
    else:
        if not requested:
            raise ValueError("请填写要补漏或替换的单句文本，或填写目标编号。")
        best: dict[str, Any] | None = None
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_score = sentence_candidate_score(requested, chunk)
            candidate = {
                "chunk_index": chunk_index,
                "clause_index": None,
                "clause_indices": [],
                "target_text": requested,
                "source_chunk": chunk,
                "source_target_text": chunk,
                "match_score": chunk_score,
                "has_target_hint": False,
            }
            if best is None or chunk_score > float(best["match_score"]):
                best = candidate
            for clause_index, clause in enumerate(split_patch_clause_units(chunk), start=1):
                clause_score = sentence_candidate_score(requested, clause)
                candidate = {
                    "chunk_index": chunk_index,
                    "clause_index": clause_index,
                    "clause_indices": [clause_index],
                    "target_text": requested,
                    "source_chunk": chunk,
                    "source_target_text": clause,
                    "match_score": clause_score,
                    "has_target_hint": False,
                }
                if best is None or clause_score > float(best["match_score"]):
                    best = candidate
        if best is None or float(best["match_score"]) < 0.35:
            best = {
                "chunk_index": len(chunks),
                "clause_index": None,
                "clause_indices": [],
                "target_text": requested,
                "source_chunk": chunks[-1],
                "source_target_text": chunks[-1],
                "match_score": 0.0,
                "has_target_hint": False,
            }
        target = best

    score, coverage, _start, _end, matched = best_fuzzy_window(str(target["target_text"]), strip_srt_text(current_srt or ""))
    missing = score < 0.65 or coverage < 0.55
    source_target = str(target.get("source_target_text") or target.get("source_chunk") or "")
    has_target_hint = bool(target.get("has_target_hint"))
    is_targeted_rewrite = bool(requested) and has_target_hint and sentence_candidate_score(requested, source_target) < 0.86
    action = "replace" if has_target_hint else ("insert" if missing else "replace")
    action_reason = (
        "manual locator replacement"
        if has_target_hint
        else ("missing in raw ASR" if missing else "present in raw ASR")
    )
    target.update(
        {
            "chunks": chunks,
            "action": action,
            "action_reason": "targeted rewrite" if is_targeted_rewrite else action_reason,
            "missing": missing,
            "srt_score": score,
            "srt_coverage": coverage,
            "srt_matched": matched,
        }
    )
    return target


def load_audio_sequence(job: Path, chunks: list[str]) -> list[dict[str, Any]]:
    signature = chunks_signature(chunks)
    sequence_path = sentence_patch_sequence_file(job)
    if sequence_path.exists():
        try:
            payload = json.loads(sequence_path.read_text(encoding="utf-8-sig"))
            entries = payload.get("entries") or []
            if payload.get("chunks_signature") == signature and all(Path(str(entry.get("audio") or "")).exists() for entry in entries):
                return [dict(entry) for entry in entries]
        except Exception:
            pass

    segment_entries = parse_segmented_repair_segments(job)
    report_chunks = [str(entry["expected"]) for entry in segment_entries]
    chunks_match = (
        len(report_chunks) == len(chunks)
        and all(normalize_text(left) == normalize_text(right) for left, right in zip(report_chunks, chunks))
    )
    if not chunks_match:
        raise ValueError("当前任务没有可用的 2b 分段音频映射；请先重新运行“2b. 分段 TTS 自动修复”。")
    sequence: list[dict[str, Any]] = []
    for entry in segment_entries:
        audio = Path(entry["audio"])
        if not audio.exists():
            raise FileNotFoundError(f"分段音频不存在: {audio}")
        index = int(entry["index"])
        sequence.append({"kind": "segment", "chunk_index": index, "text": chunks[index - 1], "audio": str(audio)})
    return sequence


def save_audio_sequence(job: Path, chunks: list[str], entries: list[dict[str, Any]]) -> Path:
    path = sentence_patch_sequence_file(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chunks_signature": chunks_signature(chunks),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_audio_sequence_for_patch(job: Path, text: str, chunk_chars: int) -> tuple[list[str], list[dict[str, Any]]]:
    candidates = [
        minimal_patch_chunks(text or ""),
        split_patch_sentence_units(text or ""),
        split_tts_chunks(text or "", chunk_chars),
    ]
    seen: set[str] = set()
    errors: list[str] = []
    for chunks in candidates:
        signature = chunks_signature(chunks)
        if not chunks or signature in seen:
            continue
        seen.add(signature)
        try:
            return chunks, load_audio_sequence(job, chunks)
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("当前任务没有可用的音频分段索引；请先点击“3. 建立修补分段索引”。" + ("\n" + "\n".join(errors[-2:]) if errors else ""))


def sequence_insert_position(entries: list[dict[str, Any]], chunk_index: int) -> int:
    for index, entry in enumerate(entries):
        if int(entry.get("chunk_index") or 0) >= chunk_index:
            return index
    return len(entries)


def export_sentence_patch_audio_file(job: Path, candidate: dict[str, Any], candidate_audio: Path) -> tuple[Path, Path]:
    if not candidate_audio.exists():
        raise FileNotFoundError(f"单句候选音频不存在: {candidate_audio}")
    chunk_index = int(candidate.get("chunk_index") or 0)
    raw_clause_indices = candidate.get("clause_indices") or []
    clause_indices = [int(index) for index in raw_clause_indices if str(index).strip()] if isinstance(raw_clause_indices, list) else []
    clause_index = int(candidate.get("clause_index") or 0) or None
    locator = str(candidate.get("target_locator") or "") or (patch_locator_text(chunk_index, clause_indices, clause_index) if chunk_index else "manual")
    action = "insert" if candidate.get("action") == "insert" else "replace"
    target_text = str(candidate.get("target_text") or "").strip()
    text_slug = safe_name(target_text[:24] or candidate_audio.stem)
    suffix = candidate_audio.suffix or ".wav"
    export_dir = job / "exports" / "premiere_patch"
    export_dir.mkdir(parents=True, exist_ok=True)
    base_name = safe_name(f"{now_slug()}_{action}_{locator}_{text_slug}")
    export_audio = export_dir / f"{base_name}{suffix}"
    shutil.copy2(candidate_audio, export_audio)

    meta_path = export_dir / f"{base_name}.txt"
    meta_lines = [
        f"audio: {export_audio}",
        f"source_audio: {candidate_audio}",
        f"action: {action}",
        f"target: {locator}",
        f"text: {target_text}",
        f"report: {candidate.get('report') or ''}",
        f"created_at: {now_slug()}",
    ]
    meta_path.write_text("\n".join(meta_lines), encoding="utf-8")
    return export_audio, meta_path


def rebuild_chunk_with_clause_candidate(
    config: AppConfig,
    job: Path,
    chunk: str,
    chunk_index: int,
    clause_indices: int | list[int],
    candidate_audio: Path,
    voice_config: Path,
    asr_model: Any,
    text_lang_value: str,
    retries: int,
    pass_cer: float,
    patch_root: Path,
) -> Path:
    clauses = split_patch_clause_units(chunk)
    if isinstance(clause_indices, int):
        replace_indices = [clause_indices]
    else:
        replace_indices = [int(index) for index in clause_indices]
    replace_indices = sorted(dict.fromkeys(index for index in replace_indices if 1 <= index <= len(clauses)))
    if not replace_indices:
        return candidate_audio
    first_replace_index = replace_indices[0]
    replace_index_set = set(replace_indices)
    clause_audios: list[Path] = []
    for current_index, clause in enumerate(clauses, start=1):
        if current_index in replace_index_set:
            if current_index == first_replace_index:
                clause_audios.append(candidate_audio)
            continue
        audio_path, _lines, _passed = synthesize_best_chunk_audio(
            config,
            job,
            clause,
            current_index,
            voice_config,
            asr_model,
            text_lang_value,
            retries,
            pass_cer,
            patch_root / f"rebuild_{chunk_index:03d}",
            f"sentence_patch_rebuild_{chunk_index:03d}",
            allow_micro_repair=True,
            report_label=f"Sentence patch rebuild {chunk_index:03d}",
        )
        clause_audios.append(audio_path)
    return concat_audio_with_padding(
        config,
        clause_audios,
        patch_root / f"chunk_{chunk_index:03d}_rebuilt.wav",
        0.08,
        job / "logs" / f"sentence_patch_rebuild_{chunk_index:03d}_concat",
    )


def synthesize_best_chunk_audio(
    config: AppConfig,
    job: Path,
    chunk: str,
    index: int,
    voice_config: Path,
    asr_model: Any,
    text_lang_value: str,
    retries: int,
    pass_cer: float,
    patch_root: Path,
    log_prefix: str,
    allow_micro_repair: bool = True,
    report_label: str = "Patch Chunk",
) -> tuple[Path, list[str], bool]:
    best_audio: Path | None = None
    best_cer = 999.0
    best_asr = ""
    best_detail = ""
    passed = False
    report_lines: list[str] = []
    for attempt in range(1, tts_attempt_count(index, retries) + 1):
        request_text = tts_text_for_attempt(chunk, index, attempt)
        text_path = write_text(job / "tmp" / f"{log_prefix}_{index:03d}_try_{attempt:02d}.txt", request_text)
        out_dir = patch_root / f"{index:03d}" / f"try_{attempt:02d}"
        log_path = job / "logs" / f"{log_prefix}_{index:03d}_try_{attempt:02d}.txt"
        audio_path = run_gsv_tts(config, job, text_path, voice_config, out_dir, log_path)
        asr_text = transcribe_model_to_text(audio_path, asr_model, asr_language(text_lang_value))
        ok, cer, detail = segment_match_report(chunk, asr_text, pass_cer)
        if cer < best_cer:
            best_audio = audio_path
            best_cer = cer
            best_asr = asr_text
            best_detail = detail
        report_lines.extend(
            [
                f"## {report_label} {index:03d} attempt {attempt:02d}",
                "",
                f"- Expected: `{chunk}`",
                f"- TTS request: `{request_text}`",
                f"- ASR: `{asr_text}`",
                f"- Audio: `{audio_path}`",
                f"- Result: {'PASS' if ok else 'RETRY'} ({detail})",
                "",
            ]
        )
        if ok:
            passed = True
            best_audio = audio_path
            best_asr = asr_text
            best_detail = detail
            break

    if best_audio is None:
        raise RuntimeError(f"Patch chunk {index:03d} did not produce audio.")
    if not passed and allow_micro_repair:
        subchunks = split_micro_tts_chunks(chunk)
        if len(subchunks) > 1:
            report_lines.extend(
                [
                    f"### {report_label} {index:03d} micro repair",
                    "",
                    "The full chunk did not pass; splitting it into 10-20 char micro subchunks.",
                    f"- Subchunks: {len(subchunks)}",
                    "",
                ]
            )
            sub_audios: list[Path] = []
            sub_all_passed = True
            for sub_index, subchunk in enumerate(subchunks, start=1):
                sub_audio, sub_lines, sub_passed = synthesize_best_chunk_audio(
                    config,
                    job,
                    subchunk,
                    sub_index,
                    voice_config,
                    asr_model,
                    text_lang_value,
                    retries,
                    pass_cer,
                    patch_root / f"{index:03d}_micro",
                    f"{log_prefix}_{index:03d}_micro",
                    allow_micro_repair=False,
                    report_label=f"{report_label} {index:03d} micro",
                )
                sub_audios.append(sub_audio)
                sub_all_passed = sub_all_passed and sub_passed
                report_lines.extend(sub_lines)

            micro_audio = concat_audio_with_padding(
                config,
                sub_audios,
                patch_root / f"{index:03d}" / "micro_repaired.wav",
                0.08,
                job / "logs" / f"{log_prefix}_{index:03d}_micro_concat",
            )
            micro_asr = transcribe_model_to_text(micro_audio, asr_model, asr_language(text_lang_value))
            micro_ok, micro_cer, micro_detail = segment_match_report(chunk, micro_asr, pass_cer)
            report_lines.extend(
                [
                    f"### {report_label} {index:03d} micro result",
                    "",
                    f"- ASR: `{micro_asr}`",
                    f"- Audio: `{micro_audio}`",
                    f"- Result: {'PASS' if micro_ok else 'RETRY'} ({micro_detail})",
                    "",
                ]
            )
            if micro_ok or micro_cer < best_cer or (sub_all_passed and micro_cer <= best_cer + 0.05):
                best_audio = micro_audio
                best_asr = micro_asr
                best_cer = micro_cer
                best_detail = micro_detail
                passed = micro_ok or sub_all_passed
    if not passed:
        report_lines.extend(
            [
                f"### {report_label} {index:03d} fallback",
                "",
                "No attempt passed; using the best attempt.",
                f"- Best CER: {best_cer:.2%}",
                f"- Best ASR: `{best_asr}`",
                f"- Detail: {best_detail}",
                "",
            ]
        )
    return best_audio, report_lines, passed


def synthesize_clause_rebuilt_segment_audio(
    config: AppConfig,
    job: Path,
    chunk: str,
    index: int,
    voice_config: Path,
    asr_model: Any,
    text_lang_value: str,
    retries: int,
    pass_cer: float,
    patch_root: Path,
    log_prefix: str,
    focus_rows: list[dict[str, Any]] | None = None,
    report_label: str = "Patch Chunk",
) -> tuple[Path, list[str], bool]:
    clauses = split_patch_clause_units(chunk)
    if len(clauses) <= 1:
        return synthesize_best_chunk_audio(
            config,
            job,
            chunk,
            index,
            voice_config,
            asr_model,
            text_lang_value,
            retries,
            pass_cer,
            patch_root,
            log_prefix,
            allow_micro_repair=True,
            report_label=report_label,
        )

    report_lines: list[str] = [
        f"## {report_label} {index:03d} clause rebuild",
        "",
        "Rebuilding this segment from comma-level clauses and replacing it at the original segment position.",
        f"- Original segment: `{chunk}`",
        f"- Clauses: {len(clauses)}",
        "",
    ]
    if focus_rows:
        report_lines.extend(["### Clause review triggers", ""])
        for row in focus_rows:
            report_lines.append(
                f"- Clause {row['index']:02d} {row['status']} "
                f"(score={row['score']:.2f}, coverage={row['coverage']:.2f}): `{row['expected']}`"
            )
        report_lines.append("")

    clause_audios: list[Path] = []
    all_passed = True
    focus_indices = [
        int(row["index"])
        for row in focus_rows or []
        if 1 <= int(row.get("index") or 0) <= len(clauses)
    ]
    grouped_focus = {
        group[0]: group
        for group in consecutive_int_groups(focus_indices)
        if len(group) > 1
    }
    clause_index = 1
    while clause_index <= len(clauses):
        group = grouped_focus.get(clause_index)
        if group:
            clause = "，".join(clauses[index - 1] for index in group)
            report_lines.extend(
                [
                    f"### {report_label} {index:03d} clauses {group[0]:02d}-{group[-1]:02d}",
                    "",
                    "Continuous manual clause sequence; generating as one replacement sentence.",
                    f"- Text: `{clause}`",
                    "",
                ]
            )
            next_clause_index = group[-1] + 1
        else:
            clause = clauses[clause_index - 1]
            next_clause_index = clause_index + 1
        clause_audio, clause_lines, clause_passed = synthesize_best_chunk_audio(
            config,
            job,
            clause,
            clause_index,
            voice_config,
            asr_model,
            text_lang_value,
            retries,
            pass_cer,
            patch_root / f"{index:03d}_clauses",
            f"{log_prefix}_{index:03d}_clause",
            allow_micro_repair=True,
            report_label=f"{report_label} {index:03d} clause",
        )
        clause_audios.append(clause_audio)
        all_passed = all_passed and clause_passed
        report_lines.extend(clause_lines)
        clause_index = next_clause_index

    rebuilt_audio = concat_audio_with_padding(
        config,
        clause_audios,
        patch_root / f"{index:03d}" / "clause_rebuilt.wav",
        0.08,
        job / "logs" / f"{log_prefix}_{index:03d}_clause_concat",
    )
    asr_text = transcribe_model_to_text(rebuilt_audio, asr_model, asr_language(text_lang_value))
    ok, cer, detail = segment_match_report(chunk, asr_text, pass_cer)
    report_lines.extend(
        [
            f"### {report_label} {index:03d} clause rebuild result",
            "",
            f"- ASR: `{asr_text}`",
            f"- Audio: `{rebuilt_audio}`",
            f"- Result: {'PASS' if ok else 'FALLBACK'} ({detail})",
            "",
        ]
    )
    return rebuilt_audio, report_lines, ok or all_passed


def run_gsv_tts(
    config: AppConfig,
    job: Path,
    text_path: Path,
    voice_config: Path,
    out_dir: Path,
    log_path: Path,
) -> Path:
    command = [
        str(config.python_exe),
        str(config.gsv_tts_script),
        "--text-file",
        str(text_path),
        "--config",
        str(voice_config),
        "--output-dir",
        str(out_dir),
        "--no-open",
    ]
    result = run_command(command, config.project_root, log_path)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    audio_path = parse_saved_audio(result.stdout, out_dir)
    if not audio_path:
        raise RuntimeError("TTS completed but no output audio was found.")
    return audio_path


def concat_audio_with_padding(
    config: AppConfig,
    audio_paths: list[Path],
    output: Path,
    pad_seconds: float,
    work_dir: Path,
) -> Path:
    if not audio_paths:
        raise ValueError("No audio segments to concatenate.")
    work_dir.mkdir(parents=True, exist_ok=True)
    padded: list[Path] = []
    pad_seconds = max(0.0, min(1.5, float(pad_seconds or 0.0)))

    for index, source in enumerate(audio_paths, start=1):
        target = work_dir / f"padded_{index:03d}.wav"
        if pad_seconds > 0:
            silence = "anullsrc=channel_layout=stereo:sample_rate=44100"
            command = [
                str(config.ffmpeg),
                "-y",
                "-f",
                "lavfi",
                "-t",
                f"{pad_seconds:.3f}",
                "-i",
                silence,
                "-i",
                str(source),
                "-f",
                "lavfi",
                "-t",
                f"{pad_seconds:.3f}",
                "-i",
                silence,
                "-filter_complex",
                (
                    "[0:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[pre];"
                    "[1:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[speech];"
                    "[2:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[post];"
                    "[pre][speech][post]concat=n=3:v=0:a=1[a]"
                ),
                "-map",
                "[a]",
                "-c:a",
                "pcm_s16le",
                str(target),
            ]
        else:
            command = [
                str(config.ffmpeg),
                "-y",
                "-i",
                str(source),
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(target),
            ]
        result = run_command(command, config.project_root, work_dir / f"pad_{index:03d}.txt")
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
        padded.append(target.resolve())

    list_path = work_dir / "concat_list.txt"
    list_path.write_text(
        "".join(f"file '{str(path).replace(chr(92), '/')}'\n" for path in padded),
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.ffmpeg),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    result = run_command(command, config.project_root, work_dir / "concat.txt")
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return output.resolve()


def prepend_audio_silence(config: AppConfig, source: Path, output: Path, pad_seconds: float, log_path: Path) -> Path:
    pad_seconds = max(0.0, float(pad_seconds or 0.0))
    if pad_seconds <= 0.0:
        return source.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.ffmpeg),
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{pad_seconds:.3f}",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-i",
        str(source),
        "-filter_complex",
        (
            "[0:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[pre];"
            "[1:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[speech];"
            "[pre][speech]concat=n=2:v=0:a=1[a]"
        ),
        "-map",
        "[a]",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    result = run_command(command, config.project_root, log_path)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return output.resolve()


def cut_audio_segment(config: AppConfig, source: Path, output: Path, start: float, end: float, log_path: Path) -> Path:
    start = max(0.0, float(start or 0.0))
    end = max(start + 0.03, float(end or 0.0))
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.ffmpeg),
        "-y",
        "-i",
        str(source),
        "-af",
        f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    result = run_command(command, config.project_root, log_path)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return output.resolve()


def make_analysis_wav(config: AppConfig, source: Path, output: Path, log_path: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.ffmpeg),
        "-y",
        "-i",
        str(source),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    result = run_command(command, config.project_root, log_path)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return output.resolve()


def read_mono_i16_wav(path: Path) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"analysis wav must be mono s16: {path}")
        rate = handle.getframerate()
        payload = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, rate


def quiet_boundary_in_analysis_wav(
    analysis_wav: Path,
    boundary: float,
    search_start: float,
    search_end: float,
) -> float:
    samples, rate = read_mono_i16_wav(analysis_wav)
    return quiet_boundary_in_samples(samples, rate, boundary, search_start, search_end)


def quiet_boundary_in_samples(
    samples: array.array,
    rate: int,
    boundary: float,
    search_start: float,
    search_end: float,
) -> float:
    if rate <= 0 or not samples:
        return boundary
    duration = len(samples) / float(rate)
    search_start = max(0.0, min(duration, search_start))
    search_end = max(search_start, min(duration, search_end))
    if search_end - search_start < 0.015:
        return max(search_start, min(search_end, boundary))

    start_sample = int(search_start * rate)
    end_sample = min(len(samples), int(search_end * rate))
    window = max(1, int(0.028 * rate))
    step = max(1, int(0.006 * rate))
    if end_sample - start_sample <= window:
        return (search_start + search_end) / 2.0

    abs_values = [abs(value) for value in samples[start_sample:end_sample]]
    prefix = [0]
    for value in abs_values:
        prefix.append(prefix[-1] + value)
    max_energy = max(1.0, max(abs_values) if abs_values else 1.0)
    span = max(0.001, search_end - search_start)
    best_score: float | None = None
    best_time = boundary
    for offset in range(0, len(abs_values) - window + 1, step):
        energy = (prefix[offset + window] - prefix[offset]) / float(window)
        center_sample = start_sample + offset + (window // 2)
        candidate_time = center_sample / float(rate)
        distance_penalty = (abs(candidate_time - boundary) / span) * max_energy * 0.12
        score = energy + distance_penalty
        if best_score is None or score < best_score:
            best_score = score
            best_time = candidate_time
    return max(search_start, min(search_end, best_time))


def manual_patch_boundary_search_seconds(config: AppConfig) -> float:
    return max(0.08, min(1.2, config_float(config, "manual_patch_boundary_search_ms", 550.0) / 1000.0))


def manual_patch_neighbor_bleed_seconds(config: AppConfig) -> float:
    return max(0.0, min(0.25, config_float(config, "manual_patch_neighbor_bleed_ms", 80.0) / 1000.0))


def manual_patch_crossfade_seconds(config: AppConfig) -> float:
    return max(0.0, min(0.12, config_float(config, "manual_patch_crossfade_ms", 45.0) / 1000.0))


def refine_patch_unit_boundaries_with_audio(
    config: AppConfig,
    audio_path: Path,
    intervals: list[dict[str, Any]],
    work_dir: Path,
) -> list[dict[str, Any]]:
    if len(intervals) < 2:
        return intervals
    refined = [dict(interval) for interval in intervals]
    try:
        analysis_wav = make_analysis_wav(config, audio_path, work_dir / "manual_patch_boundary_analysis.wav", work_dir / "manual_patch_boundary_analysis.txt")
        analysis_samples, analysis_rate = read_mono_i16_wav(analysis_wav)
        duration = audio_duration_seconds(config, audio_path) or float(refined[-1].get("end") or 0.0)
        search = manual_patch_boundary_search_seconds(config)
        min_unit = 0.05

        for index in range(1, len(refined)):
            previous = refined[index - 1]
            current = refined[index]
            previous_start = max(0.0, min(duration, float(previous.get("start") or 0.0)))
            previous_end = max(previous_start + min_unit, min(duration, float(previous.get("end") or previous_start)))
            current_start = max(0.0, min(duration, float(current.get("start") or previous_end)))
            current_end = max(current_start + min_unit, min(duration, float(current.get("end") or current_start)))

            raw_boundary = (previous_end + current_start) / 2.0
            search_start = max(previous_start + min_unit, min(previous_end, current_start) - search)
            search_end = min(current_end - min_unit, max(previous_end, current_start) + search)
            if search_end - search_start < 0.02:
                boundary = max(search_start, min(search_end, raw_boundary))
                source = "audio_guarded"
            else:
                boundary = quiet_boundary_in_samples(analysis_samples, analysis_rate, raw_boundary, search_start, search_end)
                source = "audio_quiet"

            boundary = max(previous_start + min_unit, min(current_end - min_unit, boundary))
            previous["raw_end"] = previous.get("end")
            current["raw_start"] = current.get("start")
            previous["end"] = boundary
            current["start"] = boundary
            previous["boundary_source"] = source
            current["boundary_source"] = source

        for interval in refined:
            interval["start"] = max(0.0, min(duration, float(interval.get("start") or 0.0)))
            interval["end"] = max(float(interval["start"]) + 0.04, min(duration, float(interval.get("end") or 0.0)))
            interval["refined_with_audio"] = True
        return refined
    except Exception as exc:
        fallback = [dict(interval) for interval in intervals]
        for interval in fallback:
            interval["refined_with_audio"] = False
            interval["refine_error"] = str(exc)
        return fallback


def concat_audio_with_crossfade(config: AppConfig, audio_paths: list[Path], output: Path, fade_seconds: float, work_dir: Path) -> Path:
    if not audio_paths:
        raise ValueError("No audio segments to concatenate.")
    if len(audio_paths) == 1:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_paths[0], output)
        return output.resolve()
    fade_seconds = max(0.0, min(0.12, float(fade_seconds or 0.0)))
    if fade_seconds <= 0:
        return concat_audio_with_padding(config, audio_paths, output, 0.0, work_dir)
    durations = [audio_duration_seconds(config, path) for path in audio_paths]
    known_durations = [float(value) for value in durations if value is not None and value > 0]
    if known_durations:
        fade_seconds = min(fade_seconds, max(0.0, min(known_durations) / 3.0))
    if fade_seconds < 0.008:
        return concat_audio_with_padding(config, audio_paths, output, 0.0, work_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    command = [str(config.ffmpeg), "-y"]
    for path in audio_paths:
        command.extend(["-i", str(path)])

    filters: list[str] = []
    for index in range(len(audio_paths)):
        filters.append(
            f"[{index}:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo[a{index}]"
        )
    current = "a0"
    for index in range(1, len(audio_paths)):
        out_label = f"xf{index}"
        filters.append(f"[{current}][a{index}]acrossfade=d={fade_seconds:.3f}:c1=tri:c2=tri[{out_label}]")
        current = out_label
    output.parent.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{current}]",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    result = run_command(command, config.project_root, work_dir / "crossfade_concat.txt")
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return output.resolve()


def splice_audio_replace_range(
    config: AppConfig,
    source: Path,
    patch_audio: Path,
    output: Path,
    start: float,
    end: float,
    work_dir: Path,
    start_search: tuple[float, float] | None = None,
    end_search: tuple[float, float] | None = None,
) -> Path:
    duration = audio_duration_seconds(config, source)
    if duration is None:
        raise RuntimeError(f"无法读取音频时长: {source}")
    start = max(0.0, min(float(start or 0.0), duration))
    end = max(start, min(float(end or start), duration))
    if end <= start:
        raise ValueError("替换区间无效，无法裁掉原音频片段。")

    boundary_report: dict[str, Any] = {
        "source": str(source),
        "patch_audio": str(patch_audio),
        "duration": duration,
        "input_start": start,
        "input_end": end,
        "start_search": list(start_search) if start_search else None,
        "end_search": list(end_search) if end_search else None,
    }

    try:
        analysis_wav = make_analysis_wav(config, source, work_dir / "analysis_mono.wav", work_dir / "analysis_wav.txt")
        if start_search is None:
            start_window = (start, min(end - 0.05, start + 0.35))
        else:
            start_window = start_search
        if end_search is None:
            end_window = (max(start + 0.05, end - 0.35), end)
        else:
            end_window = end_search
        start_window = (
            max(0.0, min(duration, float(start_window[0]))),
            max(0.0, min(duration, float(start_window[1]))),
        )
        end_window = (
            max(0.0, min(duration, float(end_window[0]))),
            max(0.0, min(duration, float(end_window[1]))),
        )
        refined_start = quiet_boundary_in_analysis_wav(
            analysis_wav,
            start,
            min(start_window),
            max(start_window),
        )
        refined_end = quiet_boundary_in_analysis_wav(
            analysis_wav,
            end,
            max(refined_start + 0.05, min(end_window)),
            max(end_window),
        )
        boundary_report.update(
            {
                "refined_start": refined_start,
                "refined_end": refined_end,
                "effective_start_search": [min(start_window), max(start_window)],
                "effective_end_search": [max(refined_start + 0.05, min(end_window)), max(end_window)],
            }
        )
        if refined_end - refined_start >= 0.05:
            start, end = refined_start, refined_end
    except Exception as exc:
        boundary_report["refine_error"] = str(exc)

    parts: list[Path] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    if start > 0.02:
        parts.append(cut_audio_segment(config, source, work_dir / "before.wav", 0.0, start, work_dir / "cut_before.txt"))
    parts.append(patch_audio)
    if duration - end > 0.02:
        parts.append(cut_audio_segment(config, source, work_dir / "after.wav", end, duration, work_dir / "cut_after.txt"))
    boundary_report.update({"final_start": start, "final_end": end, "crossfade_seconds": manual_patch_crossfade_seconds(config)})
    (work_dir / "splice_boundaries.json").write_text(json.dumps(boundary_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return concat_audio_with_crossfade(config, parts, output, manual_patch_crossfade_seconds(config), work_dir / "concat")


def shift_srt_timestamps(srt_text: str, offset_seconds: float) -> str:
    offset_seconds = max(0.0, float(offset_seconds or 0.0))
    blocks: list[str] = []
    for fallback_index, block in enumerate(parse_srt_blocks(srt_text), start=1):
        time_line = block.get("time") or ""
        match = re.match(r"\s*(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", time_line)
        if not match:
            continue
        start = parse_srt_timestamp(match.group(1)) + offset_seconds
        end = parse_srt_timestamp(match.group(2)) + offset_seconds
        index = str(block.get("index") or fallback_index)
        blocks.append("\n".join([index, f"{srt_timestamp(start)} --> {srt_timestamp(end)}", str(block["text"]).strip()]))
    if not blocks:
        return srt_text
    return "\n\n".join(blocks).strip() + "\n"


def render_srt_from_editor(config: AppConfig, audio_path: Path, editor_text: str, offset_seconds: float = 0.0) -> str:
    text = (editor_text or "").strip()
    if not text:
        return ""
    if "-->" in text:
        return shift_srt_timestamps(text, offset_seconds) if offset_seconds > 0 else text.strip() + "\n"
    pages = editor_subtitle_pages_preserve_lines(text)
    if not pages:
        return ""
    duration = audio_duration_seconds(config, audio_path) or float(len(pages))
    duration = max(0.1, duration)
    blocks: list[str] = []
    for index, page in enumerate(pages, start=1):
        start = offset_seconds + duration * ((index - 1) / len(pages))
        end = offset_seconds + (duration if index == len(pages) else duration * (index / len(pages)))
        blocks.append(make_srt_block(index, start, end, page))
    return "\n\n".join(blocks).strip() + "\n"


def create_ui(config: AppConfig):
    import gradio as gr  # type: ignore

    labels = [model_label(model) for model in config.models]
    default_label = labels[0] if labels else None
    default_model = config.models[0] if config.models else {}
    gpt_choices = file_choices(config, gpt_weight_files(config))
    sovits_choices = file_choices(config, sovits_weight_files(config))
    default_gpt = existing_weight_or_first(config, default_model.get("gpt_weights_path"), gpt_choices)
    default_sovits = existing_weight_or_first(config, default_model.get("sovits_weights_path"), sovits_choices)
    ref_choices = reference_choices(config)
    default_ref_id = default_reference_id(config, default_model) if default_model else ""
    default_ref_text = read_asset_reference_text(config, reference_by_id(config, default_ref_id))
    default_prompt_lang = str(model_setting(config, default_model, "prompt_lang", "zh"))
    default_text_lang = str(model_setting(config, default_model, "text_lang", "zh"))
    default_split = str(model_setting(config, default_model, "text_split_method", "cut1"))
    default_speed = float(model_setting(config, default_model, "speed_factor", 1.0))
    default_fragment = float(model_setting(config, default_model, "fragment_interval", 0.3))
    default_top_k = int(model_setting(config, default_model, "top_k", 15))
    default_top_p = float(model_setting(config, default_model, "top_p", 1.0))
    default_temperature = float(model_setting(config, default_model, "temperature", 1.0))
    agent_cfg = config.raw.get("agent_review", {}) if isinstance(config.raw.get("agent_review", {}), dict) else {}
    default_review_mode = str(agent_cfg.get("mode") or "local")
    default_agent_url = str(agent_cfg.get("api_url") or "")
    default_agent_model = str(agent_cfg.get("model") or "")
    missing = require_paths(config)
    startup_note = "路径检查通过" if not missing else "路径缺失:\n" + "\n".join(missing)

    with gr.Blocks(title="Local TTS Video Studio", css=APP_CSS, elem_id="video-webui") as demo:
        browser_heartbeat_state = gr.Textbox(value="", visible=False)
        browser_heartbeat_timer = gr.Timer(value=BROWSER_HEARTBEAT_INTERVAL_SECONDS, active=True)
        with gr.Row(elem_classes=["app-header"]):
            gr.Markdown("# Local TTS Video Studio")

        job_state = gr.State("")
        image_state = gr.State("")
        audio_state = gr.State("")
        video_state = gr.State("")
        srt_state = gr.State("")
        source_text_state = gr.State("")
        sentence_patch_state = gr.State("")

        with gr.Row(elem_classes=["dense-row"]):
            model = gr.Dropdown(labels, value=default_label, label="角色预设", scale=2)
            job_name = gr.Textbox(label="任务名", placeholder="可选，例如 mutsumi_001", scale=1)

        with gr.Row(equal_height=False, elem_classes=["main-grid"]):
            with gr.Column(scale=7):
                with gr.Tabs():
                    with gr.Tab("输入素材"):
                        source_text = gr.Textbox(
                            label="旁白文案",
                            lines=9,
                            placeholder="输入要合成/校对的文案",
                            elem_classes=["source-text"],
                        )
                        with gr.Row(elem_classes=["dense-row"]):
                            image_file = gr.File(label="图片", file_types=["image"])
                            uploaded_audio = gr.File(label="旁白音频（可选，跳过 TTS）", file_types=["audio"])
                        with gr.Row(elem_classes=["dense-row"]):
                            bgm_file = gr.File(label="BGM（可选）", file_types=["audio"])
                            uploaded_srt = gr.File(label="SRT（可选，跳过 ASR）", file_types=[".srt"])

                    with gr.Tab("模型与参考"):
                        with gr.Row(elem_classes=["dense-row"]):
                            gpt_weights = gr.Dropdown(
                                choices=gpt_choices,
                                value=default_gpt,
                                label="GPT weights",
                                allow_custom_value=True,
                            )
                            sovits_weights = gr.Dropdown(
                                choices=sovits_choices,
                                value=default_sovits,
                                label="SoVITS weights",
                                allow_custom_value=True,
                            )
                        with gr.Row(elem_classes=["dense-row"]):
                            preset_name = gr.Textbox(
                                label="参考模型预设名称",
                                placeholder="例如 Mutsumi v2Pro / 新角色名",
                                scale=3,
                            )
                            btn_save_model_preset = gr.Button("保存参考模型预设", scale=1)
                        with gr.Row(elem_classes=["dense-row"]):
                            reference_preset = gr.Dropdown(
                                choices=ref_choices,
                                value=default_ref_id,
                                label="参考音频/文本预设",
                                allow_custom_value=True,
                                scale=3,
                            )
                            btn_refresh_weights = gr.Button("刷新模型路径", scale=1)
                        with gr.Row(elem_classes=["dense-row"]):
                            ref_audio_file = gr.File(label="参考音频（可选，覆盖预设）", file_types=["audio"])
                            no_ref_text = gr.Checkbox(label="无参考文本模式", value=False)
                        reference_text = gr.Textbox(
                            label="参考音频文本",
                            lines=3,
                            value=default_ref_text,
                            elem_classes=["reference-text"],
                        )
                        with gr.Row(elem_classes=["dense-row"]):
                            prompt_lang = gr.Dropdown(
                                choices=language_choices(),
                                value=default_prompt_lang,
                                label="参考音频语种",
                            )
                            text_lang = gr.Dropdown(
                                choices=language_choices(),
                                value=default_text_lang,
                                label="合成文本语种",
                            )
                            text_split_method = gr.Dropdown(
                                choices=text_split_choices(),
                                value=default_split,
                                label="文本切分方式",
                            )

                    with gr.Tab("推理参数"):
                        with gr.Row(elem_classes=["dense-row"]):
                            speed_factor = gr.Slider(0.6, 1.6, value=default_speed, step=0.01, label="语速")
                            fragment_interval = gr.Slider(0.0, 1.5, value=default_fragment, step=0.05, label="句间停顿秒")
                            bgm_volume = gr.Slider(0.0, 0.4, value=0.12, step=0.01, label="BGM 音量")
                            bgm_start = gr.Number(value=0.0, label="BGM 起点秒")
                        with gr.Row(elem_classes=["dense-row"]):
                            top_k = gr.Slider(1, 100, value=default_top_k, step=1, label="top_k")
                            top_p = gr.Slider(0.0, 1.0, value=default_top_p, step=0.05, label="top_p")
                            temperature = gr.Slider(0.1, 2.0, value=default_temperature, step=0.05, label="temperature")
                        with gr.Row(elem_classes=["dense-row"]):
                            btn_save_inference_preset = gr.Button("保存推理参数到角色预设", scale=1)
                        with gr.Row(elem_classes=["dense-row"]):
                            crop_x = gr.Slider(0.0, 1.0, value=0.5, step=0.01, label="裁切 X")
                            crop_y = gr.Slider(0.0, 1.0, value=0.5, step=0.01, label="裁切 Y")
                            auto_chunk_chars = gr.Slider(30, 180, value=45, step=5, label="自动修复分段字数")
                            auto_retries = gr.Slider(0, 4, value=2, step=1, label="失败片段重试次数")
                        with gr.Row(elem_classes=["dense-row"]):
                            auto_pass_cer = gr.Slider(0.02, 0.30, value=0.14, step=0.01, label="ASR 通过 CER")
                            auto_pad_ms = gr.Slider(0, 800, value=160, step=20, label="片段首尾静音 ms")

            with gr.Column(scale=4):
                status = gr.Textbox(label="状态 / 日志摘要", lines=8, value=startup_note, elem_classes=["status-box"])
                generated_audio = gr.Audio(label="旁白试听", type="filepath")
                with gr.Tabs():
                    with gr.Tab("视频预览"):
                        video_preview = gr.Video(label="视频预览", elem_classes=["preview-media"])
                    with gr.Tab("裁切预览"):
                        crop_preview = gr.Image(label="裁切预览", type="filepath", elem_classes=["preview-media"])

        with gr.Row(elem_classes=["command-bar"]):
            btn_prepare = gr.Button("1. 准备任务")
            btn_tts = gr.Button("2. 生成 TTS")
            btn_tts_repair = gr.Button("3. 建立修补分段索引")
            btn_crop = gr.Button("4. 生成裁切预览")
            btn_asr = gr.Button("5. 字幕生成/重排/审核")
            btn_render = gr.Button("6. 渲染视频", variant="primary")

        with gr.Accordion("SRT 校对与任务目录", open=False):
            with gr.Row(elem_classes=["dense-row"]):
                review_mode = gr.Dropdown(
                    choices=[("本地规则审核", "local"), ("本地 + Chat API", "chat")],
                    value="chat" if default_review_mode in {"chat", "agent", "local+agent"} else "local",
                    label="二次审核模式",
                )
                agent_api_url = gr.Textbox(
                    label="Chat API URL",
                    value=default_agent_url,
                    placeholder="http://127.0.0.1:1234/v1/chat/completions",
                )
                agent_model = gr.Textbox(
                    label="Chat 模型名",
                    value=default_agent_model,
                    placeholder="local-reviewer",
                )
                agent_api_key = gr.Textbox(
                    label="Chat API Key（可选）",
                    value="",
                    type="password",
                    placeholder="留空则读取 AGENT_REVIEW_API_KEY",
                )
            srt_editor = gr.Textbox(label="SRT 校对编辑器", lines=10, elem_classes=["srt-editor"])
            review_report = gr.Textbox(label="二次审核报告", lines=10, elem_classes=["srt-editor"])
            with gr.Accordion("单句补漏 / 替换试听", open=True):
                with gr.Row(elem_classes=["dense-row"]):
                    btn_list_manual_patch = gr.Button("列出补修编号", scale=1)
                manual_patch_options = gr.Textbox(label="可补修分段/小句", lines=8, elem_classes=["srt-editor"])
                sentence_patch_text = gr.Textbox(
                    label="单句文本",
                    placeholder="填写要补漏或重新修饰的一句；或先在目标编号填 3 / 3.2 / 3.1,3.2 自动匹配文本",
                    lines=2,
                )
                with gr.Row(elem_classes=["dense-row"]):
                    sentence_patch_target = gr.Textbox(
                        label="目标编号（可选）",
                        placeholder="例如 3 或 3.2；连续小段如 3.1,3.2,3.3",
                        scale=1,
                    )
                    btn_sentence_patch_preview = gr.Button("生成单句试听", scale=1)
                    btn_sentence_patch_apply = gr.Button("应用到当前音频", scale=1)
                with gr.Row(elem_classes=["dense-row"]):
                    sentence_speed = gr.Slider(0.6, 1.6, value=default_speed, step=0.01, label="单句语速")
                    sentence_fragment = gr.Slider(0.0, 1.5, value=default_fragment, step=0.05, label="单句停顿")
                    sentence_top_k = gr.Slider(1, 100, value=default_top_k, step=1, label="单句 top_k")
                with gr.Row(elem_classes=["dense-row"]):
                    sentence_top_p = gr.Slider(0.0, 1.0, value=default_top_p, step=0.05, label="单句 top_p")
                    sentence_temperature = gr.Slider(0.1, 2.0, value=default_temperature, step=0.05, label="单句 temperature")
                    sentence_patch_audio = gr.Audio(label="单句试听", type="filepath")
            rendered_video_path = gr.Textbox(value="", visible=False)
            output_dir = gr.Textbox(label="当前任务目录")

        def on_model_preset_change(label: str):
            try:
                selected = model_by_label(config, label)
                current_gpt_choices = file_choices(config, gpt_weight_files(config))
                current_sovits_choices = file_choices(config, sovits_weight_files(config))
                ref_id = default_reference_id(config, selected)
                ref_asset = reference_by_id(config, ref_id)
                return (
                    gr.update(
                        choices=current_gpt_choices,
                        value=existing_weight_or_first(config, selected.get("gpt_weights_path"), current_gpt_choices),
                    ),
                    gr.update(
                        choices=current_sovits_choices,
                        value=existing_weight_or_first(config, selected.get("sovits_weights_path"), current_sovits_choices),
                    ),
                    gr.update(value=ref_id),
                    read_asset_reference_text(config, ref_asset),
                    str(model_setting(config, selected, "prompt_lang", ref_asset.get("prompt_lang", "zh") if ref_asset else "zh")),
                    str(model_setting(config, selected, "text_lang", "zh")),
                    str(model_setting(config, selected, "text_split_method", "cut1")),
                    float(model_setting(config, selected, "speed_factor", 1.0)),
                    float(model_setting(config, selected, "fragment_interval", 0.3)),
                    int(model_setting(config, selected, "top_k", 15)),
                    float(model_setting(config, selected, "top_p", 1.0)),
                    float(model_setting(config, selected, "temperature", 1.0)),
                )
            except Exception:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    "",
                    "zh",
                    "zh",
                    "cut1",
                    1.0,
                    0.3,
                    15,
                    1.0,
                    1.0,
                )

        def on_reference_preset_change(ref_id: str):
            asset = reference_by_id(config, ref_id)
            if not asset:
                return "", "zh"
            return read_asset_reference_text(config, asset), str(asset.get("prompt_lang") or "zh")

        def on_gpt_weights_change(gpt_weights_value: str):
            if is_base_model_choice(gpt_weights_value):
                return (
                    gr.update(choices=file_choices(config, sovits_weight_files(config)), value=BASE_MODEL_VALUE),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    "已切换到底模推理：将跳过自定义 GPT / SoVITS 权重，只使用参考音频和参考文本。",
                )
            matched = model_by_gpt_weights(config, gpt_weights_value)
            matched_sovits = matching_sovits_for_gpt(config, gpt_weights_value)
            current_sovits_choices = file_choices(config, sovits_weight_files(config))
            if not matched:
                return (
                    gr.update(value=str(matched_sovits)) if matched_sovits else gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    "未找到与当前 GPT weights 绑定的参考模型预设",
                )
            ref_id = default_reference_id(config, matched)
            ref_asset = reference_by_id(config, ref_id)
            sovits_value = matched_sovits or resolve_optional_weight_path(config, matched.get("sovits_weights_path"))
            return (
                gr.update(value=existing_weight_or_first(config, str(sovits_value) if sovits_value else "", current_sovits_choices)),
                gr.update(value=ref_id),
                read_asset_reference_text(config, ref_asset),
                str(model_setting(config, matched, "prompt_lang", ref_asset.get("prompt_lang", "zh") if ref_asset else "zh")),
                str(model_setting(config, matched, "text_lang", "zh")),
                str(model_setting(config, matched, "text_split_method", "cut1")),
                float(model_setting(config, matched, "speed_factor", 1.0)),
                float(model_setting(config, matched, "fragment_interval", 0.3)),
                int(model_setting(config, matched, "top_k", 15)),
                float(model_setting(config, matched, "top_p", 1.0)),
                float(model_setting(config, matched, "temperature", 1.0)),
                f"已根据 GPT weights 自动载入预设: {model_label(matched)}",
            )

        def save_current_model_preset(*values: Any):
            if len(values) != 14:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    f"保存预设需要刷新页面后再点一次；当前旧事件只传入了 {len(values)} 个字段。",
                )
            (
                preset_name_value,
                gpt_weights_value,
                sovits_weights_value,
                reference_id_value,
                ref_audio_upload,
                reference_text_value,
                prompt_lang_value,
                text_lang_value,
                text_split_value,
                speed_value,
                fragment_value,
                top_k_value,
                top_p_value,
                temperature_value,
            ) = values
            try:
                model_label_value, ref_id, message = save_reference_model_preset(
                    config,
                    preset_name_value,
                    gpt_weights_value,
                    sovits_weights_value,
                    reference_id_value,
                    ref_audio_upload,
                    reference_text_value,
                    prompt_lang_value,
                    text_lang_value,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
                current_labels = [model_label(item) for item in config.models]
                current_gpt_choices = file_choices(config, gpt_weight_files(config))
                current_sovits_choices = file_choices(config, sovits_weight_files(config))
                current_ref_choices = reference_choices(config)
                saved_text = read_asset_reference_text(config, reference_by_id(config, ref_id))
                saved_gpt_value = BASE_MODEL_VALUE if is_base_model_choice(gpt_weights_value) else str(resolve_weight_path(config, gpt_weights_value))
                saved_sovits_value = BASE_MODEL_VALUE if is_base_model_choice(sovits_weights_value) else str(resolve_weight_path(config, sovits_weights_value))
                return (
                    gr.update(choices=current_labels, value=model_label_value),
                    gr.update(choices=current_gpt_choices, value=saved_gpt_value),
                    gr.update(choices=current_sovits_choices, value=saved_sovits_value),
                    gr.update(choices=current_ref_choices, value=ref_id),
                    saved_text,
                    message,
                )
            except Exception:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    reference_text_value,
                    traceback.format_exc(),
                )

        def save_current_inference_preset(
            label: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
        ):
            try:
                return update_model_inference_preset(
                    config,
                    label,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
            except Exception:
                return traceback.format_exc()

        def refresh_weight_dropdowns(current_gpt: str, current_sovits: str):
            current_gpt_choices = file_choices(config, gpt_weight_files(config))
            current_sovits_choices = file_choices(config, sovits_weight_files(config))
            gpt_value = (
                BASE_MODEL_VALUE
                if is_base_model_choice(current_gpt)
                else current_gpt
                if current_gpt and Path(str(current_gpt)).exists()
                else first_choice_value(current_gpt_choices)
            )
            sovits_value = (
                BASE_MODEL_VALUE
                if is_base_model_choice(current_sovits)
                else current_sovits
                if current_sovits and Path(str(current_sovits)).exists()
                else first_choice_value(current_sovits_choices)
            )
            return (
                gr.update(choices=current_gpt_choices, value=gpt_value),
                gr.update(choices=current_sovits_choices, value=sovits_value),
                f"模型路径已刷新\nGPT weights: {len(current_gpt_choices)}\nSoVITS weights: {len(current_sovits_choices)}",
            )

        def prepare_job(job_name_value: str, image: Any, audio: Any, srt: Any, text: str):
            try:
                job = ensure_job(config, job_name_value)
                image_path = copy_upload(image, job / "input" / "source_image")
                audio_path = copy_upload(audio, job / "input" / "uploaded_audio")
                srt_path = copy_upload(srt, job / "input" / "uploaded.srt")
                source_path = write_text(job / "input" / "source.txt", text or "")
                srt_text = srt_path.read_text(encoding="utf-8-sig") if srt_path else ""
                message = [
                    "任务已准备",
                    f"Job: {job}",
                    f"Text: {source_path}",
                    f"Image: {image_path or '(未上传)'}",
                    f"Audio: {audio_path or '(将生成 TTS)'}",
                    f"SRT: {srt_path or '(将生成 ASR)'}",
                ]
                editor_srt_text = format_srt_for_editor(config, srt_text) if srt_text else ""
                return (
                    str(job),
                    str(image_path or ""),
                    str(audio_path or ""),
                    "",
                    str(srt_path or ""),
                    text or "",
                    str(audio_path) if audio_path else None,
                    editor_srt_text,
                    "\n".join(message),
                    "",
                    str(job),
                )
            except Exception:
                return "", "", "", "", "", text or "", None, "", traceback.format_exc(), "", ""

        def generate_tts(
            job_value: str,
            label: str,
            text: str,
            gpt_weights_value: str,
            sovits_weights_value: str,
            reference_id_value: str,
            ref_audio_upload: Any,
            reference_text_value: str,
            no_ref_text_value: bool,
            prompt_lang_value: str,
            text_lang_value: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
        ):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                job = Path(job_value)
                selected = model_by_label(config, label)
                text_path = write_text(job / "input" / "source.txt", text or "")
                overrides = build_tts_overrides(
                    config,
                    job,
                    selected,
                    gpt_weights_value,
                    sovits_weights_value,
                    reference_id_value,
                    ref_audio_upload,
                    reference_text_value,
                    bool(no_ref_text_value),
                    prompt_lang_value,
                    text_lang_value,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
                voice_config = merge_voice_config(config, selected, job / "tmp" / "voice_config.json", overrides)
                out_dir = job / "tts"
                audio_path = run_gsv_tts(config, job, text_path, voice_config, out_dir, job / "logs" / "tts.txt")
                return (
                    str(audio_path),
                    str(audio_path),
                    f"TTS 完成\nAudio: {audio_path}\nVoice config: {voice_config}\nLog: {job / 'logs' / 'tts.txt'}",
                )
            except Exception:
                return None, "", traceback.format_exc()

        def generate_tts_repaired(
            job_value: str,
            label: str,
            text: str,
            gpt_weights_value: str,
            sovits_weights_value: str,
            reference_id_value: str,
            ref_audio_upload: Any,
            reference_text_value: str,
            no_ref_text_value: bool,
            prompt_lang_value: str,
            text_lang_value: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
            chunk_chars_value: float,
            retries_value: float,
            pass_cer_value: float,
            pad_ms_value: float,
        ):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白文案。")
                job = Path(job_value)
                selected = model_by_label(config, label)
                source_path = write_text(job / "input" / "source.txt", text or "")
                overrides = build_tts_overrides(
                    config,
                    job,
                    selected,
                    gpt_weights_value,
                    sovits_weights_value,
                    reference_id_value,
                    ref_audio_upload,
                    reference_text_value,
                    bool(no_ref_text_value),
                    prompt_lang_value,
                    text_lang_value,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
                voice_config = merge_voice_config(config, selected, job / "tmp" / "voice_config.segmented.json", overrides)

                chunk_chars = int(chunk_chars_value or 70)
                retries = max(0, min(4, int(retries_value or 0)))
                pass_cer = max(0.02, min(0.30, float(pass_cer_value or 0.14)))
                pad_seconds = 0.0 if indexed_by_locator else max(0.0, min(0.8, float(pad_ms_value or 0.0) / 1000.0))
                chunks = split_tts_chunks(text, chunk_chars)
                if not chunks:
                    raise ValueError("分段结果为空，请检查旁白文案。")

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                segment_root = job / "tts" / "segments"
                accepted_audio: list[Path] = []
                report_lines = [
                    "# Segmented TTS Auto Repair Report",
                    "",
                    f"Source: {source_path}",
                    f"Chunks: {len(chunks)}",
                    f"Max chars: {chunk_chars}",
                    f"Retries: {retries}",
                    f"Pass CER: {pass_cer:.2%}",
                    f"Padding: {pad_seconds:.3f}s",
                    "",
                ]

                for index, chunk in enumerate(chunks, start=1):
                    best_audio, lines, passed = synthesize_best_chunk_audio(
                        config,
                        job,
                        chunk,
                        index,
                        voice_config,
                        asr_model,
                        text_lang_value,
                        retries,
                        pass_cer,
                        segment_root,
                        "tts_segment",
                        allow_micro_repair=True,
                        report_label="Segment",
                    )
                    accepted_audio.append(best_audio)
                    report_lines.extend(lines)
                    report_lines.extend(
                        [
                            f"### Segment {index:03d} accepted",
                            "",
                            f"- Accepted audio: `{best_audio}`",
                            f"- Accepted status: {'PASS' if passed else 'FALLBACK'}",
                            "",
                        ]
                    )

                final_audio = concat_audio_with_padding(
                    config,
                    accepted_audio,
                    job / "tts" / "tts_segmented_auto_repaired.wav",
                    pad_seconds,
                    job / "logs" / "tts_segmented_concat",
                )
                save_audio_sequence(
                    job,
                    chunks,
                    [
                        {"kind": "segment", "chunk_index": index, "text": chunk, "audio": str(audio)}
                        for index, (chunk, audio) in enumerate(zip(chunks, accepted_audio), start=1)
                    ],
                )
                srt_text, raw_srt_text, aligned_to_script = subtitle_srt_from_model(
                    config,
                    final_audio,
                    asr_model,
                    asr_language(text_lang_value),
                    text or "",
                )
                srt_path = write_text(job / "asr" / "subtitles.segmented_auto.srt", srt_text)
                raw_srt_path = write_text(job / "asr" / "subtitles.segmented_auto.raw_asr.srt", raw_srt_text)
                report_path = job / "checks" / "tts_segmented_auto_repair.md"
                report_path.write_text("\n".join(report_lines), encoding="utf-8")
                overall_report = triad_report(text or "", srt_text, raw_srt_text, aligned_to_script)
                failed_hint = "有片段未达标，已采用最佳尝试；建议查看报告。" if "fallback" in "\n".join(report_lines) else "所有片段通过自动回听。"
                message = "\n".join(
                    [
                        "分段 TTS 自动修复完成",
                        failed_hint,
                        overall_report,
                        f"Audio: {final_audio}",
                        f"SRT: {srt_path}",
                        f"Raw ASR SRT: {raw_srt_path}",
                        f"Report: {report_path}",
                    ]
                )
                return str(final_audio), str(final_audio), str(srt_path), format_srt_for_editor(config, srt_text), message
            except Exception:
                return None, "", "", "", traceback.format_exc()

        def make_crop(job_value: str, image_value: str, crop_x_value: float, crop_y_value: float):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not image_value:
                    raise ValueError("请先上传图片。")
                job = Path(job_value)
                output = job / "input" / "crop_preview.png"
                command = [
                    str(config.python_exe),
                    str(config.video_script),
                    "--image",
                    image_value,
                    "--crop-preview-only",
                    "--crop-preview-output",
                    str(output),
                    "--crop-x",
                    str(crop_x_value),
                    "--crop-y",
                    str(crop_y_value),
                    "--ffmpeg",
                    str(config.ffmpeg),
                    "--ffprobe",
                    str(config.ffprobe),
                    "--output-dir",
                    str(job / "render"),
                    "--name",
                    "crop_preview",
                    "--keep-existing",
                ]
                result = run_command(command, config.project_root, job / "logs" / "crop_preview.txt")
                if result.returncode != 0:
                    raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
                return str(output), f"裁切预览完成\nPreview: {output}\nLog: {job / 'logs' / 'crop_preview.txt'}"
            except Exception:
                return None, traceback.format_exc()

        def make_asr(job_value: str, audio_value: str, text: str, text_lang_value: str):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not audio_value:
                    raise ValueError("请先生成或上传旁白音频。")
                job = Path(job_value)
                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                srt_text, raw_srt_text, aligned_to_script = subtitle_srt_from_model(
                    config,
                    Path(audio_value),
                    asr_model,
                    asr_language(text_lang_value),
                    text or "",
                )
                srt_path = write_text(job / "asr" / "subtitles.srt", srt_text)
                raw_srt_path = write_text(job / "asr" / "subtitles.raw_asr.srt", raw_srt_text)
                report = triad_report(text or "", srt_text, raw_srt_text, aligned_to_script) if text else "ASR 完成，未提供原文校对。"
                return str(srt_path), format_srt_for_editor(config, srt_text), report + f"\nSRT: {srt_path}\nRaw ASR SRT: {raw_srt_path}"
            except Exception:
                return "", "", traceback.format_exc()

        def build_manual_patch_audio_index(job_value: str, audio_value: str, text: str, text_lang_value: str):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not audio_value:
                    raise ValueError("请先生成或上传旁白音频。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白原文。")
                job = Path(job_value)
                audio_path = Path(audio_value)
                if not audio_path.exists():
                    raise FileNotFoundError(f"当前音频不存在: {audio_path}")

                units = split_patch_minimal_units(text or "")
                if not units:
                    raise ValueError("没有可建立索引的句号/逗号分段。")

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                segments, char_timeline = transcribe_model_to_segments_and_char_timeline(audio_path, asr_model, asr_language(text_lang_value))
                raw_srt_text = segments_to_srt(segments)
                intervals = patch_unit_intervals_from_timeline(config, audio_path, segments, units, char_timeline)
                intervals = refine_patch_unit_boundaries_with_audio(
                    config,
                    audio_path,
                    intervals,
                    job / "logs" / "manual_patch_boundary_refine",
                )
                indexed_srt = "\n\n".join(
                    make_srt_block(index, float(interval["start"]), float(interval["end"]), str(unit["text"]))
                    for index, (unit, interval) in enumerate(zip(units, intervals), start=1)
                ).strip() + "\n"
                srt_path = write_text(job / "asr" / "subtitles.manual_patch_index.srt", indexed_srt)
                raw_srt_path = write_text(job / "asr" / "subtitles.manual_patch_index.raw_asr.srt", raw_srt_text)

                if len(intervals) != len(units):
                    raise RuntimeError("分段索引数量与字幕时间轴数量不一致。")
                segment_root = job / "tts" / "source_audio_segments"
                entries: list[dict[str, Any]] = []
                for unit, interval in zip(units, intervals):
                    start = float(interval["start"])
                    end = float(interval["end"])
                    segment_audio = cut_audio_segment(
                        config,
                        audio_path,
                        segment_root / f"{int(unit['sequence_index']):03d}_{safe_name(str(unit['locator']))}.wav",
                        start,
                        end,
                        job / "logs" / f"manual_patch_cut_{int(unit['sequence_index']):03d}.txt",
                    )
                    entries.append(
                        {
                            "kind": "source_segment",
                            "chunk_index": int(unit["sequence_index"]),
                            "sentence_index": int(unit["sentence_index"]),
                            "clause_index": int(unit["clause_index"]),
                            "locator": str(unit["locator"]),
                            "text": str(unit["text"]),
                            "target_norm": normalize_text(str(unit["text"])),
                            "source_audio": str(audio_path),
                            "start": start,
                            "end": end,
                            "raw_start": interval.get("raw_start"),
                            "raw_end": interval.get("raw_end"),
                            "boundary_source": str(interval.get("boundary_source") or ""),
                            "refined_with_audio": bool(interval.get("refined_with_audio")),
                            "refine_error": str(interval.get("refine_error") or ""),
                            "alignment_source": str(interval.get("source") or ""),
                            "alignment_confidence": float(interval.get("confidence") or 0.0),
                            "audio": str(segment_audio),
                        }
                    )

                chunks = [str(unit["text"]) for unit in units]
                sequence_path = save_audio_sequence(job, chunks, entries)
                listing = manual_patch_listing(text or "", 0)
                report_path = job / "checks" / "manual_patch_audio_index.md"
                report_path.write_text(
                    "\n".join(
                        [
                            "# Manual Patch Audio Index",
                            "",
                            f"- Source audio: `{audio_path}`",
                            f"- Segments: {len(entries)}",
                            f"- SRT: `{srt_path}`",
                            f"- Raw ASR SRT: `{raw_srt_path}`",
                            f"- Sequence: `{sequence_path}`",
                            f"- Word timestamp chars: {len(char_timeline)}",
                            "",
                            "## Locator Map",
                            "",
                            *[
                                f"- {entry['locator']} [{entry['start']:.3f}-{entry['end']:.3f}s, "
                                f"{entry['alignment_source']}, boundary={entry.get('boundary_source') or '-'}, "
                                f"confidence={entry['alignment_confidence']:.2f}]: "
                                f"`{entry['text']}` -> `{entry['audio']}`"
                                for entry in entries
                            ],
                        ]
                    ),
                    encoding="utf-8",
                )
                message = "\n".join(
                    [
                        "已从当前整段音频建立手动修补分段索引",
                        "本步骤没有重新 TTS；只做一次 ASR 时间轴和本地音频切片。",
                        "现在可以在“单句补漏 / 替换试听”的目标编号里输入 3、3.2 或 3.1,3.2。",
                        f"Segments: {len(entries)}",
                        f"SRT: {srt_path}",
                        f"Raw ASR SRT: {raw_srt_path}",
                        f"Sequence: {sequence_path}",
                        f"Word timestamp chars: {len(char_timeline)}",
                        f"Report: {report_path}",
                    ]
                )
                return str(audio_path), str(audio_path), str(srt_path), format_srt_for_editor(config, indexed_srt), listing, message
            except Exception:
                return None, audio_value or "", "", "", traceback.format_exc(), traceback.format_exc()

        def retime_srt_from_editor(job_value: str, audio_value: str, srt_text: str, text: str, text_lang_value: str):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not audio_value:
                    raise ValueError("请先生成或上传旁白音频。")
                blocks, subtitle_source_mode = final_subtitle_blocks_from_source_or_editor(config, text or "", srt_text)
                if not blocks:
                    raise ValueError("SRT 校对编辑器为空，或没有可识别的字幕文本。")
                job = Path(job_value)
                audio_path = Path(audio_value)

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                segments = transcribe_model_to_segments(audio_path, asr_model, asr_language(text_lang_value))
                raw_srt_text = segments_to_srt(segments)
                retimed_srt = script_aligned_srt_from_blocks(config, audio_path, segments, blocks)
                srt_path = write_text(job / "asr" / "subtitles.editor_retimed.srt", retimed_srt)
                raw_srt_path = write_text(job / "asr" / "subtitles.editor_retimed.raw_asr.srt", raw_srt_text)

                editor_plain = subtitle_blocks_to_plain_text(blocks)
                report = triad_report(editor_plain, retimed_srt, raw_srt_text, True)
                if subtitle_source_mode == "source_text":
                    report = "字幕文字已按旁白原文重建；ASR 只用于时间轴和审核。\n\n" + report
                if text and normalize_text(text) != normalize_text(editor_plain):
                    report += "\n\nEditor subtitle vs original script:\n" + local_srt_report(text, retimed_srt)
                return str(srt_path), format_srt_for_editor(config, retimed_srt), report + f"\nSRT: {srt_path}\nRaw ASR SRT: {raw_srt_path}"
            except Exception:
                return "", srt_text or "", traceback.format_exc()

        def check_audio_or_srt(job_value: str, audio_value: str, text: str, srt_text: str, text_lang_value: str):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                job = Path(job_value)
                messages = []
                if srt_text:
                    srt_path = write_text(job / "asr" / "subtitles.edited.srt", srt_text)
                    raw_srt_path = latest_raw_asr_srt(job)
                    if text and raw_srt_path:
                        raw_srt_text = raw_srt_path.read_text(encoding="utf-8-sig")
                        messages.append("Audio/raw ASR vs script:")
                        messages.append(local_srt_report(text or "", raw_srt_text))
                        messages.append("Final subtitle vs script:")
                    messages.append(local_srt_report(text or "", srt_text))
                    messages.append(f"Edited SRT: {srt_path}")
                    if text and raw_srt_path:
                        messages.append(f"Raw ASR SRT: {raw_srt_path}")
                if audio_value and text:
                    text_path = write_text(job / "input" / "source.txt", text)
                    report_path = job / "checks" / "tts_match_report.md"
                    if text_lang_value in {"zh", "yue"}:
                        command = [
                            str(config.python_exe),
                            str(config.tts_checker_script),
                            "--audio",
                            audio_value,
                            "--text-file",
                            str(text_path),
                            "--model",
                            str(config.asr_model),
                            "--json",
                            "--report",
                            str(report_path),
                        ]
                        command.extend(["--language", text_lang_value])
                        checker_timeout = max(60, config_int(config, "checker_timeout_seconds", 900))
                        result = run_command(
                            command,
                            config.project_root,
                            job / "logs" / "tts_check.txt",
                            timeout=checker_timeout,
                        )
                        messages.append("TTS checker return code: " + str(result.returncode))
                        if result.returncode == 124:
                            messages.append(f"TTS checker timed out after {checker_timeout} seconds; see log for partial output.")
                        messages.append("Report: " + str(report_path))
                        if result.stdout.strip():
                            messages.append(result.stdout[-2000:])
                        if result.stderr.strip():
                            messages.append(result.stderr[-2000:])
                    else:
                        messages.append("TTS checker 当前只对中文/粤语做细粒度报告；其他语种请优先使用 Faster-Whisper 字幕结果校对。")
                if not messages:
                    messages.append("没有可校对内容：需要音频+原文，或 SRT+原文。")
                return "\n\n".join(messages)
            except Exception:
                return traceback.format_exc()

        def secondary_review(
            job_value: str,
            audio_value: str,
            text: str,
            srt_text: str,
            text_lang_value: str,
            mode_value: str,
            api_url_value: str,
            agent_model_value: str,
            agent_api_key_value: str,
        ):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白原文。")
                job = Path(job_value)
                current_srt = srt_text or ""
                review_srt = ""
                raw_srt_path: Path | None = None
                if audio_value:
                    from faster_whisper import WhisperModel  # type: ignore

                    asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                    audio_path = Path(audio_value)
                    segments = transcribe_model_to_segments(audio_path, asr_model, asr_language(text_lang_value))
                    review_srt = segments_to_srt(segments)
                    if (text or "").strip():
                        current_srt, subtitle_source_mode = srt_from_final_subtitle_text(
                            config,
                            audio_path,
                            text or "",
                            current_srt,
                            segments,
                        )
                    elif not current_srt.strip():
                        current_srt = review_srt
                        subtitle_source_mode = "raw_asr"
                    else:
                        subtitle_source_mode = "editor"
                    srt_path = write_text(job / "asr" / "subtitles.secondary_review.srt", current_srt)
                    raw_srt_path = write_text(job / "asr" / "subtitles.secondary_review.raw_asr.srt", review_srt)
                else:
                    if not current_srt.strip():
                        raise ValueError("二次审核需要音频，或至少需要 SRT。")
                    srt_path = write_text(job / "asr" / "subtitles.secondary_review.srt", current_srt)
                    subtitle_source_mode = "editor"
                    raw_srt_path = latest_raw_asr_srt(job)
                    if raw_srt_path:
                        review_srt = raw_srt_path.read_text(encoding="utf-8-sig")
                    else:
                        review_srt = current_srt

                summary, local_report = local_second_review(text or "", review_srt)
                local_report_path = job / "checks" / "tts_secondary_review.md"
                local_report_path.write_text(local_report, encoding="utf-8")

                message_parts = [
                    summary,
                    f"SRT: {srt_path}",
                    f"Final subtitle text source: {subtitle_source_mode}",
                    f"Review source: {'raw ASR' if raw_srt_path else 'current SRT'}",
                    f"Local report: {local_report_path}",
                ]
                if raw_srt_path:
                    message_parts.append(f"Raw ASR SRT: {raw_srt_path}")
                final_report = local_report
                if mode_value == "chat":
                    agent_text = call_agent_review(
                        config,
                        text or "",
                        review_srt,
                        local_report,
                        api_url_value,
                        agent_model_value,
                        agent_api_key_value,
                    )
                    agent_report_path = job / "checks" / "tts_agent_chat_review.md"
                    agent_report_path.write_text(agent_text, encoding="utf-8")
                    message_parts.extend(["Chat API review: completed", f"Agent report: {agent_report_path}"])
                    final_report = local_report + "\n\n# Chat API Review\n\n" + agent_text.strip() + "\n"
                else:
                    message_parts.append("Chat API review: skipped")

                return "\n".join(message_parts), str(srt_path), format_srt_for_editor(config, current_srt), final_report
            except Exception:
                return traceback.format_exc(), "", srt_text or "", ""

        def list_manual_patch_options(text: str, chunk_chars_value: float):
            try:
                if not (text or "").strip():
                    raise ValueError("请先输入旁白文案。")
                chunk_chars = int(chunk_chars_value or 70)
                return manual_patch_listing(text or "", chunk_chars)
            except Exception:
                return traceback.format_exc()

        def sentence_patch_target_to_text(text: str, target_hint: str):
            raw = (target_hint or "").strip()
            if not raw:
                return gr.update(), "目标编号为空；可直接填写单句文本，或输入 3 / 3.2 / 3.1,3.2。"
            try:
                chunks = split_patch_sentence_units(text or "")
                if not chunks:
                    raise ValueError("请先输入旁白文案。")
                targets = parse_patch_target_tokens(raw, len(chunks))
                target = patch_target_from_tokens(chunks, targets, "")
                matched_text = str(target.get("source_target_text") or target.get("target_text") or "").strip()
                locator = str(target.get("target_locator") or "") or patch_locator_text(
                    int(target["chunk_index"]),
                    [int(index) for index in target.get("clause_indices") or []],
                    int(target["clause_index"]) if target.get("clause_index") else None,
                )
                return matched_text, f"已匹配目标 {locator}\nText: {matched_text}"
            except Exception as exc:
                return gr.update(), f"目标编号未匹配：{exc}"

        def secondary_auto_patch(
            job_value: str,
            audio_value: str,
            text: str,
            srt_text: str,
            manual_patch_value: str,
            label: str,
            gpt_weights_value: str,
            sovits_weights_value: str,
            reference_id_value: str,
            ref_audio_upload: Any,
            reference_text_value: str,
            no_ref_text_value: bool,
            prompt_lang_value: str,
            text_lang_value: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
            chunk_chars_value: float,
            retries_value: float,
            pass_cer_value: float,
            pad_ms_value: float,
        ):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白原文。")
                job = Path(job_value)
                selected = model_by_label(config, label)
                source_path = write_text(job / "input" / "source.txt", text or "")
                manual_patch_value = (manual_patch_value or "").strip()
                chunk_chars = int(chunk_chars_value or 70)
                retries = max(0, min(4, int(retries_value or 0)))
                pass_cer = max(0.02, min(0.30, float(pass_cer_value or 0.14)))
                pad_seconds = max(0.0, min(0.8, float(pad_ms_value or 0.0) / 1000.0))

                current_srt = srt_text or ""
                review_srt = ""
                if audio_value:
                    from faster_whisper import WhisperModel  # type: ignore

                    asr_model_for_input = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                    generated_srt, review_srt, _aligned_to_script = subtitle_srt_from_model(
                        config,
                        Path(audio_value),
                        asr_model_for_input,
                        asr_language(text_lang_value),
                        text or "",
                    )
                    if not current_srt.strip():
                        current_srt = generated_srt
                    write_text(job / "asr" / "subtitles.before_secondary_patch.srt", current_srt)
                    write_text(job / "asr" / "subtitles.before_secondary_patch.raw_asr.srt", review_srt)
                else:
                    if not current_srt.strip():
                        raise ValueError("自动补漏句需要当前音频，或至少需要 SRT。")
                    raw_srt_path = latest_raw_asr_srt(job)
                    if raw_srt_path:
                        review_srt = raw_srt_path.read_text(encoding="utf-8-sig")
                    else:
                        review_srt = current_srt

                segment_entries = parse_segmented_repair_segments(job)
                segment_rebuild = False
                segment_audio_by_index: dict[int, Path] = {}
                fresh_chunks = split_patch_sentence_units(text) if manual_patch_value else split_tts_chunks(text, chunk_chars)
                if segment_entries:
                    report_chunks = [str(entry["expected"]) for entry in segment_entries]
                    chunks_match_current_split = (
                        len(report_chunks) == len(fresh_chunks)
                        and all(normalize_text(left) == normalize_text(right) for left, right in zip(report_chunks, fresh_chunks))
                    )
                    if chunks_match_current_split:
                        chunks = report_chunks
                        segment_audio_by_index = {int(entry["index"]): Path(entry["audio"]) for entry in segment_entries}
                        segment_rebuild = len(segment_audio_by_index) == len(chunks)
                    else:
                        chunks = fresh_chunks
                else:
                    chunks = fresh_chunks
                if not chunks:
                    raise ValueError("分段结果为空，请检查旁白文案。")

                rows = chunk_review_rows(chunks, review_srt)
                selection_mode = "manual" if manual_patch_value else "auto"
                if manual_patch_value:
                    patch_rows, patch_clause_rows_by_index = parse_manual_patch_targets(manual_patch_value, chunks)
                else:
                    patch_rows_by_index = {int(row["index"]): row for row in rows_requiring_audio_patch(rows)}
                    patch_clause_rows_by_index: dict[int, list[dict[str, Any]]] = {}
                    for chunk_index, chunk in enumerate(chunks, start=1):
                        clauses = split_patch_clause_units(chunk)
                        if len(clauses) <= 1:
                            continue
                        clause_rows = chunk_review_rows(clauses, review_srt)
                        bad_clause_rows = rows_requiring_audio_patch(clause_rows)
                        if bad_clause_rows:
                            patch_clause_rows_by_index[chunk_index] = bad_clause_rows
                            if chunk_index not in patch_rows_by_index:
                                clause_row = dict(rows[chunk_index - 1])
                                clause_row["status"] = "CLAUSE"
                                patch_rows_by_index[chunk_index] = clause_row
                    patch_rows = [patch_rows_by_index[index] for index in sorted(patch_rows_by_index)]
                if not patch_rows:
                    summary, final_report = local_second_review(text or "", review_srt)
                    checked_srt_path = write_text(job / "asr" / "subtitles.secondary_auto_patch.checked.srt", current_srt)
                    return (
                        audio_value or None,
                        audio_value or "",
                        str(checked_srt_path),
                        format_srt_for_editor(config, current_srt),
                        final_report,
                        summary + "\n未发现需要自动补句的 MISSING/强 WEAK 片段。审核依据：raw ASR 优先，若不存在则使用当前 SRT。",
                    )

                max_patch_chunks = max(1, config_int(config, "max_auto_patch_chunks", 8))
                if len(patch_rows) > max_patch_chunks:
                    raise ValueError(
                        f"需要补的片段有 {len(patch_rows)} 个，超过上限 {max_patch_chunks}；"
                        "建议先重新跑 2b 分段 TTS 自动修复。"
                    )

                overrides = build_tts_overrides(
                    config,
                    job,
                    selected,
                    gpt_weights_value,
                    sovits_weights_value,
                    reference_id_value,
                    ref_audio_upload,
                    reference_text_value,
                    bool(no_ref_text_value),
                    prompt_lang_value,
                    text_lang_value,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
                voice_config = merge_voice_config(config, selected, job / "tmp" / "voice_config.secondary_patch.json", overrides)

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                patch_root = job / "tts" / "secondary_patch"
                patch_audio_by_index: dict[int, Path] = {}
                patch_report_lines = [
                    "# Secondary Auto Patch Report",
                    "",
                    f"Source: {source_path}",
                    f"Mode: {'rebuild segmented audio' if segment_rebuild else 'blocked: no segment map'}",
                    f"Selection: {selection_mode}",
                    f"Manual targets: {manual_patch_value or '-'}",
                    f"Chunks: {len(chunks)}",
                    f"Patch chunks: {len(patch_rows)}",
                    f"Retries: {retries}",
                    f"Pass CER: {pass_cer:.2%}",
                    f"Padding: {pad_seconds:.3f}s",
                    "",
                    "## Selected Chunks",
                    "",
                ]
                for row in patch_rows:
                    patch_report_lines.extend(
                        [
                            f"- Chunk {row['index']:03d} {row['status']} "
                            f"(score={row['score']:.2f}, coverage={row['coverage']:.2f}): `{row['expected']}`",
                        ]
                    )
                    clause_rows = patch_clause_rows_by_index.get(int(row["index"]), [])
                    for clause_row in clause_rows:
                        patch_report_lines.append(
                            f"  - Clause {clause_row['index']:02d} {clause_row['status']} "
                            f"(score={clause_row['score']:.2f}, coverage={clause_row['coverage']:.2f}): "
                            f"`{clause_row['expected']}`"
                        )
                patch_report_lines.append("")

                if not segment_rebuild:
                    raise ValueError(
                        "检测到漏句，但当前任务没有可按原文顺序重建的完整分段音频映射，已停止补漏，避免把补句追加到结尾。"
                        "请先重新运行“2b. 分段 TTS 自动修复”，再运行“自动补漏句”。"
                    )

                for row in patch_rows:
                    index = int(row["index"])
                    audio_path, lines, _passed = synthesize_clause_rebuilt_segment_audio(
                        config,
                        job,
                        str(row["expected"]),
                        index,
                        voice_config,
                        asr_model,
                        text_lang_value,
                        retries,
                        pass_cer,
                        patch_root,
                        "tts_secondary_patch",
                        focus_rows=patch_clause_rows_by_index.get(index),
                        report_label="Patch Chunk",
                    )
                    patch_audio_by_index[index] = audio_path
                    patch_report_lines.extend(lines)

                if segment_rebuild:
                    ordered_audio = [
                        patch_audio_by_index.get(index) or segment_audio_by_index[index]
                        for index in range(1, len(chunks) + 1)
                    ]
                else:
                    raise AssertionError("unreachable: segment_rebuild must be true")

                final_audio = concat_audio_with_padding(
                    config,
                    ordered_audio,
                    job / "tts" / "tts_secondary_auto_patched.wav",
                    pad_seconds,
                    job / "logs" / "tts_secondary_patch_concat",
                )
                save_audio_sequence(
                    job,
                    chunks,
                    [
                        {
                            "kind": "patch" if index in patch_audio_by_index else "segment",
                            "chunk_index": index,
                            "text": chunks[index - 1],
                            "audio": str(patch_audio_by_index.get(index) or segment_audio_by_index[index]),
                        }
                        for index in range(1, len(chunks) + 1)
                    ],
                )
                patched_srt, raw_srt_text, aligned_to_script, subtitle_source_mode = subtitle_srt_after_audio_edit(
                    config,
                    final_audio,
                    asr_model,
                    asr_language(text_lang_value),
                    text or "",
                    current_srt,
                )
                srt_path = write_text(job / "asr" / "subtitles.secondary_auto_patch.srt", patched_srt)
                raw_srt_path = write_text(job / "asr" / "subtitles.secondary_auto_patch.raw_asr.srt", raw_srt_text)
                summary, final_review = local_second_review(text or "", raw_srt_text)
                patch_report_lines.extend(["# Post Patch Review", "", final_review])
                report_path = job / "checks" / "tts_secondary_auto_patch.md"
                report_path.write_text("\n".join(patch_report_lines), encoding="utf-8")

                message = "\n".join(
                    [
                        "手动补修完成" if selection_mode == "manual" else "自动补漏句完成",
                        "Patch mode: 按分段顺序重建音频；问题分段按逗号小句逐条生成后替换",
                        summary,
                        triad_report(text or "", patched_srt, raw_srt_text, aligned_to_script),
                        f"Subtitle text source: {subtitle_source_mode}",
                        f"Audio: {final_audio}",
                        f"SRT: {srt_path}",
                        f"Raw ASR SRT: {raw_srt_path}",
                        f"Report: {report_path}",
                    ]
                )
                return str(final_audio), str(final_audio), str(srt_path), format_srt_for_editor(config, patched_srt), final_review, message
            except Exception:
                return None, audio_value or "", "", srt_text or "", "", traceback.format_exc()

        def generate_tts_repaired_then_auto_patch(
            job_value: str,
            label: str,
            text: str,
            gpt_weights_value: str,
            sovits_weights_value: str,
            reference_id_value: str,
            ref_audio_upload: Any,
            reference_text_value: str,
            no_ref_text_value: bool,
            prompt_lang_value: str,
            text_lang_value: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
            chunk_chars_value: float,
            retries_value: float,
            pass_cer_value: float,
            pad_ms_value: float,
        ):
            repaired_audio, repaired_state, repaired_srt_state, repaired_editor, repair_message = generate_tts_repaired(
                job_value,
                label,
                text,
                gpt_weights_value,
                sovits_weights_value,
                reference_id_value,
                ref_audio_upload,
                reference_text_value,
                no_ref_text_value,
                prompt_lang_value,
                text_lang_value,
                text_split_value,
                speed_value,
                fragment_value,
                top_k_value,
                top_p_value,
                temperature_value,
                chunk_chars_value,
                retries_value,
                pass_cer_value,
                pad_ms_value,
            )
            if not repaired_state:
                return repaired_audio, repaired_state, repaired_srt_state, repaired_editor, repair_message

            patched_audio, patched_state, patched_srt_state, patched_editor, final_review, patch_message = secondary_auto_patch(
                job_value,
                repaired_state,
                text,
                repaired_editor,
                "",
                label,
                gpt_weights_value,
                sovits_weights_value,
                reference_id_value,
                ref_audio_upload,
                reference_text_value,
                no_ref_text_value,
                prompt_lang_value,
                text_lang_value,
                text_split_value,
                speed_value,
                fragment_value,
                top_k_value,
                top_p_value,
                temperature_value,
                chunk_chars_value,
                retries_value,
                pass_cer_value,
                pad_ms_value,
            )
            if not patched_state:
                return (
                    repaired_audio,
                    repaired_state,
                    repaired_srt_state,
                    repaired_editor,
                    "\n\n".join(["分段 TTS 已完成，但自动补漏阶段未完成。", repair_message, patch_message]),
                )
            return (
                patched_audio,
                patched_state,
                patched_srt_state,
                patched_editor,
                "\n\n".join(["分段 TTS + 自动补漏完成", repair_message, patch_message]),
            )

        def subtitle_retime_and_review(
            job_value: str,
            audio_value: str,
            text: str,
            srt_text: str,
            text_lang_value: str,
            mode_value: str,
            api_url_value: str,
            agent_model_value: str,
            agent_api_key_value: str,
        ):
            try:
                if not (srt_text or "").strip():
                    srt_path, editor_text, first_message = make_asr(job_value, audio_value, text, text_lang_value)
                    first_step = "4. Faster-Whisper 字幕"
                else:
                    srt_path, editor_text, first_message = retime_srt_from_editor(job_value, audio_value, srt_text, text, text_lang_value)
                    first_step = "4b. 按编辑器重排时间轴"
                if not srt_path:
                    return "", editor_text or srt_text or "", first_message, ""
                if not (text or "").strip():
                    return srt_path, editor_text, first_message + "\n未提供原文，已跳过二次审核。", ""

                review_status, reviewed_srt_path, reviewed_editor, final_report = secondary_review(
                    job_value,
                    audio_value,
                    text,
                    editor_text,
                    text_lang_value,
                    mode_value,
                    api_url_value,
                    agent_model_value,
                    agent_api_key_value,
                )
                output_srt = reviewed_srt_path or srt_path
                output_editor = reviewed_editor or editor_text
                message = "\n\n".join([first_step + " 完成", first_message, "5. 审核完成", review_status])
                return output_srt, output_editor, message, final_report
            except Exception:
                return "", srt_text or "", traceback.format_exc(), ""

        def generate_sentence_patch_candidate(
            job_value: str,
            audio_value: str,
            text: str,
            srt_text: str,
            patch_text: str,
            target_hint: str,
            label: str,
            gpt_weights_value: str,
            sovits_weights_value: str,
            reference_id_value: str,
            ref_audio_upload: Any,
            reference_text_value: str,
            no_ref_text_value: bool,
            prompt_lang_value: str,
            text_lang_value: str,
            text_split_value: str,
            speed_value: float,
            fragment_value: float,
            top_k_value: float,
            top_p_value: float,
            temperature_value: float,
            chunk_chars_value: float,
            retries_value: float,
            pass_cer_value: float,
        ):
            try:
                if not job_value:
                    raise ValueError("请先准备任务。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白原文。")
                job = Path(job_value)
                selected = model_by_label(config, label)
                chunk_chars = int(chunk_chars_value or 70)
                retries = max(0, min(4, int(retries_value or 0)))
                pass_cer = max(0.02, min(0.30, float(pass_cer_value or 0.14)))

                review_srt = srt_text or ""
                raw_srt_path = latest_raw_asr_srt(job)
                if raw_srt_path:
                    review_srt = raw_srt_path.read_text(encoding="utf-8-sig")
                target = resolve_sentence_patch_target(text or "", review_srt, patch_text or "", target_hint or "", chunk_chars)
                target_text = str(target["target_text"]).strip()
                if not normalize_text(target_text):
                    raise ValueError("单句文本为空。")

                overrides = build_tts_overrides(
                    config,
                    job,
                    selected,
                    gpt_weights_value,
                    sovits_weights_value,
                    reference_id_value,
                    ref_audio_upload,
                    reference_text_value,
                    bool(no_ref_text_value),
                    prompt_lang_value,
                    text_lang_value,
                    text_split_value,
                    speed_value,
                    fragment_value,
                    top_k_value,
                    top_p_value,
                    temperature_value,
                )
                voice_config = merge_voice_config(config, selected, job / "tmp" / "voice_config.sentence_patch.json", overrides)

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                patch_root = job / "tts" / "sentence_patch" / time.strftime("%Y%m%d_%H%M%S")
                audio_path, lines, passed = synthesize_best_chunk_audio(
                    config,
                    job,
                    target_text,
                    int(target["chunk_index"]),
                    voice_config,
                    asr_model,
                    text_lang_value,
                    retries,
                    pass_cer,
                    patch_root,
                    "sentence_patch",
                    allow_micro_repair=True,
                    report_label="Sentence Patch Candidate",
                )
                report_path = patch_root / "candidate_report.md"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text("\n".join(lines), encoding="utf-8")

                candidate = {
                    "audio": str(audio_path),
                    "target_text": target_text,
                    "chunk_index": int(target["chunk_index"]),
                    "clause_index": target.get("clause_index"),
                    "clause_indices": target.get("clause_indices") or [],
                    "target_units": target.get("target_units") or [],
                    "target_locator": target.get("target_locator") or "",
                    "chunks": target.get("chunks") or [],
                    "source_chunk": target.get("source_chunk"),
                    "source_target_text": target.get("source_target_text"),
                    "action": target.get("action"),
                    "action_reason": target.get("action_reason"),
                    "missing": bool(target.get("missing")),
                    "srt_score": float(target.get("srt_score") or 0.0),
                    "srt_coverage": float(target.get("srt_coverage") or 0.0),
                    "voice_config": str(voice_config),
                    "retries": retries,
                    "pass_cer": pass_cer,
                    "text_lang": text_lang_value,
                    "report": str(report_path),
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                export_audio, export_meta = export_sentence_patch_audio_file(job, candidate, audio_path)
                candidate["source_audio"] = str(audio_path)
                candidate["audio"] = str(export_audio)
                candidate["export_meta"] = str(export_meta)
                state_path = job / "tmp" / "sentence_patch_candidate.json"
                state_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
                action_text = "仅无目标编号时插入漏句" if candidate["action"] == "insert" else "替换原音频片段"
                locator = str(candidate.get("target_locator") or "") or patch_locator_text(
                    int(candidate["chunk_index"]),
                    [int(index) for index in candidate.get("clause_indices") or []],
                    int(candidate["clause_index"]) if candidate.get("clause_index") else None,
                )
                message = "\n".join(
                    [
                        "单句试听已生成",
                        f"Action: {action_text}",
                        f"Reason: {candidate.get('action_reason')}",
                        f"Target: {locator}",
                        f"Text: {target_text}",
                        f"ASR score: {candidate['srt_score']:.2f}, coverage: {candidate['srt_coverage']:.2f}",
                        f"Candidate passed: {passed}",
                        f"Audio: {export_audio}",
                        f"Source audio: {audio_path}",
                        f"PR meta: {export_meta}",
                        f"Report: {report_path}",
                    ]
                )
                return json.dumps(candidate, ensure_ascii=False), str(export_audio), str(export_audio), message
            except Exception:
                return "", None, None, traceback.format_exc()

        def export_sentence_patch_candidate(job_value: str, candidate_state: str, audio_value: str):
            try:
                if not job_value:
                    raise ValueError("请先准备任务。")
                job = Path(job_value)
                candidate: dict[str, Any] = {}
                if candidate_state:
                    candidate = json.loads(candidate_state)
                else:
                    state_path = job / "tmp" / "sentence_patch_candidate.json"
                    if state_path.exists():
                        candidate = json.loads(state_path.read_text(encoding="utf-8-sig"))

                candidate_audio = Path(str(candidate.get("audio") or audio_value or "")).expanduser()
                if not candidate_audio.exists():
                    raise FileNotFoundError(f"单句候选音频不存在: {candidate_audio}")

                chunk_index = candidate.get("chunk_index")
                clause_index = candidate.get("clause_index")
                locator = str(candidate.get("target_locator") or "") or str(chunk_index or "manual")
                if clause_index and not candidate.get("target_locator"):
                    locator += f".{clause_index}"
                action = "insert" if candidate.get("action") == "insert" else "replace"
                target_text = str(candidate.get("target_text") or "").strip()
                text_slug = safe_name(target_text[:24] or candidate_audio.stem)
                suffix = candidate_audio.suffix or ".wav"
                export_dir = job / "exports" / "premiere_patch"
                export_dir.mkdir(parents=True, exist_ok=True)
                base_name = safe_name(f"{now_slug()}_{action}_{locator}_{text_slug}")
                export_audio = export_dir / f"{base_name}{suffix}"
                shutil.copy2(candidate_audio, export_audio)

                meta_path = export_dir / f"{base_name}.txt"
                meta_lines = [
                    f"audio: {export_audio}",
                    f"source_audio: {candidate_audio}",
                    f"action: {action}",
                    f"target: {locator}",
                    f"text: {target_text}",
                    f"report: {candidate.get('report') or ''}",
                    f"created_at: {now_slug()}",
                ]
                meta_path.write_text("\n".join(meta_lines), encoding="utf-8")

                message = "\n".join(
                    [
                        "单句音频已导出给 PR 手动修音轨",
                        f"Audio: {export_audio}",
                        f"Meta: {meta_path}",
                    ]
                )
                return str(export_audio), str(export_audio), message
            except Exception:
                return audio_value or None, "", traceback.format_exc()

        def apply_sentence_patch_candidate(
            job_value: str,
            audio_value: str,
            text: str,
            srt_text: str,
            candidate_state: str,
            text_lang_value: str,
            chunk_chars_value: float,
            pad_ms_value: float,
        ):
            try:
                if not job_value:
                    raise ValueError("请先准备任务。")
                if not (text or "").strip():
                    raise ValueError("请先输入旁白原文。")
                if not (candidate_state or "").strip():
                    raise ValueError("请先点击 6a 生成并试听单句候选。")
                job = Path(job_value)
                candidate = json.loads(candidate_state)
                candidate_audio = Path(str(candidate.get("audio") or ""))
                if not candidate_audio.exists():
                    raise FileNotFoundError(f"单句候选音频不存在: {candidate_audio}")

                chunk_chars = int(chunk_chars_value or 70)
                sequence_chunks, entries = load_audio_sequence_for_patch(job, text or "", chunk_chars)
                patch_chunk_index = int(candidate.get("chunk_index") or 0)
                if patch_chunk_index < 1:
                    raise ValueError("候选状态缺少有效目标分段。")
                target_text = str(candidate.get("target_text") or "")
                target_norm = normalize_text(target_text)
                action = str(candidate.get("action") or "replace")
                final_patch_audio = candidate_audio
                replacement_text = target_text
                source_target_text = str(candidate.get("source_target_text") or target_text)
                source_target_norm = normalize_text(source_target_text)
                source_chunk_norm = normalize_text(str(candidate.get("source_chunk") or ""))
                sequence_chunk_index = patch_chunk_index
                sequence_chunk_text = sequence_chunks[sequence_chunk_index - 1] if 1 <= sequence_chunk_index <= len(sequence_chunks) else ""
                indexed_by_locator = any("sentence_index" in entry and "clause_index" in entry for entry in entries)
                direct_splice_audio: Path | None = None
                splice_report_path: Path | None = None

                if indexed_by_locator:
                    target_units = [
                        unit
                        for unit in (candidate.get("target_units") or [])
                        if isinstance(unit, dict)
                    ]
                    target_unit_keys = {
                        (int(unit.get("sentence_index") or 0), int(unit.get("clause_index") or 0))
                        for unit in target_units
                    }
                    clause_indices = [
                        int(index)
                        for index in (candidate.get("clause_indices") or [])
                        if str(index).strip()
                    ]
                    if not clause_indices and candidate.get("clause_index"):
                        clause_indices = [int(candidate["clause_index"])]

                    target_entry_indexes: list[int] = []
                    for entry_index, entry in enumerate(entries):
                        if entry.get("kind") == "insert":
                            continue
                        if target_unit_keys:
                            key = (int(entry.get("sentence_index") or 0), int(entry.get("clause_index") or 0))
                            if key in target_unit_keys:
                                target_entry_indexes.append(entry_index)
                            continue
                        if int(entry.get("sentence_index") or 0) != patch_chunk_index:
                            continue
                        if clause_indices and int(entry.get("clause_index") or 0) not in clause_indices:
                            continue
                        target_entry_indexes.append(entry_index)

                    if not target_entry_indexes:
                        for entry_index, entry in enumerate(entries):
                            if entry.get("kind") == "insert":
                                continue
                            entry_norm = normalize_text(str(entry.get("text") or ""))
                            if (
                                (source_target_norm and (source_target_norm in entry_norm or entry_norm in source_target_norm))
                                or (target_norm and (target_norm in entry_norm or entry_norm in target_norm))
                            ):
                                target_entry_indexes.append(entry_index)

                    if target_entry_indexes:
                        first_target = entries[target_entry_indexes[0]]
                        sequence_chunk_index = int(first_target.get("chunk_index") or sequence_chunk_index)
                        sequence_chunk_text = str(first_target.get("text") or sequence_chunk_text)
                else:
                    for entry in entries:
                        if entry.get("kind") == "insert":
                            continue
                        entry_text = str(entry.get("text") or "")
                        entry_norm = normalize_text(entry_text)
                        if (
                            (source_target_norm and source_target_norm in entry_norm)
                            or (source_chunk_norm and source_chunk_norm in entry_norm)
                        ):
                            sequence_chunk_index = int(entry.get("chunk_index") or sequence_chunk_index)
                            sequence_chunk_text = entry_text
                            break

                replace_clause_indices = find_clause_indices_for_text(sequence_chunk_text, source_target_text)
                if action == "replace" and replace_clause_indices and not indexed_by_locator:
                    from faster_whisper import WhisperModel  # type: ignore

                    asr_model_for_rebuild = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                    final_patch_audio = rebuild_chunk_with_clause_candidate(
                        config,
                        job,
                        sequence_chunk_text,
                        sequence_chunk_index,
                        replace_clause_indices,
                        candidate_audio,
                        Path(str(candidate["voice_config"])),
                        asr_model_for_rebuild,
                        str(candidate.get("text_lang") or text_lang_value),
                        int(candidate.get("retries") or 0),
                        float(candidate.get("pass_cer") or 0.14),
                        job / "tts" / "sentence_patch" / "applied",
                    )
                    replacement_text = sequence_chunk_text

                if indexed_by_locator:
                    duplicate_key = (patch_chunk_index, target_norm)
                    entries = [
                        entry
                        for entry in entries
                        if not (
                            entry.get("kind") == "insert"
                            and int(entry.get("sentence_index") or 0) == duplicate_key[0]
                            and str(entry.get("target_norm") or "") == duplicate_key[1]
                        )
                    ]
                    if action == "insert":
                        insert_at = next(
                            (
                                index
                                for index, entry in enumerate(entries)
                                if int(entry.get("sentence_index") or 0) >= patch_chunk_index
                            ),
                            len(entries),
                        )
                        entries.insert(
                            insert_at,
                            {
                                "kind": "insert",
                                "chunk_index": sequence_chunk_index,
                                "sentence_index": patch_chunk_index,
                                "clause_index": 0,
                                "locator": str(candidate.get("target_locator") or "") or patch_locator_text(patch_chunk_index),
                                "text": target_text,
                                "target_norm": target_norm,
                                "audio": str(final_patch_audio),
                            },
                        )
                    else:
                        clause_indices = [
                            int(index)
                            for index in (candidate.get("clause_indices") or [])
                            if str(index).strip()
                        ]
                        if not clause_indices and candidate.get("clause_index"):
                            clause_indices = [int(candidate["clause_index"])]
                        target_units = [
                            unit
                            for unit in (candidate.get("target_units") or [])
                            if isinstance(unit, dict)
                        ]
                        target_unit_keys = {
                            (int(unit.get("sentence_index") or 0), int(unit.get("clause_index") or 0))
                            for unit in target_units
                        }
                        if target_unit_keys:
                            target_entry_indexes = [
                                index
                                for index, entry in enumerate(entries)
                                if entry.get("kind") != "insert"
                                and (int(entry.get("sentence_index") or 0), int(entry.get("clause_index") or 0)) in target_unit_keys
                            ]
                        else:
                            target_entry_indexes = [
                                index
                                for index, entry in enumerate(entries)
                                if entry.get("kind") != "insert"
                                and int(entry.get("sentence_index") or 0) == patch_chunk_index
                                and (not clause_indices or int(entry.get("clause_index") or 0) in clause_indices)
                            ]
                        if not target_entry_indexes:
                            target_entry_indexes = [
                                index
                                for index, entry in enumerate(entries)
                                if entry.get("kind") != "insert"
                                and source_target_norm
                                and source_target_norm in normalize_text(str(entry.get("text") or ""))
                            ]
                        if not target_entry_indexes:
                            raise ValueError(
                                f"替换目标 {str(candidate.get('target_locator') or '') or patch_locator_text(patch_chunk_index, candidate.get('clause_indices') or [], candidate.get('clause_index'))} 没有可剪掉的原音频小段。"
                                "请先点击“3. 建立修补分段索引”。"
                            )
                        target_entries = [entries[index] for index in target_entry_indexes]
                        timed_target_entries = [
                            entry
                            for entry in target_entries
                            if entry.get("start") is not None and entry.get("end") is not None
                        ]
                        current_source = Path(str(audio_value or "")).expanduser()
                        target_set = set(target_entry_indexes)
                        first_target_entry = entries[target_entry_indexes[0]]

                        if current_source.exists() and len(timed_target_entries) == len(target_entries):
                            raw_splice_start = min(float(entry["start"]) for entry in timed_target_entries)
                            raw_splice_end = max(float(entry["end"]) for entry in timed_target_entries)
                            splice_start = raw_splice_start
                            splice_end = raw_splice_end
                            previous_entries = [
                                entry
                                for index, entry in enumerate(entries[: min(target_entry_indexes)])
                                if index not in target_set and entry.get("end") is not None
                            ]
                            next_entries = [
                                entry
                                for index, entry in enumerate(entries[max(target_entry_indexes) + 1 :], start=max(target_entry_indexes) + 1)
                                if index not in target_set and entry.get("start") is not None
                            ]
                            search_seconds = manual_patch_boundary_search_seconds(config)
                            neighbor_bleed = manual_patch_neighbor_bleed_seconds(config)
                            previous_end = float(previous_entries[-1]["end"]) if previous_entries else None
                            next_start = float(next_entries[0]["start"]) if next_entries else None
                            if previous_entries:
                                splice_start = max(splice_start, float(previous_end) - neighbor_bleed)
                            if next_entries:
                                splice_end = min(splice_end, float(next_start) + neighbor_bleed)
                            if splice_end <= splice_start:
                                raise ValueError("替换区间被相邻编号保护夹空，请重新建立修补分段索引或改用更大的编号范围。")
                            current_duration = audio_duration_seconds(config, current_source) or max(splice_end, raw_splice_end)
                            start_search = (
                                max(0.0, min(raw_splice_start, splice_start) - search_seconds),
                                min(splice_end - 0.05, max(raw_splice_start, splice_start) + search_seconds),
                            )
                            end_search = (
                                max(splice_start + 0.05, min(raw_splice_end, splice_end) - search_seconds),
                                min(current_duration, max(raw_splice_end, splice_end) + search_seconds),
                            )
                            if previous_end is not None:
                                start_search = (max(start_search[0], previous_end - neighbor_bleed), start_search[1])
                            if next_start is not None:
                                end_search = (end_search[0], min(end_search[1], next_start + neighbor_bleed))
                            direct_splice_audio = splice_audio_replace_range(
                                config,
                                current_source,
                                final_patch_audio,
                                job / "tts" / "tts_sentence_patch_applied.wav",
                                splice_start,
                                splice_end,
                                job / "logs" / "sentence_patch_direct_splice",
                                start_search=start_search,
                                end_search=end_search,
                            )
                            splice_report_path = job / "logs" / "sentence_patch_direct_splice" / "splice_boundaries.json"
                            if splice_report_path.exists():
                                try:
                                    splice_report = json.loads(splice_report_path.read_text(encoding="utf-8-sig"))
                                    splice_start = float(splice_report.get("final_start", splice_start))
                                    splice_end = float(splice_report.get("final_end", splice_end))
                                except Exception:
                                    pass
                            original_span = max(0.001, splice_end - splice_start)
                            patch_duration = audio_duration_seconds(config, final_patch_audio) or original_span
                            delta = patch_duration - original_span
                            replacement_entry = {
                                "kind": "patch",
                                "chunk_index": int(first_target_entry.get("chunk_index") or sequence_chunk_index),
                                "sentence_index": patch_chunk_index,
                                "clause_index": int(first_target_entry.get("clause_index") or 0),
                                "locator": str(candidate.get("target_locator") or "") or patch_locator_text(patch_chunk_index, clause_indices, candidate.get("clause_index")),
                                "text": replacement_text,
                                "target_norm": target_norm,
                                "source_audio": str(direct_splice_audio),
                                "start": splice_start,
                                "end": splice_start + patch_duration,
                                "audio": str(final_patch_audio),
                                "splice_report": str(splice_report_path) if splice_report_path else "",
                            }
                            rebuilt_entries: list[dict[str, Any]] = []
                            inserted_replacement = False
                            for entry_index, entry in enumerate(entries):
                                if entry_index in target_set:
                                    if not inserted_replacement:
                                        rebuilt_entries.append(replacement_entry)
                                        inserted_replacement = True
                                    continue
                                updated_entry = dict(entry)
                                if updated_entry.get("start") is not None and updated_entry.get("end") is not None:
                                    old_start = float(updated_entry["start"])
                                    old_end = float(updated_entry["end"])
                                    if old_end <= splice_start:
                                        pass
                                    elif old_start >= splice_end:
                                        updated_entry["start"] = old_start + delta
                                        updated_entry["end"] = old_end + delta
                                    elif old_start < splice_start < old_end:
                                        updated_entry["end"] = splice_start
                                    elif old_start < splice_end < old_end:
                                        updated_entry["start"] = splice_start + patch_duration
                                        updated_entry["end"] = old_end + delta
                                    updated_entry["source_audio"] = str(direct_splice_audio)
                                rebuilt_entries.append(updated_entry)
                            entries = rebuilt_entries
                        else:
                            replacement_entry = {
                                "kind": "patch",
                                "chunk_index": int(first_target_entry.get("chunk_index") or sequence_chunk_index),
                                "sentence_index": patch_chunk_index,
                                "clause_index": int(first_target_entry.get("clause_index") or 0),
                                "locator": str(candidate.get("target_locator") or "") or patch_locator_text(patch_chunk_index, clause_indices, candidate.get("clause_index")),
                                "text": replacement_text,
                                "target_norm": target_norm,
                                "audio": str(final_patch_audio),
                            }
                            rebuilt_entries = []
                            inserted_replacement = False
                            for entry_index, entry in enumerate(entries):
                                if entry_index in target_set:
                                    if not inserted_replacement:
                                        rebuilt_entries.append(replacement_entry)
                                        inserted_replacement = True
                                    continue
                                rebuilt_entries.append(entry)
                            entries = rebuilt_entries
                elif action == "insert":
                    entries = [
                        entry
                        for entry in entries
                        if not (
                            entry.get("kind") == "insert"
                            and int(entry.get("chunk_index") or 0) == sequence_chunk_index
                            and str(entry.get("target_norm") or "") == target_norm
                        )
                    ]
                    insert_at = sequence_insert_position(entries, sequence_chunk_index)
                    entries.insert(
                        insert_at,
                        {
                            "kind": "insert",
                            "chunk_index": sequence_chunk_index,
                            "text": target_text,
                            "target_norm": target_norm,
                            "audio": str(final_patch_audio),
                        },
                    )
                else:
                    entries = [
                        entry
                        for entry in entries
                        if not (
                            entry.get("kind") == "insert"
                            and int(entry.get("chunk_index") or 0) == sequence_chunk_index
                            and str(entry.get("target_norm") or "") == target_norm
                        )
                    ]
                    replaced = False
                    for entry_index, entry in enumerate(entries):
                        if int(entry.get("chunk_index") or 0) == sequence_chunk_index and entry.get("kind") != "insert":
                            entries[entry_index] = {
                                "kind": "patch",
                                "chunk_index": sequence_chunk_index,
                                "text": replacement_text,
                                "target_norm": target_norm,
                                "audio": str(final_patch_audio),
                            }
                            replaced = True
                            break
                    if not replaced:
                        raise ValueError(
                            f"替换目标 {str(candidate.get('target_locator') or '') or patch_locator_text(patch_chunk_index, candidate.get('clause_indices') or [], candidate.get('clause_index'))} 没有可剪掉的原音频段。"
                            "请先运行 2b 生成完整分段音频映射，或改用漏句插入。"
                        )

                if direct_splice_audio is not None:
                    final_audio = direct_splice_audio
                else:
                    pad_seconds = 0.0 if indexed_by_locator else max(0.0, min(0.8, float(pad_ms_value or 0.0) / 1000.0))
                    ordered_audio = [Path(str(entry["audio"])) for entry in entries]
                    final_audio = concat_audio_with_padding(
                        config,
                        ordered_audio,
                        job / "tts" / "tts_sentence_patch_applied.wav",
                        pad_seconds,
                        job / "logs" / "sentence_patch_concat",
                    )
                save_audio_sequence(job, sequence_chunks, entries)

                from faster_whisper import WhisperModel  # type: ignore

                asr_model = WhisperModel(str(config.asr_model), device="cpu", compute_type="int8")
                patched_srt, raw_srt_text, aligned_to_script, subtitle_source_mode = subtitle_srt_after_audio_edit(
                    config,
                    final_audio,
                    asr_model,
                    asr_language(text_lang_value),
                    text or "",
                    srt_text or "",
                )
                srt_path = write_text(job / "asr" / "subtitles.sentence_patch.srt", patched_srt)
                raw_srt_path = write_text(job / "asr" / "subtitles.sentence_patch.raw_asr.srt", raw_srt_text)
                summary, final_review = local_second_review(text or "", raw_srt_text)
                report_path = job / "checks" / "sentence_patch_apply.md"
                report_path.write_text(
                    "\n".join(
                        [
                            "# Sentence Patch Apply",
                            "",
                            f"- Action: {action}",
                            f"- Target: {str(candidate.get('target_locator') or '') or patch_locator_text(patch_chunk_index, candidate.get('clause_indices') or [], candidate.get('clause_index'))}",
                            f"- Target text: `{target_text}`",
                            f"- Candidate audio: `{candidate_audio}`",
                            f"- Applied audio: `{final_audio}`",
                            f"- Splice report: `{splice_report_path}`" if splice_report_path else "- Splice report: -",
                            f"- Subtitle text source: {subtitle_source_mode}",
                            f"- SRT: `{srt_path}`",
                            f"- Raw ASR SRT: `{raw_srt_path}`",
                            "",
                            final_review,
                        ]
                    ),
                    encoding="utf-8",
                )
                message = "\n".join(
                    [
                        "单句补漏/替换已应用",
                        summary,
                        triad_report(text or "", patched_srt, raw_srt_text, aligned_to_script),
                        f"Subtitle text source: {subtitle_source_mode}",
                        f"Audio: {final_audio}",
                        f"SRT: {srt_path}",
                        f"Raw ASR SRT: {raw_srt_path}",
                        f"Report: {report_path}",
                    ]
                )
                return str(final_audio), str(final_audio), str(srt_path), format_srt_for_editor(config, patched_srt), final_review, message
            except Exception:
                return None, audio_value or "", "", srt_text or "", "", traceback.format_exc()

        def render_video(job_value: str, image_value: str, audio_value: str, bgm: Any, text: str, srt_text: str, crop_x_value: float, crop_y_value: float, bgm_volume_value: float, bgm_start_value: float):
            try:
                if not job_value:
                    raise ValueError("请先点击“准备任务”。")
                if not image_value:
                    raise ValueError("请先上传图片。")
                if not audio_value:
                    raise ValueError("请先生成或上传旁白音频。")
                job = Path(job_value)
                text_path = write_text(job / "input" / "source.txt", text or "")
                head_pad_ms = max(0, min(1000, config_int(config, "render_audio_head_pad_ms", 200)))
                head_pad_seconds = head_pad_ms / 1000.0
                render_audio = Path(audio_value)
                if head_pad_seconds > 0:
                    render_audio = prepend_audio_silence(
                        config,
                        Path(audio_value),
                        job / "tmp" / "render_narration_head_padded.wav",
                        head_pad_seconds,
                        job / "logs" / "render_audio_head_pad.txt",
                    )
                srt_path = None
                subtitle_source_mode = "none"
                if (text or "").strip():
                    audio_for_timing = Path(audio_value)
                    final_blocks, subtitle_source_mode = final_subtitle_blocks_from_source_or_editor(
                        config,
                        text or "",
                        srt_text or "",
                    )
                    if srt_text.strip() and "-->" in srt_text:
                        timing_segments = srt_segments_from_text(srt_text)
                        if timing_segments:
                            srt_for_render = script_aligned_srt_from_blocks(config, audio_for_timing, timing_segments, final_blocks)
                        else:
                            srt_for_render = render_srt_from_editor(config, audio_for_timing, "\n\n".join(final_blocks), 0.0)
                    else:
                        srt_for_render = render_srt_from_editor(config, audio_for_timing, "\n\n".join(final_blocks), 0.0)
                    if head_pad_seconds > 0:
                        srt_for_render = shift_srt_timestamps(srt_for_render, head_pad_seconds)
                    srt_path = write_text(job / "asr" / "subtitles.render_shifted.srt", srt_for_render)
                elif srt_text.strip():
                    subtitle_source_mode = "editor"
                    srt_for_render = render_srt_from_editor(config, Path(audio_value), srt_text, head_pad_seconds)
                    srt_path = write_text(job / "asr" / "subtitles.render_shifted.srt", srt_for_render)
                bgm_path = copy_upload(bgm, job / "input" / "bgm") if bgm else None
                render_name = "final"
                command = [
                    str(config.python_exe),
                    str(config.video_script),
                    "--image",
                    image_value,
                    "--audio",
                    str(render_audio),
                    "--expected-text-file",
                    str(text_path),
                    "--crop-x",
                    str(crop_x_value),
                    "--crop-y",
                    str(crop_y_value),
                    "--ffmpeg",
                    str(config.ffmpeg),
                    "--ffprobe",
                    str(config.ffprobe),
                    "--asr-model",
                    str(config.asr_model),
                    "--output-dir",
                    str(job / "render"),
                    "--name",
                    render_name,
                    "--keep-existing",
                ]
                if srt_path:
                    command.extend(["--srt", str(srt_path)])
                if bgm_path:
                    command.extend(["--bgm", str(bgm_path), "--bgm-volume", str(bgm_volume_value), "--bgm-start", str(bgm_start_value)])
                result = run_command(command, config.project_root, job / "logs" / "render.txt")
                if result.returncode != 0:
                    raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
                manifest = parse_json_from_stdout(result.stdout)
                video = Path(str(manifest.get("video") or job / "render" / render_name / "preview.mp4"))
                if not video.exists():
                    candidates = sorted((job / "render").glob("**/*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                    video = candidates[0] if candidates else video
                if not video.exists():
                    raise RuntimeError("渲染完成但没有找到 MP4。")
                return (
                    str(video),
                    str(video),
                    str(video),
                    "\n".join(
                        [
                            "渲染完成",
                            f"Video: {video}",
                            f"Render audio: {render_audio}",
                            f"Head pad: {head_pad_ms} ms",
                            f"Subtitle text source: {subtitle_source_mode}",
                            f"Job: {job}",
                            f"Log: {job / 'logs' / 'render.txt'}",
                        ]
                    ),
                )
            except Exception:
                return None, "", "", traceback.format_exc()

        btn_prepare.click(
            prepare_job,
            inputs=[job_name, image_file, uploaded_audio, uploaded_srt, source_text],
            outputs=[
                job_state,
                image_state,
                audio_state,
                video_state,
                srt_state,
                source_text_state,
                generated_audio,
                srt_editor,
                status,
                rendered_video_path,
                output_dir,
            ],
        )
        model.change(
            on_model_preset_change,
            inputs=[model],
            outputs=[
                gpt_weights,
                sovits_weights,
                reference_preset,
                reference_text,
                prompt_lang,
                text_lang,
                text_split_method,
                speed_factor,
                fragment_interval,
                top_k,
                top_p,
                temperature,
            ],
        )
        gpt_weights.change(
            on_gpt_weights_change,
            inputs=[gpt_weights],
            outputs=[
                sovits_weights,
                reference_preset,
                reference_text,
                prompt_lang,
                text_lang,
                text_split_method,
                speed_factor,
                fragment_interval,
                top_k,
                top_p,
                temperature,
                status,
            ],
        )
        btn_save_model_preset.click(
            save_current_model_preset,
            inputs=[
                preset_name,
                gpt_weights,
                sovits_weights,
                reference_preset,
                ref_audio_file,
                reference_text,
                prompt_lang,
                text_lang,
                text_split_method,
                speed_factor,
                fragment_interval,
                top_k,
                top_p,
                temperature,
            ],
            outputs=[
                model,
                gpt_weights,
                sovits_weights,
                reference_preset,
                reference_text,
                status,
            ],
        )
        btn_save_inference_preset.click(
            save_current_inference_preset,
            inputs=[
                model,
                text_split_method,
                speed_factor,
                fragment_interval,
                top_k,
                top_p,
                temperature,
            ],
            outputs=[status],
            api_name="save_inference_preset",
        )
        reference_preset.change(
            on_reference_preset_change,
            inputs=[reference_preset],
            outputs=[reference_text, prompt_lang],
        )
        btn_refresh_weights.click(
            refresh_weight_dropdowns,
            inputs=[gpt_weights, sovits_weights],
            outputs=[gpt_weights, sovits_weights, status],
        )
        btn_tts.click(
            generate_tts,
            inputs=[
                job_state,
                model,
                source_text,
                gpt_weights,
                sovits_weights,
                reference_preset,
                ref_audio_file,
                reference_text,
                no_ref_text,
                prompt_lang,
                text_lang,
                text_split_method,
                speed_factor,
                fragment_interval,
                top_k,
                top_p,
                temperature,
            ],
            outputs=[generated_audio, audio_state, status],
        )
        btn_tts_repair.click(
            build_manual_patch_audio_index,
            inputs=[
                job_state,
                audio_state,
                source_text,
                text_lang,
            ],
            outputs=[generated_audio, audio_state, srt_state, srt_editor, manual_patch_options, status],
        )
        btn_crop.click(
            make_crop,
            inputs=[job_state, image_state, crop_x, crop_y],
            outputs=[crop_preview, status],
        )
        btn_asr.click(
            subtitle_retime_and_review,
            inputs=[job_state, audio_state, source_text, srt_editor, text_lang, review_mode, agent_api_url, agent_model, agent_api_key],
            outputs=[srt_state, srt_editor, status, review_report],
            api_name="subtitle_retime_and_review",
        )
        btn_list_manual_patch.click(
            list_manual_patch_options,
            inputs=[source_text, auto_chunk_chars],
            outputs=[manual_patch_options],
            api_name="list_manual_patch_options",
        )
        sentence_patch_target.change(
            sentence_patch_target_to_text,
            inputs=[source_text, sentence_patch_target],
            outputs=[sentence_patch_text, status],
            api_name="sentence_patch_target_to_text",
        )
        btn_sentence_patch_preview.click(
            generate_sentence_patch_candidate,
            inputs=[
                job_state,
                audio_state,
                source_text,
                srt_editor,
                sentence_patch_text,
                sentence_patch_target,
                model,
                gpt_weights,
                sovits_weights,
                reference_preset,
                ref_audio_file,
                reference_text,
                no_ref_text,
                prompt_lang,
                text_lang,
                text_split_method,
                sentence_speed,
                sentence_fragment,
                sentence_top_k,
                sentence_top_p,
                sentence_temperature,
                auto_chunk_chars,
                auto_retries,
                auto_pass_cer,
            ],
            outputs=[sentence_patch_state, sentence_patch_audio, generated_audio, status],
            api_name="sentence_patch_preview",
        )
        btn_sentence_patch_apply.click(
            apply_sentence_patch_candidate,
            inputs=[
                job_state,
                audio_state,
                source_text,
                srt_editor,
                sentence_patch_state,
                text_lang,
                auto_chunk_chars,
                auto_pad_ms,
            ],
            outputs=[generated_audio, audio_state, srt_state, srt_editor, review_report, status],
            api_name="sentence_patch_apply",
        )
        btn_render.click(
            render_video,
            inputs=[job_state, image_state, audio_state, bgm_file, source_text, srt_editor, crop_x, crop_y, bgm_volume, bgm_start],
            outputs=[video_preview, video_state, rendered_video_path, status],
        )
        demo.load(
            note_browser_heartbeat,
            outputs=[browser_heartbeat_state],
            queue=False,
        )
        browser_heartbeat_timer.tick(
            note_browser_heartbeat,
            outputs=[browser_heartbeat_state],
            queue=False,
            show_progress="hidden",
        )
        demo.unload(request_browser_unload_exit)

    return demo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--open-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    stdout_log = (APP_ROOT / "server_stdout.txt").open("a", encoding="utf-8", buffering=1)
    stderr_log = (APP_ROOT / "server_stderr.txt").open("a", encoding="utf-8", buffering=1)
    sys.stdout = stdout_log
    sys.stderr = stderr_log
    try:
        args = parse_args()
        configure_local_proxy_bypass()
        patch_gradio_local_url_check()
        boot_log(f"loading config: {args.config}")
        config = load_config(Path(args.config))
        boot_log("creating UI")
        demo = create_ui(config)
        demo.queue()
        boot_log(f"launching on http://{config.host}:{config.port}")
        demo.launch(
            server_name=config.host,
            server_port=config.port,
            inbrowser=args.open_browser,
            prevent_thread_lock=True,
        )
        boot_log("launch returned; keeping process alive")
        while True:
            time.sleep(3600)
        return 0
    except BaseException:
        boot_log("fatal error:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
