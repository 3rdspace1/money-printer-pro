#!/usr/bin/env python3
"""
Turn the daily BypassHormuz podcast briefing into a video and publish it.

    python scripts/bypass_daily.py --mode short --upload

Pipeline
--------
1. Read https://bypasshormuz.com/podcast/feed.xml and take the newest episode
   (or --date YYYY-MM-DD).
2. Download the published MP3. This is the narration; no TTS is generated.
3. Transcribe it via AIMLAPI (cached per episode) to recover the Marcus Kade
   script with punctuation and word timestamps.
4. Hand the script to MoneyPrinterPro, which builds scenes, visuals and
   word-accurate burned-in subtitles, and muxes the real podcast audio.
5. Optionally upload to YouTube / TikTok / Post Bridge and ping a webhook.

Nothing here writes to the bypass-monitor Cloudflare Worker. It is a
read-only consumer of the public feed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import requests  # noqa: E402

from bypass_narrator import (  # noqa: E402
    BypassNarratorError,
    Narration,
    PrerecordedTTS,
    prepare_narration,
)
from cache import get_accounts  # noqa: E402
from classes.YouTube import YouTube  # noqa: E402
from config import get_openai_api_key  # noqa: E402

SITE_URL = "https://bypasshormuz.com/"
DEFAULT_TAGS = [
    "geopolitics",
    "Strait of Hormuz",
    "oil markets",
    "Middle East",
    "energy security",
    "pipeline",
    "OSINT",
    "daily brief",
]
DEFAULT_HASHTAGS = ["Hormuz", "Geopolitics", "OOTT"]


def build_metadata(narration: Narration, mode: str) -> dict:
    """
    Builds YouTube metadata straight from the feed instead of asking an LLM,
    which keeps the marginal cost of each video at effectively zero.
    """
    episode = narration.episode
    title = episode.title.strip() or "The Daily Situation Brief"
    if mode == "short" and len(title) < 60:
        title = f"{title} | BYPASS Daily Brief"

    description = "\n\n".join(
        part
        for part in [
            episode.description.strip(),
            f"From BYPASS: The Daily Situation Brief for {episode.date}, hosted by Marcus Kade.",
            f"Full briefing, live map and archive: {SITE_URL}",
            " ".join(f"#{tag}" for tag in DEFAULT_HASHTAGS),
        ]
        if part
    )

    return {
        "title": title[:100],
        "description": description[:4900],
        "tags": DEFAULT_TAGS,
        "hashtags": DEFAULT_HASHTAGS,
    }


def resolve_account(nickname: str | None) -> dict:
    accounts = get_accounts("youtube")
    if not accounts:
        raise SystemExit(
            "No YouTube account configured. Add one via `python src/main.py` first."
        )
    if not nickname:
        return accounts[0]
    for account in accounts:
        if account.get("nickname", "").lower() == nickname.lower():
            return account
    raise SystemExit(
        f"No account named {nickname!r}. Have: "
        + ", ".join(a.get("nickname", "?") for a in accounts)
    )


def notify_webhook(url: str, narration: Narration, video_path: str, metadata: dict) -> None:
    payload = {
        "source": "money-printer-pro",
        "episode_date": narration.episode.date,
        "title": metadata["title"],
        "script_seconds": round(narration.duration, 1),
        "video_path": video_path,
        "video_id": metadata.get("_video_id", ""),
        "site": SITE_URL,
    }
    try:
        response = requests.post(url, json=payload, timeout=20)
        print(f"[webhook] {response.status_code}")
    except requests.RequestException as exc:
        print(f"[webhook] failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Episode date YYYY-MM-DD (default: latest)")
    parser.add_argument(
        "--mode",
        choices=["short", "long"],
        default="short",
        help="short = sentence-aligned ~55s cut; long = the full briefing",
    )
    parser.add_argument("--seconds", type=float, default=55.0, help="Target length in short mode")
    parser.add_argument("--account", help="YouTube account nickname to render under")
    parser.add_argument("--workdir", default=".bypass", help="Cache dir for audio/transcripts")
    parser.add_argument("--upload", action="store_true", help="Upload to YouTube via Selenium (needs a desktop Firefox profile)")
    parser.add_argument(
        "--upload-api",
        action="store_true",
        help="Upload via the YouTube Data API. This is the headless/CI path.",
    )
    parser.add_argument(
        "--privacy",
        choices=["private", "unlisted", "public"],
        default="private",
        help="Privacy for --upload-api. Defaults to private so an unattended run never publishes unreviewed.",
    )
    parser.add_argument("--crosspost", action="store_true", help="Cross-post via Post Bridge")
    parser.add_argument("--webhook", help="POST a JSON summary here when done")
    parser.add_argument("--dry-run", action="store_true", help="Prepare narration only, do not render")
    args = parser.parse_args()

    api_key = get_openai_api_key()
    if not api_key:
        raise SystemExit("Set openai_api_key in config.json to your AIMLAPI key.")

    try:
        narration = prepare_narration(
            workdir=args.workdir,
            api_key=api_key,
            date=args.date,
            mode=args.mode,
            target_seconds=args.seconds,
        )
    except BypassNarratorError as exc:
        raise SystemExit(f"Could not prepare narration: {exc}") from exc

    episode = narration.episode
    print(f"[episode] {episode.date} — {episode.title}")
    print(f"[audio]   {narration.audio_path}")
    print(f"[segment] {narration.start:.1f}s -> {narration.end:.1f}s ({narration.duration:.1f}s)")
    print(f"[script]  {len(narration.script)} chars\n{narration.script[:400]}\n")

    if args.dry_run:
        return 0

    metadata = build_metadata(narration, args.mode)
    account = resolve_account(args.account)

    youtube = YouTube(
        account["id"],
        account["nickname"],
        account["firefox_profile"],
        account.get("niche") or "geopolitics and energy security",
        account.get("language") or "English",
        account.get("dialect", ""),
        account.get("character_context", ""),
        account.get("is_for_kids"),
        open_browser=False,
    )

    tts = PrerecordedTTS(narration.audio_path, narration.start, narration.end)
    youtube.set_manual_metadata(metadata)
    video_path = youtube.generate_video_from_existing_script(
        tts, narration.script, subject=episode.subject
    )
    print(f"[video] {video_path}")

    if args.upload:
        try:
            youtube.open_browser = True
            uploaded = youtube.upload_video()
            print(f"[youtube] uploaded={uploaded}")
        except Exception as exc:  # noqa: BLE001 - upload is best-effort
            print(f"[youtube] upload failed: {exc}")

    video_id = ""
    if args.upload_api:
        try:
            from youtube_api_upload import upload_video as api_upload

            video_id = api_upload(
                video_path,
                title=metadata["title"],
                description=metadata["description"],
                tags=metadata["tags"],
                privacy_status=args.privacy,
            )
            print(f"[youtube-api] https://youtu.be/{video_id} ({args.privacy})")
        except Exception as exc:  # noqa: BLE001 - upload is best-effort
            print(f"[youtube-api] upload failed: {exc}")

    if args.crosspost:
        try:
            from post_bridge_integration import maybe_crosspost_youtube_short

            maybe_crosspost_youtube_short(video_path, metadata["title"], interactive=False)
            print("[postbridge] cross-post requested")
        except Exception as exc:  # noqa: BLE001
            print(f"[postbridge] failed: {exc}")

    if args.webhook:
        notify_webhook(args.webhook, narration, video_path, metadata)

    summary = {"episode": episode.date, "video": video_path, "video_id": video_id}
    step_summary = os.environ.get("GITHUB_OUTPUT")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"video_path={video_path}\n")
            handle.write(f"episode_date={episode.date}\n")
            handle.write(f"video_id={video_id}\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
