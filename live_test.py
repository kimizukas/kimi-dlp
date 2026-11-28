from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from kimi.extractor import InstagramExtractor, YouTubeExtractor, YouTubeOptions

# Termux shared storage paths.
ANDROID_DOWNLOAD = Path("/storage/emulated/0/Download")
YT_COOKIE_FILE = Path(os.getenv("YT_COOKIES", str(ANDROID_DOWNLOAD / "cookies.txt"))).expanduser()
IG_COOKIE_FILE = Path(os.getenv("IG_COOKIES", str(ANDROID_DOWNLOAD / "insta.txt"))).expanduser()
OUTPUT_DIR = ANDROID_DOWNLOAD / "kimi-dlp" / "downloads"

YT_OPTIONS = YouTubeOptions(
    metadata=True,
    formats=True,
    thumbnails=False,
    description=False,
    tags=False,
    chapters=False,
    captions=False,
    automatic_captions=False,
    watch_playlist=False,
    related_videos=False,
    raw_response=False,
    comments=False,
    hls=False,
    dash=False,
    retries=3,
    timeout=20.0,
)


def safe_filename(value: object, limit: int = 100) -> str:
    name = re.sub(r"[^\w.-]+", "_", str(value or "media"), flags=re.UNICODE)
    return name.strip("._")[:limit] or "media"


def platform_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    host = (parsed.hostname or "").lower().rstrip(".")

    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com") or host == "instagr.am" or host.endswith(".instagr.am"):
        return "instagram"
    raise ValueError(f"Unsupported website: {host or 'unknown host'}")


async def download_youtube(extractor: YouTubeExtractor, url: str) -> Path:
    print("[YouTube] Extracting...")
    data = await extractor.extract(url, options=YT_OPTIONS, refresh=True)

    video_id = data.get("id")
    if not video_id:
        raise RuntimeError("Extractor returned no YouTube video ID.")

    print("Title:", data.get("title") or video_id)
    formats = data.get("formats") or []
    candidates = [
        item for item in formats
        if item.get("itag") is not None
        and item.get("url")
        and item.get("is_direct")
        and item.get("has_audio")
        and item.get("has_video")
    ]
    if not candidates:
        summary = [
            {
                "itag": item.get("itag"),
                "quality": item.get("quality_label"),
                "audio": item.get("has_audio"),
                "video": item.get("has_video"),
                "direct": item.get("is_direct"),
            }
            for item in formats[:8]
        ]
        raise RuntimeError("No direct muxed format available: " + repr(summary))

    selected = max(
        candidates,
        key=lambda item: (int(item.get("height") or 0), int(item.get("bitrate") or 0)),
    )
    extension = safe_filename(selected.get("ext") or "mp4", 8)
    title = safe_filename(data.get("title") or video_id)
    destination = OUTPUT_DIR / f"{title}_{video_id}.{extension}"

    print("Format:", selected.get("quality_label") or "unknown", "| itag:", selected["itag"])
    await extractor.download_format(
        video_id,
        selected["itag"],
        destination,
        options=YT_OPTIONS,
        resume=True,
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("Download did not produce a non-empty file.")
    print(f"[SAVED] {destination} ({destination.stat().st_size} bytes)")
    return destination


async def download_instagram(extractor: InstagramExtractor, url: str) -> list[Path]:
    print("[Instagram] Extracting...")
    data = await extractor.extract(url)
    shortcode = data.get("shortcode") or data.get("id") or "instagram"
    media = data.get("media") or []
    if not media:
        raise RuntimeError("Instagram returned no downloadable media for this URL.")

    print("Type:", data.get("type"), "| media items:", len(media))
    paths = await extractor.download(
        data,
        output_dir=OUTPUT_DIR,
        filename_prefix=safe_filename(shortcode),
    )
    for path in paths:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"Download output missing or empty: {path}")
        print(f"[SAVED] {path} ({path.stat().st_size} bytes)")
    return paths


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("KIMI-DLP INTERACTIVE EXTRACT + DOWNLOAD")
    print("Paste one YouTube / Instagram URL and press Enter.")
    print("Each URL downloads automatically. Type q or exit to stop.")
    print("YouTube cookies :", YT_COOKIE_FILE, "| exists:", YT_COOKIE_FILE.is_file())
    print("Instagram cookies:", IG_COOKIE_FILE, "| exists:", IG_COOKIE_FILE.is_file())
    print("Save folder     :", OUTPUT_DIR)
    print("=" * 62)

    yt_cookie = str(YT_COOKIE_FILE) if YT_COOKIE_FILE.is_file() else None
    ig_cookie = str(IG_COOKIE_FILE) if IG_COOKIE_FILE.is_file() else None
    count = 0
    passed = 0
    failed = 0

    async with YouTubeExtractor(cookie_file=yt_cookie, timeout=20.0) as youtube, InstagramExtractor(
        cookie_file=ig_cookie,
        timeout=20.0,
        retries=3,
    ) as instagram:
        print("YouTube cookie stats:", youtube.cookie_stats())
        print("Instagram cookie stats:", instagram.cookie_stats())

        while True:
            try:
                url = input("\nKimi-DLP URL > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nStopping...")
                break

            if not url:
                continue
            if url.lower() in {"q", "quit", "exit", "stop"}:
                break

            count += 1
            try:
                platform = platform_for_url(url)
                if platform == "youtube":
                    await download_youtube(youtube, url)
                else:
                    await download_instagram(instagram, url)
                passed += 1
                print("[SUCCESS] Download completed.")
            except Exception as exc:
                failed += 1
                print(f"[FAILED] {type(exc).__name__}: {exc}")
                print("Paste another URL to continue.")

    print("\n" + "=" * 62)
    print(f"SESSION SUMMARY | Total: {count} | Passed: {passed} | Failed: {failed}")
    print("Downloads:", OUTPUT_DIR)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
