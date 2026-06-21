---
name: video-platform-publisher
description: Prepare and publish finished vertical videos to creator websites after the GPT-SoVITS audio and single-image video workflows are complete. Use when the user wants to upload a generated MP4 to platforms such as Douyin, Xiaohongshu, Bilibili, Kuaishou, YouTube Shorts, or TikTok, needs platform-specific title/tag/cover/description handling, wants upload-ready metadata packages, or wants browser-assisted posting with a required final confirmation before public release.
---

# Video Platform Publisher

Use this skill only after `gptsovits-tts` and `single-image-tts-video` have produced a reviewed MP4.

## Principle

Treat publishing as three separate gates:

1. Prepare a local publish package.
2. Fill or save a platform draft in the website UI.
3. Ask the user for explicit confirmation before clicking final publish.

Never click the final public publish button without a fresh user confirmation in the current conversation.

## Inputs

- Final MP4, usually `outputs/video/.../preview.mp4`.
- Optional cover image. If absent, extract a cover frame from the MP4.
- Base title, description, and tags from the user or generated from the video copy.
- Target platform list.

## Platform Requirements

Use `references/platform_profiles.json` as the editable platform profile file. It stores:

- Upload URL.
- Expected video shape.
- Title or caption limits.
- Tag style: inline hashtags, separate tags, or topic tags.
- Cover requirement and cover guidance.
- Extra fields such as category.

These profiles are practical defaults, not permanent truth. If an exact platform limit matters, verify the live upload page in the browser before posting and update the profile if needed.

## Metadata Handling

Create one platform-specific draft per platform:

- Title: keep the core hook early. Short-video platforms prefer short titles; Bilibili can use a fuller title.
- Description: use the source copy or a concise emotional summary. Keep credits or workflow notes here when needed.
- Tags: store both plain tags and inline hashtags. Some platforms require separate tag chips, while others expect inline `#tag` text.
- Cover: choose a clean vertical frame with readable character face and no subtitle blocking the face. If the platform crops the cover, adjust inside the website UI.
- Category: select manually in the platform UI when required, because category options change often.

## Bilibili Preset

When preparing `--platform bilibili`, use these project defaults:

- Cover: use the original image supplied by the user, not the 9:16 video crop and not a video frame.
- Cover review: generate `cover_16x9.png` from the original image and send it to the user for approval.
- Cover upload: in Bilibili, choose `个人空间封面`, upload the approved 16:9 cover, and tick `双比例同步改动`. Bilibili will auto-generate the 4:3 首页推荐封面; do not manually edit the 4:3 cover.
- Title: if the user does not provide a title, use the first sentence/chunk of the video copy.
- Category/partition: `情感`.
- Creative declaration: `内容无需标注`.
- Default tags: `情感`, `原创`, `人生`, `文艺`, `MyGo`, `Avemujica`, `睦子米`, `若叶睦`.
- Publish timing: if the user requests scheduled publishing, use the requested time; otherwise default to immediate publishing.
- Description:

```text
文案：（本次音频和视频制作中使用的文案）
```

## Local Package Command

Prepare a multi-platform package:

```powershell
python .\skills\video-platform-publisher\scripts\prepare_publish_package.py `
  --video .\outputs\video\mutsumi_single_image_20260610_crop9x16\preview.mp4 `
  --platform douyin `
  --platform xiaohongshu `
  --platform bilibili `
  --title "好像我不主动联系你" `
  --description "一段Mutsumi风格的情绪短片。" `
  --tag Mutsumi `
  --tag 配音 `
  --tag 情绪短片
```

Prepare a Bilibili package with the project preset:

```powershell
python .\skills\video-platform-publisher\scripts\prepare_publish_package.py `
  --video .\outputs\video\mutsumi_single_image_20260610_crop9x16\preview.mp4 `
  --platform bilibili `
  --text-file .\work\tts-input-20260610.txt `
  --original-image "E:\path\to\original-image.png"
```

The Bilibili package includes:

- `cover_16x9.png`
- `category.txt`
- `creative_declaration.txt`
- `publish_timing.txt`
- The default tag list unless the user explicitly passes custom `--tag` values.

Prepare Bilibili with scheduled publishing:

```powershell
python .\skills\video-platform-publisher\scripts\prepare_publish_package.py `
  --video .\outputs\video\mutsumi_single_image_20260610_crop9x16\preview.mp4 `
  --platform bilibili `
  --text-file .\work\tts-input-20260610.txt `
  --original-image "E:\path\to\original-image.png" `
  --scheduled-time "2026-06-10 20:30"
```

Prepare every known platform:

```powershell
python .\skills\video-platform-publisher\scripts\prepare_publish_package.py `
  --video .\outputs\video\final\preview.mp4 `
  --all-platforms `
  --title "标题" `
  --description "简介" `
  --tag Mutsumi
```

Use an existing cover:

```powershell
python .\skills\video-platform-publisher\scripts\prepare_publish_package.py `
  --video .\outputs\video\final\preview.mp4 `
  --platform douyin `
  --title "标题" `
  --cover .\work\cover.png
```

The package is saved under `outputs/publish/<name>/` and contains:

- `publish_manifest.json`
- `cover.png`
- One folder per platform
- `metadata.json`
- `publish_draft.md`
- `title.txt`
- `description.txt`
- `tags.txt`
- `hashtags.txt`

## Browser Upload Workflow

When uploading through a website:

1. Open the platform URL from the profile.
2. Let the user handle login, QR scan, or two-factor checks.
3. Upload the MP4 from the package or original final video path.
4. Fill title, description, tags, cover, category, visibility, comments, and other UI fields from the platform draft.
5. For Bilibili, do not choose a cover until the user has approved the 16:9 original-image crop.
6. For Bilibili cover upload, choose `个人空间封面`, upload the approved 16:9 cover, tick `双比例同步改动`, and leave the auto-generated 4:3 首页推荐封面 unchanged.
7. If `publish_timing.txt` says `scheduled`, select scheduled publishing and enter the scheduled time. If it says `immediate`, leave the post as immediate publishing.
8. If the platform supports drafts, save a draft first.
9. Screenshot or summarize the final pre-publish page.
10. Ask the user to confirm exact final publishing.
11. Only after confirmation, click publish.
12. Save the published URL, post ID, account name, timestamp, and any warnings into `publish_result.json`.

## Decision Rules

- If the user says "upload" but not "publish", stop at an uploaded draft or pre-publish page.
- If the user says "publish directly", still ask for one final confirmation after the fields are filled.
- If the user does not request scheduled publishing, treat publishing mode as immediate.
- If login or captcha appears, hand control to the user and continue after they say it is ready.
- If platform UI requirements differ from the profile, follow the live UI and record the change.
- If an asset or field is missing, generate a local draft and ask the user for the missing value instead of inventing risky public-facing details.

## Suggested Defaults For This Project

- Keep video as `1080x1920`, `25fps`.
- Use one of the first clear face frames as cover.
- Prefer 3-6 tags.
- Use emotional/character keywords rather than workflow keywords unless posting to a behind-the-scenes account.
- Keep public post text short and human; put technical notes in the description only when the platform audience expects them.
