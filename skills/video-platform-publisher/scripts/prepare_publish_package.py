#!/usr/bin/env python3
"""Prepare upload-ready metadata packages for finished videos."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
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
DEFAULT_PROFILES = SKILL_ROOT / "references" / "platform_profiles.json"
DEFAULT_GPTSOVITS_ROOT = Path(
    "E:/TTS/GPT-SoVITS-v2pro-20250604"
)


class PublishPackageError(RuntimeError):
    """User-facing packaging error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, help="Finished MP4 video.")
    parser.add_argument("--platform", action="append", help="Platform key. Repeat for multiple.")
    parser.add_argument("--all-platforms", action="store_true", help="Prepare every known platform.")
    parser.add_argument("--title", help="Base title. If omitted, platform defaults may use --text-file.")
    parser.add_argument("--description", default="", help="Base description/caption.")
    parser.add_argument("--text", help="Copy/text used in the video.")
    parser.add_argument("--text-file", help="UTF-8 file containing copy/text used in the video.")
    parser.add_argument("--source-credit", help="Image source credit for platform descriptions.")
    parser.add_argument("--tag", action="append", default=[], help="Tag without #. Repeat as needed.")
    parser.add_argument("--cover", help="Existing cover image. If omitted, extract one from the video.")
    parser.add_argument("--original-image", help="Original user-supplied image for platform cover crops.")
    parser.add_argument("--cover-time", type=float, default=1.0, help="Seconds used for extracted cover.")
    parser.add_argument("--cover-crop-x", type=float, default=0.5, help="Cover crop anchor: 0 left, 1 right.")
    parser.add_argument("--cover-crop-y", type=float, default=0.5, help="Cover crop anchor: 0 top, 1 bottom.")
    parser.add_argument(
        "--scheduled-time",
        help="Optional scheduled publish time. If omitted, publish timing defaults to immediate.",
    )
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs" / "publish"))
    parser.add_argument("--name", help="Publish package folder name.")
    parser.add_argument("--profiles", default=str(DEFAULT_PROFILES), help="Platform profiles JSON.")
    parser.add_argument("--ffmpeg", help="ffmpeg path.")
    parser.add_argument("--ffprobe", help="ffprobe path.")
    parser.add_argument("--copy-video", action="store_true", help="Copy video into each platform folder.")
    return parser.parse_args()


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def find_runtime_tool(name: str, explicit: str | None) -> Path:
    if explicit:
        path = resolve_path(explicit)
        if path.exists():
            return path
        raise PublishPackageError(f"{name} does not exist: {path}")
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path).resolve()
    candidate = DEFAULT_GPTSOVITS_ROOT / "runtime" / f"{name}.exe"
    if candidate.exists():
        return candidate
    raise PublishPackageError(f"Could not find {name}. Pass --{name} <path>.")


def load_profiles(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PublishPackageError(f"Profiles file does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def selected_profiles(args: argparse.Namespace, profiles: dict[str, Any]) -> list[str]:
    if args.all_platforms:
        return list(profiles.keys())
    keys = args.platform or []
    if not keys:
        raise PublishPackageError("Pass --platform <key> or --all-platforms.")
    missing = [key for key in keys if key not in profiles]
    if missing:
        known = ", ".join(sorted(profiles))
        raise PublishPackageError(f"Unknown platform(s): {', '.join(missing)}. Known: {known}")
    return keys


def read_text_argument(text: str | None, text_file: str | None) -> str:
    if text:
        return text.strip()
    if text_file:
        path = resolve_path(text_file)
        if not path.exists():
            raise PublishPackageError(f"Text file does not exist: {path}")
        return path.read_text(encoding="utf-8-sig").strip()
    return ""


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    pieces = [piece.strip() for piece in re.split(r"[。！？!?，,、\r\n]+", text) if piece.strip()]
    return pieces[0] if pieces else text.strip()


def clamp_anchor(value: float, name: str) -> float:
    if value < 0.0 or value > 1.0:
        raise PublishPackageError(f"{name} must be between 0.0 and 1.0.")
    return value


def probe_video(ffprobe: Path, video: Path) -> dict[str, Any]:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate,duration:format=duration,bit_rate",
        "-of",
        "json",
        str(video),
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if completed.returncode != 0:
        raise PublishPackageError(f"ffprobe failed:\n{completed.stderr.strip()}")
    return json.loads(completed.stdout)


def extract_cover(ffmpeg: Path, video: Path, output: Path, seconds: float) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-y",
        "-ss",
        f"{max(0.0, seconds):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        str(output),
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
    (output.parent / "cover_extract_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise PublishPackageError(f"Cover extraction failed:\n{completed.stderr[-3000:]}")
    return output


def crop_cover_from_image(
    ffmpeg: Path,
    image: Path,
    output: Path,
    width: int,
    height: int,
    crop_x: float,
    crop_y: float,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = ",".join(
        [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}:(iw-ow)*{crop_x:.4f}:(ih-oh)*{crop_y:.4f}",
            "setsar=1",
        ]
    )
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(image),
        "-vf",
        vf,
        "-frames:v",
        "1",
        str(output),
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
    (output.parent / f"{output.stem}_crop_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise PublishPackageError(f"Cover crop failed for {output.name}:\n{completed.stderr[-3000:]}")
    return output


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", value)
    if not match:
        raise PublishPackageError(f"Invalid resolution: {value}")
    return int(match.group(1)), int(match.group(2))


def normalize_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = tag.strip().lstrip("#")
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def inline_hashtags(tags: list[str]) -> str:
    return " ".join(f"#{tag}" for tag in tags)


def text_len(value: str) -> int:
    return len(value.strip())


def make_platform_metadata(
    *,
    platform_key: str,
    profile: dict[str, Any],
    title: str,
    description: str,
    tags: list[str],
    video: Path,
    cover: Path,
    probe: dict[str, Any],
    copy_text: str,
    source_credit: str,
    timing: dict[str, str],
    cover_candidates: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    fields = profile.get("fields", {})
    defaults = profile.get("defaults", {})
    cover_field = fields.get("cover", {})
    warnings: list[str] = []
    category = defaults.get("category")
    creative_declaration = defaults.get("creative_declaration")
    meta: dict[str, Any] = {
        "platform": platform_key,
        "platform_name": profile.get("display_name", platform_key),
        "upload_url": profile.get("url"),
        "video": str(video),
        "cover": str(cover),
        "cover_candidates": cover_candidates or [],
        "title": title.strip(),
        "description": description.strip(),
        "tags": tags,
        "hashtags_inline": inline_hashtags(tags),
        "category": category,
        "creative_declaration": creative_declaration,
        "source_credit": source_credit,
        "copy_text": copy_text,
        "publish_timing": timing,
        "cover_upload": {
            "target": cover_field.get("upload_target", ""),
            "sync_dual_ratio": bool(cover_field.get("sync_dual_ratio", False)),
            "auto_home_recommend_cover": cover_field.get("auto_home_recommend_cover", ""),
            "instruction": cover_field.get("guidance", ""),
        },
        "video_probe": probe,
        "status": "draft_ready",
        "publish_rule": "Do not click final publish until the user explicitly confirms.",
    }
    if cover_candidates:
        meta["cover_status"] = "needs_user_review"

    title_limit = fields.get("title", fields.get("caption", {})).get("max_chars")
    title_field_name = "caption" if "caption" in fields and "title" not in fields else "title"
    if title_limit and text_len(title) > int(title_limit):
        warnings.append(
            f"{profile.get('display_name', platform_key)} {title_field_name} is {text_len(title)} chars; "
            f"profile limit is {title_limit}."
        )

    description_limit = fields.get("description", {}).get("max_chars")
    if description_limit and text_len(description) > int(description_limit):
        warnings.append(
            f"{profile.get('display_name', platform_key)} description is {text_len(description)} chars; "
            f"profile limit is {description_limit}."
        )

    tag_field = fields.get("hashtags") or fields.get("tags")
    if tag_field:
        max_count = tag_field.get("max_count")
        if max_count and len(tags) > int(max_count):
            warnings.append(
                f"{profile.get('display_name', platform_key)} has {len(tags)} tags; "
                f"profile suggests at most {max_count}."
            )
        meta["tag_style"] = tag_field.get("style")

    expected = profile.get("expected_video", {})
    video_stream = next(
        (stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"),
        {},
    )
    width = video_stream.get("width")
    height = video_stream.get("height")
    if expected.get("resolution") and width and height and f"{width}x{height}" != expected["resolution"]:
        warnings.append(
            f"Video is {width}x{height}; {profile.get('display_name', platform_key)} profile expects "
            f"{expected['resolution']}."
        )

    meta["warnings"] = warnings
    return meta, warnings


def write_text_files(folder: Path, metadata: dict[str, Any]) -> None:
    tags = metadata.get("tags", [])
    platform_name = metadata.get("platform_name", metadata.get("platform"))
    lines = [
        f"# Publish Draft - {platform_name}",
        "",
        f"- Upload URL: {metadata.get('upload_url')}",
        f"- Video: `{metadata.get('video')}`",
        f"- Cover: `{metadata.get('cover')}`",
        f"- Status: {metadata.get('status')}",
        f"- Category: {metadata.get('category') or ''}",
        f"- Creative declaration: {metadata.get('creative_declaration') or ''}",
        f"- Publish timing: {metadata.get('publish_timing', {}).get('mode', 'immediate')}",
        f"- Scheduled time: {metadata.get('publish_timing', {}).get('scheduled_time', '')}",
        "",
        "## Title",
        "",
        metadata.get("title", ""),
        "",
        "## Description",
        "",
        metadata.get("description", ""),
        "",
        "## Tags",
        "",
        ", ".join(tags),
        "",
        "## Inline Hashtags",
        "",
        metadata.get("hashtags_inline", ""),
        "",
        "## Cover Candidates",
        "",
    ]
    cover_candidates = metadata.get("cover_candidates") or []
    if cover_candidates:
        lines.extend(
            f"- {candidate.get('aspect_ratio')}: `{candidate.get('path')}`"
            for candidate in cover_candidates
        )
        lines.append("")
        cover_upload = metadata.get("cover_upload") or {}
        if cover_upload.get("target"):
            lines.extend(
                [
                    f"Upload target: {cover_upload.get('target')}",
                    f"Sync dual ratio: {'yes' if cover_upload.get('sync_dual_ratio') else 'no'}",
                    f"Auto home recommendation cover: {cover_upload.get('auto_home_recommend_cover') or 'no'}",
                    "",
                ]
            )
        lines.extend(
            [
                "Cover review is required before using the Bilibili cover.",
                "",
            ]
        )
    else:
        lines.extend(["No alternate cover candidates.", ""])
    lines.extend(
        [
        "## Warnings",
        "",
        ]
    )
    warnings = metadata.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No profile warnings.")
    lines.extend(
        [
            "",
            "## Final Gate",
            "",
            "Open the platform upload page, fill fields, save as draft if possible, then ask the user before final publish.",
        ]
    )
    (folder / "publish_draft.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    (folder / "title.txt").write_text(metadata.get("title", "") + "\n", encoding="utf-8")
    (folder / "description.txt").write_text(metadata.get("description", "") + "\n", encoding="utf-8")
    (folder / "tags.txt").write_text("\n".join(tags) + "\n", encoding="utf-8")
    (folder / "hashtags.txt").write_text(metadata.get("hashtags_inline", "") + "\n", encoding="utf-8")
    (folder / "category.txt").write_text((metadata.get("category") or "") + "\n", encoding="utf-8")
    (folder / "creative_declaration.txt").write_text(
        (metadata.get("creative_declaration") or "") + "\n",
        encoding="utf-8",
    )
    timing = metadata.get("publish_timing") or {}
    (folder / "publish_timing.txt").write_text(
        f"{timing.get('mode', 'immediate')}\n{timing.get('scheduled_time', '')}\n",
        encoding="utf-8",
    )


def platform_title(args: argparse.Namespace, profile: dict[str, Any], copy_text: str) -> str:
    if args.title:
        return args.title.strip()
    if profile.get("defaults", {}).get("title") == "first_sentence":
        title = first_sentence(copy_text)
        if title:
            return title
    raise PublishPackageError("Title is missing. Pass --title or provide --text-file for default title.")


def platform_description(
    args: argparse.Namespace,
    profile: dict[str, Any],
    copy_text: str,
    source_credit: str,
) -> tuple[str, str]:
    if args.description:
        return args.description.strip(), source_credit
    defaults = profile.get("defaults", {})
    template = defaults.get("description_template")
    if template:
        credit = source_credit or defaults.get("source_credit_placeholder", "")
        return template.format(source_credit=credit, copy_text=copy_text), credit
    return "", source_credit


def publish_timing(args: argparse.Namespace, profile: dict[str, Any]) -> dict[str, str]:
    if args.scheduled_time:
        return {
            "mode": "scheduled",
            "scheduled_time": args.scheduled_time.strip(),
        }
    return {
        "mode": str(profile.get("defaults", {}).get("publish_timing", "immediate")),
        "scheduled_time": "",
    }


def platform_tags(cli_tags: list[str], profile: dict[str, Any]) -> list[str]:
    if cli_tags:
        return cli_tags
    return normalize_tags([str(tag) for tag in profile.get("defaults", {}).get("tags", [])])


def bilibili_cover_candidates(
    *,
    ffmpeg: Path,
    profile: dict[str, Any],
    original_image: Path | None,
    folder: Path,
    crop_x: float,
    crop_y: float,
) -> list[dict[str, str]]:
    cover_field = profile.get("fields", {}).get("cover", {})
    if cover_field.get("source") != "original_image":
        return []
    if original_image is None:
        raise PublishPackageError("Bilibili preset requires --original-image for 16:9 cover review.")
    if not original_image.exists():
        raise PublishPackageError(f"Original image does not exist: {original_image}")

    candidates: list[dict[str, str]] = []
    for item in cover_field.get("candidates", []):
        width, height = parse_resolution(str(item["resolution"]))
        output = folder / str(item["name"])
        crop_cover_from_image(ffmpeg, original_image, output, width, height, crop_x, crop_y)
        candidates.append(
            {
                "name": str(item["name"]),
                "aspect_ratio": str(item["aspect_ratio"]),
                "resolution": str(item["resolution"]),
                "path": str(output),
                "source_image": str(original_image),
            }
        )
    return candidates


def profile_uses_original_image_cover(profile: dict[str, Any]) -> bool:
    return profile.get("fields", {}).get("cover", {}).get("source") == "original_image"


def main() -> int:
    args = parse_args()
    try:
        video = resolve_path(args.video)
        if not video.exists():
            raise PublishPackageError(f"Video does not exist: {video}")
        ffmpeg = find_runtime_tool("ffmpeg", args.ffmpeg)
        ffprobe = find_runtime_tool("ffprobe", args.ffprobe)
        profiles = load_profiles(resolve_path(args.profiles))
        platform_keys = selected_profiles(args, profiles)
        tags = normalize_tags(args.tag)
        copy_text = read_text_argument(args.text, args.text_file)
        source_credit = (args.source_credit or "").strip()
        original_image = resolve_path(args.original_image) if args.original_image else None
        crop_x = clamp_anchor(args.cover_crop_x, "--cover-crop-x")
        crop_y = clamp_anchor(args.cover_crop_y, "--cover-crop-y")

        output_base = resolve_path(args.output_dir)
        package_name = args.name or f"publish_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        package_dir = output_base / package_name
        package_dir.mkdir(parents=True, exist_ok=True)

        probe = probe_video(ffprobe, video)
        needs_shared_cover = bool(args.cover) or any(
            not profile_uses_original_image_cover(profiles[key]) for key in platform_keys
        )
        cover = resolve_path(args.cover) if args.cover else package_dir / "cover.png"
        if args.cover:
            if not cover.exists():
                raise PublishPackageError(f"Cover does not exist: {cover}")
        elif needs_shared_cover:
            extract_cover(ffmpeg, video, cover, args.cover_time)
        else:
            cover = None

        all_warnings: list[str] = []
        platform_outputs: dict[str, Any] = {}
        for key in platform_keys:
            folder = package_dir / key
            folder.mkdir(parents=True, exist_ok=True)
            profile = profiles[key]
            cover_candidates = bilibili_cover_candidates(
                ffmpeg=ffmpeg,
                profile=profile,
                original_image=original_image,
                folder=folder,
                crop_x=crop_x,
                crop_y=crop_y,
            )
            if cover_candidates:
                platform_cover = Path(cover_candidates[0]["path"])
            else:
                if cover is None:
                    raise PublishPackageError(f"{key} requires a cover, but no cover was prepared.")
                platform_cover = folder / cover.name
                shutil.copy2(cover, platform_cover)
            platform_video = video
            if args.copy_video:
                platform_video = folder / video.name
                shutil.copy2(video, platform_video)
            title = platform_title(args, profile, copy_text)
            description, effective_source_credit = platform_description(
                args,
                profile,
                copy_text,
                source_credit,
            )
            platform_tag_list = platform_tags(tags, profile)
            timing = publish_timing(args, profile)
            metadata, warnings = make_platform_metadata(
                platform_key=key,
                profile=profile,
                title=title,
                description=description,
                tags=platform_tag_list,
                video=platform_video,
                cover=platform_cover,
                probe=probe,
                copy_text=copy_text,
                source_credit=effective_source_credit,
                timing=timing,
                cover_candidates=cover_candidates,
            )
            (folder / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            write_text_files(folder, metadata)
            all_warnings.extend(warnings)
            platform_outputs[key] = {
                "folder": str(folder),
                "metadata": str(folder / "metadata.json"),
                "draft": str(folder / "publish_draft.md"),
                "cover": str(platform_cover),
                "cover_candidates": cover_candidates,
                "video": str(platform_video),
                "publish_timing": timing,
                "warnings": warnings,
            }

        manifest = {
            "package_dir": str(package_dir),
            "video": str(video),
            "cover": str(cover) if cover is not None else None,
            "platforms": platform_outputs,
            "warnings": all_warnings,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (package_dir / "publish_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except PublishPackageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

