
from __future__ import annotations

import asyncio
import json
import re
import time

from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx


_BASE = "https://www.youtube.com"
_ANDROID_VERSION = "20.10.38"
_WEB_VERSION = "2.20261123.00.00"

_DEFAULT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")

_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')


class YouTubeExtractorError(Exception):
    """Base extractor error."""


class YouTubeUnavailable(YouTubeExtractorError):
    """Video or playlist is unavailable."""


class YouTubeRestricted(YouTubeExtractorError):
    """Authentication or verification is required."""


class YouTubeRequestError(YouTubeExtractorError):
    """Network or HTTP request failed."""


@dataclass(slots=True)
class YouTubeOptions:
    metadata: bool = True
    formats: bool = True
    thumbnails: bool = True
    description: bool = True
    tags: bool = True
    chapters: bool = True
    captions: bool = True

    automatic_captions: bool = False
    watch_playlist: bool = True
    related_videos: bool = False
    raw_response: bool = False

    comments: bool = False
    hls: bool = False
    dash: bool = False

    flat_playlist: bool = True
    playlist_limit: int = 100

    retries: int = 2
    timeout: float = 12.0

    def __post_init__(self):
        self.retries = max(0, min(int(self.retries), 5))
        self.playlist_limit = max(1, min(int(self.playlist_limit), 500))
        self.timeout = max(3.0, min(float(self.timeout), 60.0))


@dataclass(slots=True)
class CacheEntry:
    value: Any
    expires: float


class TTLCache:
    """Bounded LRU cache with TTL and concurrent request de-duplication."""

    def __init__(self, maxsize: int = 512, ttl: int = 21600):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(1, int(ttl))

        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

        self.hits = 0
        self.misses = 0
        self.expired = 0

    async def get(self, key: str):
        async with self._lock:
            item = self._data.get(key)

            if item is None:
                self.misses += 1
                return None

            if item.expires <= time.monotonic():
                self._data.pop(key, None)
                self.expired += 1
                self.misses += 1
                return None

            self._data.move_to_end(key)
            self.hits += 1
            return item.value

    async def put(self, key: str, value: Any, ttl: int | None = None):
        lifetime = self.ttl if ttl is None else max(1, int(ttl))

        async with self._lock:
            self._data[key] = CacheEntry(
                value=value,
                expires=time.monotonic() + lifetime,
            )
            self._data.move_to_end(key)

            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def _produce(self, key: str, factory, ttl: int | None):
        try:
            result = await factory()
            await self.put(key, result, ttl)
            return result
        finally:
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def get_or_create(self, key: str, factory, ttl: int | None = None):
        cached = await self.get(key)

        if cached is not None:
            return cached

        async with self._lock:
            task = self._inflight.get(key)

            if task is None:
                task = asyncio.create_task(
                    self._produce(key, factory, ttl)
                )
                self._inflight[key] = task

        return await asyncio.shield(task)

    async def clear(self):
        async with self._lock:
            self._data.clear()

    async def stats(self):
        async with self._lock:
            now = time.monotonic()

            return {
                "entries": sum(
                    1 for item in self._data.values()
                    if item.expires > now
                ),
                "inflight": len(self._inflight),
                "hits": self.hits,
                "misses": self.misses,
                "expired": self.expired,
                "maxsize": self.maxsize,
                "ttl": self.ttl,
            }


class YouTubeExtractor:
    """
    Lightweight asynchronous YouTube extractor.

    Metadata cache: 6 hours.
    Playlist cache: 6 hours.
    Stream URL cache: 5 minutes.

    Android Innertube is preferred for player requests.
    WEB is used as a fallback.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cookies: str | None = None,
        proxy: str | None = None,
        timeout: float = 12.0,
        cache_size: int = 512,
        metadata_ttl: int = 21600,
        stream_ttl: int = 300,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key or _DEFAULT_API_KEY
        self._external_client = client is not None

        if client is not None:
            self.client = client
        else:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                proxy=proxy,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 15) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Mobile Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
                cookies=self._load_cookies(cookies),
            )

        self.metadata_cache = TTLCache(cache_size, metadata_ttl)
        self.playlist_cache = TTLCache(
            max(32, cache_size // 4),
            metadata_ttl,
        )
        self.stream_cache = TTLCache(cache_size, stream_ttl)

        self._api_key = self.api_key
        self._client_version = _WEB_VERSION

        self._bootstrap_lock = asyncio.Lock()
        self._bootstrapped = False

    @staticmethod
    def _load_cookies(cookie_file: str | None):
        if not cookie_file:
            return None

        cookies = httpx.Cookies()

        try:
            with open(cookie_file, encoding="utf-8") as file:
                for line in file:
                    line = line.strip()

                    if not line or line.startswith("#"):
                        continue

                    parts = line.split("\t")

                    if len(parts) < 7:
                        continue

                    domain = parts[0].lstrip("#")
                    path = parts[2] or "/"
                    name = parts[5]
                    value = parts[6]

                    if name:
                        cookies.set(
                            name,
                            value,
                            domain=domain,
                            path=path,
                        )

        except OSError as exc:
            raise YouTubeExtractorError(
                f"Unable to read cookie file: {exc}"
            ) from exc

        return cookies

    async def close(self):
        if not self._external_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    @staticmethod
    def _retry_delay(response, attempt: int):
        retry_after = response.headers.get("Retry-After")

        if retry_after:
            try:
                return min(15.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    delay = date.timestamp() - time.time()
                    return min(15.0, max(0.0, delay))
                except (TypeError, ValueError, OverflowError):
                    pass

        return min(0.4 * (2 ** attempt), 3.0)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        options: YouTubeOptions | None = None,
        **kwargs,
    ):
        retries = options.retries if options else 2

        for attempt in range(retries + 1):
            try:
                response = await self.client.request(
                    method,
                    url,
                    **kwargs,
                )

                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < retries:
                        delay = self._retry_delay(response, attempt)
                        await response.aclose()
                        await asyncio.sleep(delay)
                        continue

                if response.status_code in (401, 403):
                    await response.aclose()
                    raise YouTubeRestricted(
                        "YouTube restricted this request. "
                        "Authentication or verification may be required."
                    )

                response.raise_for_status()
                return response

            except YouTubeExtractorError:
                raise

            except httpx.HTTPStatusError as exc:
                raise YouTubeRequestError(
                    f"YouTube HTTP error: {exc.response.status_code}"
                ) from exc

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= retries:
                    raise YouTubeRequestError(
                        f"YouTube network error: {exc}"
                    ) from exc

                await asyncio.sleep(min(0.3 * (2 ** attempt), 2.0))

        raise YouTubeRequestError("Request failed.")

    async def _bootstrap(self):
        if self._bootstrapped:
            return

        async with self._bootstrap_lock:
            if self._bootstrapped:
                return

            try:
                response = await self._request("GET", _BASE)

                try:
                    page = response.text
                finally:
                    await response.aclose()

                key = _API_KEY_RE.search(page)
                version = _VERSION_RE.search(page)

                if key:
                    self._api_key = key.group(1)

                if version:
                    self._client_version = version.group(1)

            except YouTubeExtractorError:
                pass

            self._bootstrapped = True

    @staticmethod
    def _player_payload(video_id, client_name, client_version):
        client = {
            "clientName": client_name,
            "clientVersion": client_version,
            "hl": "en",
            "gl": "US",
        }

        if client_name == "ANDROID":
            client["androidSdkVersion"] = 35

        return {
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "context": {"client": client},
        }

    async def _player_request(
        self,
        video_id: str,
        options: YouTubeOptions,
        client_name: str,
        client_version: str,
    ):
        payload = self._player_payload(
            video_id,
            client_name,
            client_version,
        )

        response = await self._request(
            "POST",
            f"{_BASE}/youtubei/v1/player",
            options=options,
            params={"key": self._api_key},
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": _BASE,
                "Referer": f"{_BASE}/",
            },
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise YouTubeRequestError(
                "Invalid player response."
            ) from exc
        finally:
            await response.aclose()

        if not isinstance(data, dict):
            raise YouTubeRequestError(
                "Unexpected player response."
            )

        return data

    async def _player(self, video_id, options):
        await self._bootstrap()

        android_data = None
        android_error = None

        try:
            android_data = await self._player_request(
                video_id,
                options,
                "ANDROID",
                _ANDROID_VERSION,
            )
        except YouTubeExtractorError as exc:
            android_error = exc

        if isinstance(android_data, dict):
            status = (
                android_data.get("playabilityStatus") or {}
            ).get("status")

            streaming = android_data.get("streamingData") or {}
            formats = (
                streaming.get("formats") or []
            ) + (
                streaming.get("adaptiveFormats") or []
            )

            if status in (None, "OK") and formats:
                return android_data

            if status in (None, "OK"):
                try:
                    web_data = await self._player_request(
                        video_id,
                        options,
                        "WEB",
                        self._client_version,
                    )

                    web_status = (
                        web_data.get("playabilityStatus") or {}
                    ).get("status")

                    web_streaming = web_data.get("streamingData") or {}
                    web_formats = (
                        web_streaming.get("formats") or []
                    ) + (
                        web_streaming.get("adaptiveFormats") or []
                    )

                    if web_status in (None, "OK") and web_formats:
                        return web_data

                except YouTubeExtractorError:
                    pass

                return android_data

        try:
            web_data = await self._player_request(
                video_id,
                options,
                "WEB",
                self._client_version,
            )

            web_status = (
                web_data.get("playabilityStatus") or {}
            ).get("status")

            if web_status in (None, "OK"):
                return web_data

            if android_data is None:
                return web_data

        except YouTubeExtractorError:
            if android_data is None:
                if android_error:
                    raise android_error
                raise

        if android_data is not None:
            return android_data

        raise YouTubeRequestError(
            "Unable to obtain YouTube player response."
        )

    @staticmethod
    def _video_id(value: str) -> str:
        if not isinstance(value, str):
            raise YouTubeExtractorError(
                "Video ID or URL must be a string."
            )

        value = value.strip()

        if _ID_RE.fullmatch(value):
            return value

        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise YouTubeExtractorError(
                "Invalid YouTube URL."
            ) from exc

        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtube-nocookie.com",
            "youtube-nocookie.com",
        }

        if host not in allowed_hosts:
            raise YouTubeExtractorError(
                "Invalid YouTube video URL or ID."
            )

        video_id = None

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]

        elif host in (
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        ):
            query = parse_qs(parsed.query)
            video_id = query.get("v", [None])[0]

            if not video_id:
                parts = [
                    part for part in parsed.path.split("/")
                    if part
                ]

                if len(parts) >= 2 and parts[0] in (
                    "shorts", "live", "embed", "v"
                ):
                    video_id = parts[1]

        else:
            parts = [
                part for part in parsed.path.split("/")
                if part
            ]

            if len(parts) >= 2 and parts[0] == "embed":
                video_id = parts[1]

        if not video_id or not _ID_RE.fullmatch(video_id):
            raise YouTubeExtractorError(
                "Invalid YouTube video URL or ID."
            )

        return video_id

    @staticmethod
    def _text(value: Any):
        if isinstance(value, str):
            return value or None

        if not isinstance(value, dict):
            return None

        simple = value.get("simpleText")

        if isinstance(simple, str):
            return simple or None

        runs = value.get("runs")

        if isinstance(runs, list):
            result = "".join(
                item.get("text", "")
                for item in runs
                if isinstance(item, dict)
                and isinstance(item.get("text", ""), str)
            ).strip()

            return result or None

        return None

    @staticmethod
    def _walk(data: Any):
        stack = [data]

        while stack:
            item = stack.pop()

            if isinstance(item, dict):
                yield item
                stack.extend(reversed(tuple(item.values())))

            elif isinstance(item, list):
                stack.extend(reversed(item))

    @staticmethod
    def _duration(value):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _thumbnails(data):
        if not isinstance(data, list):
            return []

        output = []

        for item in data:
            if not isinstance(item, dict):
                continue

            url = item.get("url")

            if not isinstance(url, str):
                continue

            output.append({
                "url": url,
                "width": item.get("width"),
                "height": item.get("height"),
            })

        return output

    @staticmethod
    def _codec_details(mime):
        match = re.search(
            r'codecs="([^"]+)"',
            mime or "",
            re.IGNORECASE,
        )

        codecs = [
            codec.strip().lower()
            for codec in (match.group(1) if match else "").split(",")
            if codec.strip()
        ]

        audio_prefixes = (
            "mp4a", "opus", "vorbis", "aac",
            "ac-3", "ec-3", "alac", "flac",
        )

        video_prefixes = (
            "avc", "av01", "vp8", "vp9",
            "vp09", "hev", "hvc",
        )

        audio_codec = next(
            (
                codec for codec in codecs
                if codec.startswith(audio_prefixes)
            ),
            None,
        )

        video_codec = next(
            (
                codec for codec in codecs
                if codec.startswith(video_prefixes)
            ),
            None,
        )

        return codecs, audio_codec, video_codec

    @staticmethod
    def _extension(mime):
        mime = (mime or "").lower().split(";")[0].strip()

        mapping = {
            "audio/webm": "webm",
            "video/webm": "webm",
            "audio/mp4": "m4a",
            "video/mp4": "mp4",
            "audio/mpeg": "mp3",
            "audio/ogg": "ogg",
            "video/3gpp": "3gp",
        }

        return mapping.get(mime, "bin")

    @staticmethod
    def _format_flags(item):
        mime = (item.get("mimeType") or "").lower()
        codecs, audio_codec, video_codec = (
            YouTubeExtractor._codec_details(mime)
        )

        has_audio = (
            mime.startswith("audio/")
            or audio_codec is not None
            or item.get("audioQuality") is not None
            or item.get("audioChannels") is not None
        )

        has_video = (
            mime.startswith("video/")
            or video_codec is not None
            or item.get("width") is not None
            or item.get("height") is not None
        )

        return (
            has_audio,
            has_video,
            codecs,
            audio_codec,
            video_codec,
        )

    @staticmethod
    def _formats(player):
        streaming = player.get("streamingData") or {}

        if not isinstance(streaming, dict):
            return []

        regular = streaming.get("formats") or []
        adaptive = streaming.get("adaptiveFormats") or []

        if not isinstance(regular, list):
            regular = []

        if not isinstance(adaptive, list):
            adaptive = []

        output = []
        seen = set()

        for item in regular + adaptive:
            if not isinstance(item, dict):
                continue

            itag = item.get("itag")
            mime = item.get("mimeType") or ""

            cipher = (
                item.get("signatureCipher")
                or item.get("cipher")
            )

            direct_url = item.get("url")
            cipher_data = {}

            if isinstance(cipher, str):
                cipher_data = {
                    key: values[0]
                    for key, values in parse_qs(
                        cipher,
                        keep_blank_values=True,
                    ).items()
                    if values
                }

                cipher_url = cipher_data.get("url")

                if cipher_url and not direct_url:
                    direct_url = cipher_url

                plain_signature = (
                    cipher_data.get("sig")
                    or cipher_data.get("signature")
                )

                if direct_url and plain_signature:
                    parsed = urlparse(direct_url)
                    query = parse_qs(
                        parsed.query,
                        keep_blank_values=True,
                    )

                    signature_name = (
                        cipher_data.get("sp") or "signature"
                    )

                    query[signature_name] = [plain_signature]

                    direct_url = urlunparse(
                        parsed._replace(
                            query=urlencode(query, doseq=True)
                        )
                    )

                if cipher_data.get("s"):
                    direct_url = None

            parsed_url = (
                urlparse(direct_url)
                if isinstance(direct_url, str)
                else None
            )

            query = (
                parse_qs(parsed_url.query)
                if parsed_url
                else {}
            )

            (
                has_audio,
                has_video,
                codecs,
                audio_codec,
                video_codec,
            ) = YouTubeExtractor._format_flags(item)

            requires_n = bool(query.get("n"))
            requires_signature = bool(cipher_data.get("s"))

            unique_key = (
                itag,
                mime,
                item.get("qualityLabel"),
                item.get("bitrate"),
            )

            if unique_key in seen:
                continue

            seen.add(unique_key)

            bitrate = item.get("bitrate")
            average_bitrate = item.get("averageBitrate")

            output.append({
                "format_id": str(itag) if itag is not None else None,
                "itag": itag,
                "ext": YouTubeExtractor._extension(mime),
                "mime_type": mime or None,
                "quality": item.get("quality"),
                "quality_label": item.get("qualityLabel"),
                "width": item.get("width"),
                "height": item.get("height"),
                "fps": item.get("fps"),
                "bitrate": bitrate,
                "abr": (
                    round(bitrate / 1000, 2)
                    if has_audio and bitrate else None
                ),
                "average_bitrate": average_bitrate,
                "audio_quality": item.get("audioQuality"),
                "audio_sample_rate": item.get("audioSampleRate"),
                "audio_channels": item.get("audioChannels"),
                "content_length": item.get("contentLength"),
                "approximate_content_length": item.get(
                    "approximateContentLength"
                ),
                "codecs": codecs,
                "audio_codec": audio_codec,
                "video_codec": video_codec,
                "has_audio": has_audio,
                "has_video": has_video,
                "url": direct_url,
                "is_direct": bool(
                    direct_url
                    and not requires_signature
                    and not requires_n
                ),
                "requires_signature": requires_signature,
                "requires_n_transform": requires_n,
                "signature_cipher": (
                    cipher if requires_signature else None
                ),
            })

        output.sort(
            key=lambda item: (
                bool(item["has_audio"] and item["has_video"]),
                bool(item["is_direct"]),
                item.get("height") or 0,
                item.get("bitrate") or 0,
            ),
            reverse=True,
        )

        return output

    @staticmethod
    def _captions(player, automatic):
        captions_data = player.get("captions") or {}

        if not isinstance(captions_data, dict):
            return []

        renderer = captions_data.get(
            "playerCaptionsTracklistRenderer"
        ) or {}

        tracks = renderer.get("captionTracks") or []

        if not isinstance(tracks, list):
            return []

        output = []

        for item in tracks:
            if not isinstance(item, dict):
                continue

            is_auto = item.get("kind") == "asr"

            if is_auto and not automatic:
                continue

            output.append({
                "language": item.get("languageCode"),
                "name": YouTubeExtractor._text(item.get("name")),
                "url": item.get("baseUrl"),
                "automatic": is_auto,
                "vss_id": item.get("vssId"),
            })

        return output

    @staticmethod
    def _parse_player(video_id, player, options):
        details = player.get("videoDetails") or {}
        status = player.get("playabilityStatus") or {}

        microformat = player.get("microformat") or {}
        micro = microformat.get("playerMicroformatRenderer") or {}

        if not isinstance(details, dict):
            details = {}

        if not isinstance(status, dict):
            status = {}

        status_code = status.get("status")

        if status_code not in (None, "OK"):
            reason = status.get("reason") or status_code or "Video unavailable."

            if status_code in (
                "LOGIN_REQUIRED",
                "AGE_CHECK_REQUIRED",
                "CONTENT_CHECK_REQUIRED",
                "AGE_VERIFICATION_REQUIRED",
            ):
                raise YouTubeRestricted(str(reason))

            raise YouTubeUnavailable(str(reason))

        thumbnail_data = details.get("thumbnail") or {}

        result = {
            "id": video_id,
            "title": details.get("title"),
            "url": f"{_BASE}/watch?v={video_id}",
            "author": details.get("author"),
            "channel_id": details.get("channelId"),
            "duration": YouTubeExtractor._duration(
                details.get("lengthSeconds")
            ),
            "view_count": YouTubeExtractor._duration(
                details.get("viewCount")
            ),
            "is_live": bool(details.get("isLiveContent")),
            "is_private": bool(details.get("isPrivate")),
            "is_unlisted": bool(details.get("isUnlisted")),
            "is_family_safe": details.get("isFamilySafe"),
            "category": micro.get("category"),
            "upload_date": micro.get("uploadDate"),
            "publish_date": micro.get("publishDate"),
            "owner_channel_url": micro.get("ownerProfileUrl"),
            "description": (
                details.get("shortDescription")
                if options.description else None
            ),
            "tags": (
                details.get("keywords") or []
                if options.tags else []
            ),
            "thumbnails": (
                YouTubeExtractor._thumbnails(
                    thumbnail_data.get("thumbnails")
                )
                if options.thumbnails else []
            ),
            "formats": (
                YouTubeExtractor._formats(player)
                if options.formats else []
            ),
            "captions": [],
            "automatic_captions": [],
            "chapters": [],
            "related_videos": [],
            "playlist": None,
            "playability_status": status_code,
            "playability_reason": status.get("reason"),
            "hls_manifest_url": None,
            "dash_manifest_url": None,
        }

        if options.captions:
            captions = YouTubeExtractor._captions(
                player,
                options.automatic_captions,
            )

            result["captions"] = [
                item for item in captions
                if not item["automatic"]
            ]

            if options.automatic_captions:
                result["automatic_captions"] = [
                    item for item in captions
                    if item["automatic"]
                ]

        if options.chapters:
            result["chapters"] = micro.get("chapters") or []

        streaming = player.get("streamingData") or {}

        if options.hls:
            result["hls_manifest_url"] = streaming.get("hlsManifestUrl")

        if options.dash:
            result["dash_manifest_url"] = streaming.get("dashManifestUrl")

        if options.raw_response:
            result["raw_response"] = player

        return result

    @staticmethod
    def _copy_result(value):
        return json.loads(json.dumps(value))

    @staticmethod
    def _full_options(options):
        return YouTubeOptions(
            metadata=True,
            formats=True,
            thumbnails=True,
            description=True,
            tags=True,
            chapters=True,
            captions=True,
            automatic_captions=True,
            watch_playlist=options.watch_playlist,
            related_videos=options.related_videos,
            raw_response=False,
            comments=options.comments,
            hls=options.hls,
            dash=options.dash,
            flat_playlist=options.flat_playlist,
            playlist_limit=options.playlist_limit,
            retries=options.retries,
            timeout=options.timeout,
        )

    async def _fetch_video(self, video_id, options):
        player = await self._player(video_id, options)
        full_options = self._full_options(options)

        result = self._parse_player(
            video_id,
            player,
            full_options,
        )

        await self.stream_cache.put(
            f"streams:{video_id}",
            result["formats"],
        )

        return result

    async def extract(
        self,
        value: str,
        *,
        options: YouTubeOptions | None = None,
        user_id: str | int | None = None,
        refresh: bool = False,
    ):
        options = options or YouTubeOptions()
        video_id = self._video_id(value)

        cache_key = f"video:{video_id}"

        async def fetch():
            return await self._fetch_video(video_id, options)

        if refresh:
            result = await fetch()
            await self.metadata_cache.put(cache_key, result)
        else:
            result = await self.metadata_cache.get_or_create(
                cache_key,
                fetch,
            )

        result = self._copy_result(result)

        if not options.metadata:
            for key in (
                "title",
                "author",
                "channel_id",
                "duration",
                "view_count",
            ):
                result[key] = None

        if not options.formats:
            result["formats"] = []
        else:
            stream_key = f"streams:{video_id}"
            streams = await self.stream_cache.get(stream_key)

            if streams is None:
                async def fetch_streams():
                    player = await self._player(video_id, options)
                    return self._formats(player)

                streams = await self.stream_cache.get_or_create(
                    stream_key,
                    fetch_streams,
                )

            result["formats"] = self._copy_result(streams)

        if not options.thumbnails:
            result["thumbnails"] = []

        if not options.description:
            result["description"] = None

        if not options.tags:
            result["tags"] = []

        if not options.captions:
            result["captions"] = []
            result["automatic_captions"] = []
        elif not options.automatic_captions:
            result["automatic_captions"] = []

        if not options.chapters:
            result["chapters"] = []

        if not options.hls:
            result["hls_manifest_url"] = None

        if not options.dash:
            result["dash_manifest_url"] = None

        if options.raw_response:
            player = await self._player(video_id, options)
            result["raw_response"] = player
        else:
            result.pop("raw_response", None)

        return result

    async def _fresh_format(self, video_id, itag, options):
        player = await self._player(video_id, options)

        selected = next(
            (
                item for item in self._formats(player)
                if item.get("itag") == itag
            ),
            None,
        )

        if not selected:
            raise YouTubeUnavailable(
                f"Format {itag} is unavailable."
            )

        if not selected.get("url") or not selected.get("is_direct"):
            raise YouTubeExtractorError(
                "This format requires signature or n-parameter "
                "deciphering and has no usable direct URL."
            )

        return selected

    async def download_format(
        self,
        video_id: str,
        itag: int,
        output: str | Path,
        *,
        options: YouTubeOptions | None = None,
        chunk_size: int = 512 * 1024,
        resume: bool = True,
        max_url_refreshes: int = 3,
    ):
        """
        Download a direct format using bounded HTTP Range requests.

        On 403, fetches a fresh player response and retries the same offset.
        A matching .part file can be resumed on a later invocation.
        The final file is published only after size verification.
        """
        video_id = self._video_id(video_id)
        options = options or YouTubeOptions()

        try:
            itag = int(itag)
            chunk_size = max(64 * 1024, min(int(chunk_size), 2 * 1024 * 1024))
            max_url_refreshes = max(1, min(int(max_url_refreshes), 5))
        except (TypeError, ValueError) as exc:
            raise YouTubeExtractorError(
                "Invalid download parameters."
            ) from exc

        target = Path(output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary = target.with_name(target.name + ".part")
        metadata_file = target.with_name(target.name + ".part.json")

        identity = {
            "video_id": video_id,
            "itag": itag,
        }

        expected_total = None
        downloaded = 0

        # Resume only a partial file belonging to this exact video/itag.
        if resume and temporary.is_file() and metadata_file.is_file():
            try:
                saved = json.loads(metadata_file.read_text("utf-8"))

                if saved == identity:
                    downloaded = temporary.stat().st_size
                    expected_total = saved.get("total")
            except (OSError, ValueError, TypeError):
                downloaded = 0
                expected_total = None

        if downloaded == 0:
            temporary.unlink(missing_ok=True)

            metadata_file.write_text(
                json.dumps(identity),
                encoding="utf-8",
            )

        if expected_total is not None and downloaded > expected_total:
            temporary.unlink(missing_ok=True)
            downloaded = 0
            expected_total = None

        current_format = None
        refreshes = 0

        try:
            current_format = await self._fresh_format(
                video_id,
                itag,
                options,
            )

            while True:
                if expected_total is not None and downloaded == expected_total:
                    break

                end = downloaded + chunk_size - 1

                request_headers = {
                    "Range": f"bytes={downloaded}-{end}",
                    "Accept-Encoding": "identity",
                    "Referer": f"{_BASE}/watch?v={video_id}",
                    "Origin": _BASE,
                }

                retry_with_fresh_url = False

                async with self.client.stream(
                    "GET",
                    current_format["url"],
                    headers=request_headers,
                    follow_redirects=True,
                    timeout=httpx.Timeout(
                        options.timeout,
                        read=max(60.0, options.timeout),
                    ),
                ) as response:

                    if response.status_code == 403:
                        retry_with_fresh_url = True
                        await response.aread()

                    elif response.status_code in (
                        429, 500, 502, 503, 504
                    ):
                        delay = self._retry_delay(response, 0)
                        await response.aread()
                        await asyncio.sleep(delay)
                        retry_with_fresh_url = True

                    elif response.status_code == 200:
                        # Server ignored Range. Only safe to restart from byte 0.
                        if downloaded:
                            await response.aread()
                            temporary.unlink(missing_ok=True)
                            downloaded = 0
                            expected_total = None
                            retry_with_fresh_url = True
                        else:
                            content_length = response.headers.get(
                                "Content-Length"
                            )

                            try:
                                expected_total = (
                                    int(content_length)
                                    if content_length else None
                                )
                            except ValueError:
                                expected_total = None

                            with temporary.open("wb") as file:
                                async for block in response.aiter_raw(
                                    chunk_size=chunk_size
                                ):
                                    if block:
                                        file.write(block)
                                        downloaded += len(block)

                    elif response.status_code == 206:
                        content_range = response.headers.get(
                            "Content-Range", ""
                        )

                        match = re.fullmatch(
                            r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                            content_range.strip(),
                            re.IGNORECASE,
                        )

                        if not match:
                            raise YouTubeRequestError(
                                "Invalid Content-Range from media server."
                            )

                        range_start = int(match.group(1))
                        range_end = int(match.group(2))
                        total_text = match.group(3)

                        if range_start != downloaded:
                            raise YouTubeRequestError(
                                "Media server returned an unexpected "
                                "range offset."
                            )

                        if range_end < range_start:
                            raise YouTubeRequestError(
                                "Media server returned an invalid range."
                            )

                        if total_text != "*":
                            response_total = int(total_text)

                            if (
                                expected_total is not None
                                and expected_total != response_total
                            ):
                                raise YouTubeRequestError(
                                    "Media size changed during download."
                                )

                            expected_total = response_total

                        expected_chunk_size = (
                            range_end - range_start + 1
                        )

                        block_data = await response.aread()

                        if len(block_data) != expected_chunk_size:
                            raise YouTubeRequestError(
                                "Incomplete media chunk received."
                            )

                        if (
                            expected_total is not None
                            and range_end >= expected_total
                        ):
                            raise YouTubeRequestError(
                                "Media range exceeds total file size."
                            )

                        with temporary.open("ab") as file:
                            file.write(block_data)

                        downloaded += len(block_data)

                    else:
                        raise YouTubeRequestError(
                            f"Media server returned HTTP "
                            f"{response.status_code}."
                        )

                if retry_with_fresh_url:
                    refreshes += 1

                    if refreshes > max_url_refreshes:
                        raise YouTubeRequestError(
                            "Media URL refresh limit reached. "
                            "YouTube continues rejecting the request."
                        )

                    current_format = await self._fresh_format(
                        video_id,
                        itag,
                        options,
                    )

                    continue

                if expected_total is None:
                    raise YouTubeRequestError(
                        "Unable to determine the complete media size."
                    )

                if downloaded > expected_total:
                    raise YouTubeRequestError(
                        "Downloaded data exceeds expected media size."
                    )

                if downloaded == 0:
                    raise YouTubeRequestError(
                        "YouTube returned an empty media response."
                    )

            if not temporary.is_file():
                raise YouTubeRequestError(
                    "Temporary download file was not created."
                )

            actual_size = temporary.stat().st_size

            if actual_size != expected_total:
                raise YouTubeRequestError(
                    f"Incomplete download: {actual_size}/"
                    f"{expected_total} bytes."
                )

            temporary.replace(target)
            metadata_file.unlink(missing_ok=True)

            return {
                "id": video_id,
                "itag": itag,
                "format_id": str(itag),
                "path": str(target),
                "size": actual_size,
                "mime_type": current_format.get("mime_type"),
                "ext": current_format.get("ext"),
                "bitrate": current_format.get("bitrate"),
                "success": True,
            }

        except YouTubeExtractorError:
            raise

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise YouTubeRequestError(
                f"Media network error: {type(exc).__name__}"
            ) from exc

        except OSError as exc:
            raise YouTubeRequestError(
                f"Unable to save media file: {exc}"
            ) from exc

    @staticmethod
    def _extract_json_assignment(page, variable):
        if not isinstance(page, str):
            return None

        pattern = re.compile(
            rf"(?:var\s+|let\s+|const\s+)?{re.escape(variable)}\s*="
        )

        match = pattern.search(page)

        if not match:
            return None

        start = page.find("{", match.end())

        if start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(page)):
            char = page[index]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False

                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1

                if depth == 0:
                    try:
                        return json.loads(page[start:index + 1])
                    except ValueError:
                        return None

        return None

    @staticmethod
    def _playlist_items(data):
        output = []
        seen = set()

        for node in YouTubeExtractor._walk(data):
            renderer = (
                node.get("playlistVideoRenderer")
                or node.get("playlistPanelVideoRenderer")
            )

            if not isinstance(renderer, dict):
                continue

            video_id = renderer.get("videoId")

            if (
                not isinstance(video_id, str)
                or not _ID_RE.fullmatch(video_id)
                or video_id in seen
            ):
                continue

            seen.add(video_id)

            output.append({
                "id": video_id,
                "title": YouTubeExtractor._text(
                    renderer.get("title")
                ),
                "url": f"{_BASE}/watch?v={video_id}",
                "thumbnail": YouTubeExtractor._thumbnails(
                    (renderer.get("thumbnail") or {}).get("thumbnails")
                ),
                "duration": YouTubeExtractor._text(
                    renderer.get("lengthText")
                ),
                "index": YouTubeExtractor._text(
                    renderer.get("index")
                ),
                "is_playable": renderer.get("isPlayable"),
                "short_byline": YouTubeExtractor._text(
                    renderer.get("shortBylineText")
                ),
            })

        return output

    @staticmethod
    def _continuation(data):
        for node in YouTubeExtractor._walk(data):
            endpoint = node.get("continuationEndpoint")

            if isinstance(endpoint, dict):
                command = endpoint.get("continuationCommand") or {}
                token = command.get("token")

                if token:
                    return token

            item = node.get("continuationItemRenderer")

            if isinstance(item, dict):
                endpoint = item.get("continuationEndpoint") or {}
                command = endpoint.get("continuationCommand") or {}
                token = command.get("token")

                if token:
                    return token

        return None

    @staticmethod
    def _playlist_metadata(data):
        for node in YouTubeExtractor._walk(data):
            renderer = node.get("playlistMetadataRenderer")

            if isinstance(renderer, dict):
                return {
                    "title": renderer.get("title"),
                    "description": renderer.get("description"),
                }

        return {"title": None, "description": None}

    async def _browse_continuation(self, continuation, options):
        await self._bootstrap()

        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": self._client_version,
                    "hl": "en",
                    "gl": "US",
                }
            },
            "continuation": continuation,
        }

        response = await self._request(
            "POST",
            f"{_BASE}/youtubei/v1/browse",
            options=options,
            params={"key": self._api_key},
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": _BASE,
                "Referer": f"{_BASE}/",
            },
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise YouTubeRequestError(
                "Invalid playlist continuation response."
            ) from exc
        finally:
            await response.aclose()

        if not isinstance(data, dict):
            raise YouTubeRequestError(
                "Unexpected playlist continuation response."
            )

        return data

    async def get_playlist(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        flat: bool = True,
        options: YouTubeOptions | None = None,
        refresh: bool = False,
    ):
        options = options or YouTubeOptions()

        if (
            not isinstance(playlist_id, str)
            or not _PLAYLIST_RE.fullmatch(playlist_id)
        ):
            raise YouTubeExtractorError(
                "Invalid YouTube playlist ID."
            )

        limit = max(1, min(int(limit), options.playlist_limit, 500))

        cache_key = f"playlist:{playlist_id}:{limit}:{bool(flat)}"

        async def fetch():
            response = await self._request(
                "GET",
                f"{_BASE}/playlist",
                options=options,
                params={"list": playlist_id},
            )

            try:
                data = self._extract_json_assignment(
                    response.text,
                    "ytInitialData",
                )
            finally:
                await response.aclose()

            if not isinstance(data, dict):
                raise YouTubeRequestError(
                    "Could not parse YouTube playlist page."
                )

            metadata = self._playlist_metadata(data)
            items = self._playlist_items(data)
            token = self._continuation(data)

            pages = 0

            while token and len(items) < limit and pages < 20:
                pages += 1

                next_data = await self._browse_continuation(
                    token,
                    options,
                )

                next_items = self._playlist_items(next_data)
                known = {item["id"] for item in items}

                for item in next_items:
                    if item["id"] not in known:
                        items.append(item)
                        known.add(item["id"])

                        if len(items) >= limit:
                            break

                token = self._continuation(next_data)

                if not next_items:
                    break

            return {
                "id": playlist_id,
                "title": metadata["title"],
                "description": metadata["description"],
                "url": f"{_BASE}/playlist?list={playlist_id}",
                "video_count": None,
                "items": items[:limit],
                "continuation": token,
                "has_more": bool(token),
                "flat": bool(flat),
            }

        if refresh:
            result = await fetch()
            await self.playlist_cache.put(cache_key, result)
            return self._copy_result(result)

        result = await self.playlist_cache.get_or_create(
            cache_key,
            fetch,
        )

        return self._copy_result(result)

    async def get_playlist_next(
        self,
        playlist_id: str,
        continuation: str,
        *,
        limit: int = 100,
        options: YouTubeOptions | None = None,
    ):
        options = options or YouTubeOptions()

        if (
            not isinstance(playlist_id, str)
            or not _PLAYLIST_RE.fullmatch(playlist_id)
        ):
            raise YouTubeExtractorError(
                "Invalid YouTube playlist ID."
            )

        if not isinstance(continuation, str) or not continuation:
            raise YouTubeExtractorError(
                "A valid continuation token is required."
            )

        limit = max(1, min(int(limit), options.playlist_limit, 500))

        data = await self._browse_continuation(
            continuation,
            options,
        )

        items = self._playlist_items(data)
        next_token = self._continuation(data)

        return {
            "id": playlist_id,
            "items": items[:limit],
            "continuation": next_token,
            "has_more": bool(next_token),
        }

    async def cache_stats(self):
        return {
            "metadata": await self.metadata_cache.stats(),
            "playlists": await self.playlist_cache.stats(),
            "streams": await self.stream_cache.stats(),
        }

    async def clear_cache(self):
        await asyncio.gather(
            self.metadata_cache.clear(),
            self.playlist_cache.clear(),
            self.stream_cache.clear(),
        )


__all__ = [
    "YouTubeExtractor",
    "YouTubeExtractorError",
    "YouTubeUnavailable",
    "YouTubeRestricted",
    "YouTubeRequestError",
    "YouTubeOptions",
    "TTLCache",
]
