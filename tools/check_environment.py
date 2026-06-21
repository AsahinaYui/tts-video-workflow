from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
VIDEO_WEBUI_DIR = REPO_ROOT / "video-webui"


@dataclass
class Check:
    level: str
    name: str
    detail: str


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def is_unset(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or "your_" in text.lower() or "path/to" in text.lower()


def resolve_path(value: Any, base: Path) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def add(checks: list[Check], level: str, name: str, detail: str) -> None:
    checks.append(Check(level=level, name=name, detail=detail))


def check_file(
    checks: list[Check],
    name: str,
    value: Any,
    *,
    base: Path,
    required: bool = True,
) -> Path | None:
    if is_unset(value):
        add(checks, "FAIL" if required else "WARN", name, "not configured")
        return None
    path = resolve_path(value, base)
    if path.is_file():
        add(checks, "OK", name, str(path))
        return path
    add(checks, "FAIL" if required else "WARN", name, f"missing file: {path}")
    return path


def check_dir(
    checks: list[Check],
    name: str,
    value: Any,
    *,
    base: Path,
    required: bool = True,
) -> Path | None:
    if is_unset(value):
        add(checks, "FAIL" if required else "WARN", name, "not configured")
        return None
    path = resolve_path(value, base)
    if path.is_dir():
        add(checks, "OK", name, str(path))
        return path
    add(checks, "FAIL" if required else "WARN", name, f"missing directory: {path}")
    return path


def run_probe(command: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic tool should report any probe error.
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip().splitlines()
    detail = output[0] if output else f"exit code {result.returncode}"
    return result.returncode == 0, detail


def parse_simple_yaml(path: Path) -> dict[str, dict[str, str]]:
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


def check_python_imports(checks: list[Check], python_exe: Path | None) -> None:
    if not python_exe or not python_exe.is_file():
        add(checks, "FAIL", "Python imports", "python_exe is missing")
        return
    ok, detail = run_probe([str(python_exe), "-c", "import gradio, faster_whisper; print('imports ok')"])
    add(checks, "OK" if ok else "FAIL", "Python packages", detail)


def check_executable_version(checks: list[Check], name: str, exe: Path | None, arg: str = "-version") -> None:
    if not exe or not exe.is_file():
        add(checks, "FAIL", name, "missing executable")
        return
    ok, detail = run_probe([str(exe), arg], timeout=20)
    add(checks, "OK" if ok else "WARN", name, detail)


def check_port(checks: list[Check], host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        result = sock.connect_ex((host, port))
    if result == 0:
        add(checks, "WARN", "WebUI port", f"{host}:{port} is already in use")
    else:
        add(checks, "OK", "WebUI port", f"{host}:{port} is available")


def check_write_access(checks: list[Check], directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write_test_", dir=directory, delete=True) as handle:
            handle.write(b"ok")
        add(checks, "OK", "Job directory write access", str(directory))
    except Exception as exc:  # noqa: BLE001 - diagnostic tool should report any write error.
        add(checks, "FAIL", "Job directory write access", f"{directory}: {exc}")


def check_asr_model(checks: list[Check], value: Any, base: Path) -> None:
    path = check_dir(checks, "Faster-Whisper model", value, base=base, required=True)
    if not path or not path.is_dir():
        return
    expected = ["model.bin", "config.json"]
    missing = [name for name in expected if not (path / name).is_file()]
    if missing:
        add(checks, "FAIL", "Faster-Whisper model files", "missing: " + ", ".join(missing))
    else:
        add(checks, "OK", "Faster-Whisper model files", str(path))


def check_tts_base_weights(
    checks: list[Check],
    config: dict[str, Any],
    config_base: Path,
    project_root: Path,
    gptsovits_root: Path | None,
) -> None:
    models = config.get("models") or []
    first_model = models[0] if models else {}
    base_config_path = check_file(
        checks,
        "Base voice config",
        first_model.get("base_config"),
        base=config_base,
        required=True,
    )
    if not base_config_path or not base_config_path.is_file():
        return

    voice_config = load_json(base_config_path)
    tts_config_value = voice_config.get("tts_config_path")
    tts_config_path = check_file(
        checks,
        "GPT-SoVITS TTS config",
        tts_config_value,
        base=project_root,
        required=True,
    )
    if not gptsovits_root or not gptsovits_root.is_dir() or not tts_config_path or not tts_config_path.is_file():
        return

    version = str(first_model.get("default_version") or voice_config.get("model_switch", {}).get("default_version") or "v2")
    sections = parse_simple_yaml(tts_config_path)
    section = sections.get(version)
    if not section:
        add(checks, "FAIL", "Pretrained base section", f"missing section [{version}] in {tts_config_path}")
        return

    for key, label, suffix in (
        ("t2s_weights_path", "Base GPT weight", ".ckpt"),
        ("vits_weights_path", "Base SoVITS weight", ".pth"),
    ):
        raw = section.get(key)
        if not raw:
            add(checks, "FAIL", label, f"{version}.{key} is missing")
            continue
        path = resolve_path(raw, gptsovits_root)
        if path.is_file() and path.suffix.lower() == suffix:
            add(checks, "OK", label, str(path))
        elif path.is_file():
            add(checks, "FAIL", label, f"wrong suffix, expected {suffix}: {path}")
        else:
            add(checks, "FAIL", label, f"missing file: {path}")


def build_checks(config_path: Path) -> list[Check]:
    checks: list[Check] = []
    config = load_json(config_path)
    config_base = config_path.parent

    add(checks, "OK", "Config", str(config_path))
    project_root = check_dir(checks, "Project root", config.get("project_root"), base=config_base, required=True)
    if project_root is None:
        project_root = REPO_ROOT

    python_exe = check_file(checks, "Python executable", config.get("python_exe"), base=config_base, required=True)
    check_python_imports(checks, python_exe)

    gptsovits_root = check_dir(checks, "GPT-SoVITS root", config.get("gptsovits_root"), base=config_base, required=True)
    if gptsovits_root:
        check_file(checks, "GPT-SoVITS API script", gptsovits_root / "api_v2.py", base=config_base, required=True)

    ffmpeg = check_file(checks, "FFmpeg", config.get("ffmpeg"), base=config_base, required=True)
    ffprobe = check_file(checks, "FFprobe", config.get("ffprobe"), base=config_base, required=True)
    check_executable_version(checks, "FFmpeg version", ffmpeg)
    check_executable_version(checks, "FFprobe version", ffprobe)

    check_file(checks, "TTS script", config.get("gsv_tts_script"), base=config_base, required=True)
    check_file(checks, "TTS checker script", config.get("tts_checker_script"), base=config_base, required=True)
    check_file(checks, "Video render script", config.get("video_script"), base=config_base, required=True)

    check_asr_model(checks, config.get("asr_model"), config_base)
    check_tts_base_weights(checks, config, config_base, project_root, gptsovits_root)

    for index, value in enumerate(config.get("gpt_weights_dirs") or [], start=1):
        check_dir(checks, f"GPT weights dir {index}", value, base=config_base, required=False)
    for index, value in enumerate(config.get("sovits_weights_dirs") or [], start=1):
        check_dir(checks, f"SoVITS weights dir {index}", value, base=config_base, required=False)

    host = str(config.get("host") or "127.0.0.1")
    port = int(config.get("port") or 7860)
    check_port(checks, host, port)

    jobs_dir = resolve_path(config.get("jobs_dir") or "jobs", config_base)
    check_write_access(checks, jobs_dir)
    return checks


def print_report(checks: list[Check]) -> None:
    width = max((len(item.name) for item in checks), default=10)
    for item in checks:
        print(f"[{item.level:<4}] {item.name:<{width}}  {item.detail}")
    fails = sum(1 for item in checks if item.level == "FAIL")
    warns = sum(1 for item in checks if item.level == "WARN")
    print()
    print(f"Summary: {fails} fail(s), {warns} warning(s), {len(checks)} check(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local TTS video workflow dependencies.")
    default_config = VIDEO_WEBUI_DIR / "config.json"
    if not default_config.exists():
        default_config = VIDEO_WEBUI_DIR / "config.example.json"
    parser.add_argument("--config", default=str(default_config), help="Path to video-webui config.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when warnings are present")
    args = parser.parse_args()

    config_path = resolve_path(args.config, Path.cwd())
    try:
        checks = build_checks(config_path)
    except Exception as exc:  # noqa: BLE001 - diagnostic tool should fail visibly.
        checks = [Check("FAIL", "Environment check", str(exc))]

    if args.json:
        print(json.dumps([item.__dict__ for item in checks], ensure_ascii=False, indent=2))
    else:
        print_report(checks)

    has_fail = any(item.level == "FAIL" for item in checks)
    has_warn = any(item.level == "WARN" for item in checks)
    if has_fail or (args.strict and has_warn):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
