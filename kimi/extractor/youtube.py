from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from ..cookies import CookieStore
from ..network import RETRYABLE_STATUS_CODES, RetryPolicy

# Optional compiled accelerator. No extra fallback file is required.
try:
    from ._youtube_fast import (
        walk_dicts as _fast_walk,
        text_value as _fast_text,
        parse_content_range as _fast_content_range,
        parse_unsatisfied_range as _fast_unsatisfied_range,
    )
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False

    def _fast_walk(data):
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                stack.extend(reversed(tuple(item.values())))
            elif isinstance(item, list):
                stack.extend(reversed(item))

    def _fast_text(value):
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
                x.get("text", "") for x in runs
                if isinstance(x, dict) and isinstance(x.get("text", ""), str)
            ).strip()
            return result or None
        return None

    def _fast_content_range(value):
        if not isinstance(value, str):
            return None
        m = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
        if not m:
            return None
        start, end = int(m.group(1)), int(m.group(2))
        total = None if m.group(3) == "*" else int(m.group(3))
        if start < 0 or end < start or (total is not None and (total <= 0 or end >= total)):
            return None
        return start, end, total

    def _fast_unsatisfied_range(value):
        if not isinstance(value, str):
            return None
        m = re.fullmatch(r"bytes\s+\*/(\d+)", value.strip(), re.I)
        return int(m.group(1)) if m else None


_BASE = "https://www.youtube.com"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')

# Versions are deliberately configurable. YouTube can invalidate client versions.
_ANDROID_VERSION = "20.10.38"
_WEB_VERSION = "2.20261123.00.00"
_IOS_VERSION = "20.10.4"
_TV_VERSION = "7.20250219.00.00"

_ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 15) gzip"
_WEB_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_IOS_UA = "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_0 like Mac OS X)"
_TV_UA = "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.5) AppleWebKit/537.36"

# Client order is intentional: mobile clients first, then browser/TV fallbacks.
_CLIENTS = (
    {"name": "ANDROID", "version": _ANDROID_VERSION, "ua": _ANDROID_UA,
     "client_id": 3, "platform": "MOBILE", "sdk": 35},
    {"name": "IOS", "version": _IOS_VERSION, "ua": _IOS_UA,
     "client_id": 5, "platform": "MOBILE"},
    {"name": "WEB", "version": _WEB_VERSION, "ua": _WEB_UA,
     "client_id": 1, "platform": "DESKTOP"},
    {"name": "TVHTML5", "version": _TV_VERSION, "ua": _TV_UA,
     "client_id": 7, "platform": "TV"},
)

_DEFAULT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


class YouTubeExtractorError(Exception):
    """Base extractor exception."""


class YouTubeUnavailable(YouTubeExtractorError):
    """Video or playlist is unavailable."""


class YouTubeRestricted(YouTubeExtractorError):
    """YouTube requires authentication, verification, or a token."""


class YouTubeRequestError(YouTubeExtractorError):
    """Network, HTTP, or response parsing failure."""


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
class _CacheEntry:
    value: Any
    expires: float


class TTLCache:
    """Bounded async LRU cache with TTL and in-flight de-duplication."""

    def __init__(self, maxsize=512, ttl=21600):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(1, int(ttl))
        self._data: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self.hits = self.misses = self.expired = 0

    async def get(self, key):
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

    async def put(self, key, value, ttl=None):
        lifetime = self.ttl if ttl is None else max(1, int(ttl))
        async with self._lock:
            self._data[key] = _CacheEntry(value, time.monotonic() + lifetime)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def get_or_create(self, key, factory, ttl=None):
        cached = await self.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._produce(key, factory, ttl))
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _produce(self, key, factory, ttl):
        try:
            value = await factory()
            await self.put(key, value, ttl)
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._data.clear()

    async def stats(self):
        async with self._lock:
            now = time.monotonic()
            return {
                "entries": sum(x.expires > now for x in self._data.values()),
                "inflight": len(self._inflight),
                "hits": self.hits, "misses": self.misses,
                "expired": self.expired, "maxsize": self.maxsize,
                "ttl": self.ttl,
            }


class YouTubeExtractor:
    """
    Native async YouTube Innertube extractor.

    PO token provider contract:
      async_or_sync_provider(video_id, client_name, user_id) -> str | dict | None

    A string is sent as serviceIntegrityDimensions.poToken in the Innertube
    context. A dict may contain {"po_token": "...", "visitor_data": "..."}.
    Token placement/support varies by client and YouTube rollout; this is an
    extension point, not a promise that every token works for every client.

    Cookies may be a Netscape cookie file path, raw Netscape text, a mapping,
    or httpx.Cookies. Do not share one extractor between unrelated users when
    using user-specific cookies; create a separate instance/profile per user.
    """

    def __init__(
        self, *, api_key=None, cookies=None, proxy=None, timeout=12.0,
        cache_size=512, metadata_ttl=21600, stream_ttl=300,
        client: httpx.AsyncClient | None = None,
        po_tokens: dict | None = None,
        po_token_provider: Callable | None = None,
        client_order: tuple[str, ...] | None = None,
        visitor_data: str | None = None,
        cookie_file: str | Path | None = None,
    ):
        self.api_key = api_key or _DEFAULT_API_KEY
        self._api_key = self.api_key
        self.timeout = max(3.0, min(float(timeout), 60.0))
        self._external_client = client is not None
        self.cookie_store = CookieStore(
            default_domain=".youtube.com", path=cookie_file
        )
        if cookie_file is not None:
            cookie_path = Path(cookie_file).expanduser()
            if cookie_path.exists() and not cookie_path.is_file():
                raise ValueError(f"Cookie path is not a file: {cookie_path}")
            if cookie_path.is_file():
                self.cookie_store.load(cookie_path)
        if cookies is not None:
            self.cookie_store.load(cookies)
        self._cookies = self.cookie_store.cookies
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            proxy=proxy,
            limits=httpx.Limits(
                max_connections=20, max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
            headers={"User-Agent": _WEB_UA, "Accept-Language": "en-US,en;q=0.9"},
            cookies=self._cookies,
        )
        self.cookie_store.bind(self.client.cookies)
        self._closed = False
        self.po_tokens = dict(po_tokens or {})
        self.po_token_provider = po_token_provider
        self.visitor_data = visitor_data
        self.client_order = tuple(client_order or ("ANDROID", "IOS", "WEB", "TVHTML5"))
        self._client_map = {x["name"]: x for x in _CLIENTS}
        unknown = set(self.client_order) - set(self._client_map)
        if unknown:
            raise ValueError(f"Unknown YouTube clients: {', '.join(sorted(unknown))}")

        self.metadata_cache = TTLCache(cache_size, metadata_ttl)
        self.playlist_cache = TTLCache(max(32, cache_size // 4), metadata_ttl)
        self.stream_cache = TTLCache(cache_size, stream_ttl)
        self._bootstrap_lock = asyncio.Lock()
        self._bootstrapped = False
        self._request_semaphore = asyncio.Semaphore(8)
        self._download_semaphore = asyncio.Semaphore(4)
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._download_locks_guard = asyncio.Lock()

    @staticmethod
    def _load_cookies(source):
        """Backward-compatible cookie parser returning ``httpx.Cookies``."""
        if source is None:
            return None
        return CookieStore(source, default_domain=".youtube.com").cookies

    def save_cookies(self, path: str | Path | None = None) -> Path:
        """Persist the active cookie jar in Netscape format with private permissions."""
        return self.cookie_store.save(path)

    def cookie_stats(self) -> dict:
        return self.cookie_store.stats()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.cookie_store.path is not None:
                self.cookie_store.save()
        finally:
            if not self._external_client:
                await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    @staticmethod
    def _retry_delay(response, attempt):
        return RetryPolicy(base_delay=0.4, max_delay=20.0, jitter=0.0).delay(
            response, attempt
        )

    async def _request(self, method, url, *, options=None, retry_status=True, **kwargs):
        retries = options.retries if options else 2
        async with self._request_semaphore:
            for attempt in range(retries + 1):
                response = None
                try:
                    response = await self.client.request(method, url, **kwargs)
                    if RetryPolicy(retries=retries).should_retry(response.status_code) and retry_status and attempt < retries:
                        delay = self._retry_delay(response, attempt)
                        await response.aclose()
                        await asyncio.sleep(delay)
                        continue
                    if response.status_code in (401, 403):
                        status = response.status_code
                        await response.aclose()
                        raise YouTubeRestricted(f"YouTube rejected the request with HTTP {status}.")
                    response.raise_for_status()
                    return response
                except YouTubeExtractorError:
                    raise
                except httpx.HTTPStatusError as exc:
                    raise YouTubeRequestError(f"YouTube HTTP error: {exc.response.status_code}") from exc
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= retries:
                        raise YouTubeRequestError(f"YouTube network error: {type(exc).__name__}") from exc
                    await asyncio.sleep(min(0.3 * (2 ** attempt), 2.0))
                finally:
                    if response is not None and not response.is_closed and response.status_code >= 400:
                        await response.aclose()
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
                m = _API_KEY_RE.search(page)
                v = _VERSION_RE.search(page)
                if m:
                    self._api_key = m.group(1)
                if v:
                    self._client_map["WEB"]["version"] = v.group(1)
            except YouTubeExtractorError:
                pass
            self._bootstrapped = True

    async def _get_po_token(self, video_id, client_name, user_id):
        token_data = self.po_tokens.get(client_name) or self.po_tokens.get("*")
        if self.po_token_provider is not None:
            result = self.po_token_provider(video_id, client_name, user_id)
            if inspect.isawaitable(result):
                result = await result
            if result:
                token_data = result
        if isinstance(token_data, str):
            return {"po_token": token_data}
        return token_data if isinstance(token_data, dict) else {}

    async def _player_payload(self, video_id, client, user_id=None):
        c = {
            "clientName": client["name"],
            "clientVersion": client["version"],
            "hl": "en", "gl": "US",
        }
        if client.get("sdk"):
            c["androidSdkVersion"] = client["sdk"]
        if self.visitor_data:
            c["visitorData"] = self.visitor_data
        token_data = await self._get_po_token(video_id, client["name"], user_id)
        context = {"client": c}
        po_token = token_data.get("po_token")
        if po_token:
            context["serviceIntegrityDimensions"] = {"poToken": po_token}
        payload = {
            "videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
            "context": context,
        }
        return payload, token_data

    async def _player_request(self, video_id, options, client_name, user_id=None):
        client = self._client_map[client_name]
        payload, token_data = await self._player_payload(video_id, client, user_id)
        headers = {
            "User-Agent": client["ua"], "Content-Type": "application/json",
            "Origin": _BASE, "Referer": f"{_BASE}/",
        }
        visitor = token_data.get("visitor_data") or self.visitor_data
        if visitor:
            headers["X-Goog-Visitor-Id"] = visitor
        response = await self._request(
            "POST", f"{_BASE}/youtubei/v1/player", options=options,
            params={"key": self._api_key}, json=payload, headers=headers,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise YouTubeRequestError("Invalid player response.") from exc
        finally:
            await response.aclose()
        if not isinstance(data, dict):
            raise YouTubeRequestError("Unexpected player response.")
        data["_extractor_client"] = client_name
        data["_extractor_user_agent"] = client["ua"]
        data["_extractor_token_used"] = bool(token_data.get("po_token"))
        return data

    @staticmethod
    def _player_status(data):
        return (data.get("playabilityStatus") or {}).get("status")

    @staticmethod
    def _player_formats(data):
        streaming = data.get("streamingData") or {}
        if not isinstance(streaming, dict):
            return []
        return (streaming.get("formats") or []) + (streaming.get("adaptiveFormats") or [])

    async def _player(self, video_id, options, user_id=None, preferred=None):
        await self._bootstrap()
        order = list(self.client_order)
        if preferred in order:
            order.remove(preferred)
            order.insert(0, preferred)
        errors, restricted = [], None
        for name in order:
            try:
                data = await self._player_request(video_id, options, name, user_id)
            except YouTubeExtractorError as exc:
                errors.append(exc)
                continue
            status = self._player_status(data)
            formats = self._player_formats(data)
            if status in (None, "OK") and formats:
                return data
            reason = (data.get("playabilityStatus") or {}).get("reason") or status
            if status in ("LOGIN_REQUIRED", "AGE_CHECK_REQUIRED", "CONTENT_CHECK_REQUIRED",
                          "AGE_VERIFICATION_REQUIRED", "GEO_RESTRICTED"):
                restricted = reason or status
            errors.append(YouTubeUnavailable(str(reason or f"{name} returned no usable formats.")))
        if restricted:
            raise YouTubeRestricted(str(restricted))
        if errors:
            raise errors[-1]
        raise YouTubeRequestError("All configured YouTube player clients failed.")

    @staticmethod
    def _video_id(value):
        if not isinstance(value, str):
            raise YouTubeExtractorError("Video ID or URL must be a string.")
        value = value.strip()
        if _ID_RE.fullmatch(value):
            return value
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise YouTubeExtractorError("Invalid YouTube URL.") from exc
        host = (parsed.hostname or "").lower()
        allowed = {"youtube.com", "www.youtube.com", "m.youtube.com",
                   "music.youtube.com", "youtu.be", "www.youtube-nocookie.com",
                   "youtube-nocookie.com"}
        if host not in allowed:
            raise YouTubeExtractorError("Invalid YouTube video URL or ID.")
        video_id = None
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            if not video_id:
                parts = [x for x in parsed.path.split("/") if x]
                if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
                    video_id = parts[1]
        else:
            parts = [x for x in parsed.path.split("/") if x]
            if len(parts) >= 2 and parts[0] == "embed":
                video_id = parts[1]
        if not video_id or not _ID_RE.fullmatch(video_id):
            raise YouTubeExtractorError("Invalid YouTube video URL or ID.")
        return video_id

    _text = staticmethod(_fast_text)
    _walk = staticmethod(_fast_walk)

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
        return [
            {"url": x.get("url"), "width": x.get("width"), "height": x.get("height")}
            for x in data if isinstance(x, dict) and isinstance(x.get("url"), str)
        ]

    @staticmethod
    def _codec_details(mime):
        m = re.search(r'codecs="([^"]+)"', mime or "", re.I)
        codecs = [x.strip().lower() for x in (m.group(1) if m else "").split(",") if x.strip()]
        audio_prefixes = ("mp4a", "opus", "vorbis", "aac", "ac-3", "ec-3", "alac", "flac")
        video_prefixes = ("avc", "av01", "vp8", "vp9", "vp09", "hev", "hvc")
        return (
            codecs,
            next((x for x in codecs if x.startswith(audio_prefixes)), None),
            next((x for x in codecs if x.startswith(video_prefixes)), None),
        )

    @staticmethod
    def _extension(mime):
        mime = (mime or "").lower().split(";")[0].strip()
        return {
            "audio/webm": "webm", "video/webm": "webm",
            "audio/mp4": "m4a", "video/mp4": "mp4",
            "audio/mpeg": "mp3", "audio/ogg": "ogg",
            "video/3gpp": "3gp",
        }.get(mime, "bin")

    @staticmethod
    def _formats(player):
        streaming = player.get("streamingData") or {}
        if not isinstance(streaming, dict):
            return []
        regular, adaptive = streaming.get("formats") or [], streaming.get("adaptiveFormats") or []
        output, seen = [], set()
        for item in list(regular) + list(adaptive):
            if not isinstance(item, dict):
                continue
            itag = item.get("itag")
            mime = item.get("mimeType") or ""
            cipher = item.get("signatureCipher") or item.get("cipher")
            cipher_data = {}
            direct_url = item.get("url")
            if isinstance(cipher, str):
                cipher_data = {k: v[0] for k, v in parse_qs(cipher, keep_blank_values=True).items() if v}
                direct_url = direct_url or cipher_data.get("url")
                signature = cipher_data.get("sig") or cipher_data.get("signature")
                if direct_url and signature:
                    parsed = urlparse(direct_url)
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    query[cipher_data.get("sp") or "signature"] = [signature]
                    direct_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
                if cipher_data.get("s"):
                    direct_url = None
            parsed_url = urlparse(direct_url) if isinstance(direct_url, str) else None
            query = parse_qs(parsed_url.query) if parsed_url else {}
            codecs, audio_codec, video_codec = YouTubeExtractor._codec_details(mime)
            has_audio = mime.startswith("audio/") or audio_codec is not None or any(
                item.get(k) is not None for k in ("audioQuality", "audioChannels")
            )
            has_video = mime.startswith("video/") or video_codec is not None or any(
                item.get(k) is not None for k in ("width", "height")
            )
            requires_s, requires_n = bool(cipher_data.get("s")), bool(query.get("n"))
            unique = (itag, mime, item.get("qualityLabel"), item.get("bitrate"))
            if unique in seen:
                continue
            seen.add(unique)
            bitrate = item.get("bitrate")
            try:
                abr = round(bitrate / 1000, 2) if has_audio and bitrate else None
            except (TypeError, ValueError):
                abr = None
            output.append({
                "format_id": str(itag) if itag is not None else None,
                "itag": itag, "ext": YouTubeExtractor._extension(mime),
                "mime_type": mime or None, "quality": item.get("quality"),
                "quality_label": item.get("qualityLabel"),
                "width": item.get("width"), "height": item.get("height"),
                "fps": item.get("fps"), "bitrate": bitrate, "abr": abr,
                "average_bitrate": item.get("averageBitrate"),
                "audio_quality": item.get("audioQuality"),
                "audio_sample_rate": item.get("audioSampleRate"),
                "audio_channels": item.get("audioChannels"),
                "content_length": item.get("contentLength"),
                "approximate_content_length": item.get("approximateContentLength"),
                "codecs": codecs, "audio_codec": audio_codec,
                "video_codec": video_codec, "has_audio": has_audio,
                "has_video": has_video, "url": direct_url,
                "is_direct": bool(direct_url and not requires_s and not requires_n),
                "requires_signature": requires_s, "requires_n_transform": requires_n,
                "signature_cipher": cipher if requires_s else None,
                "client_name": player.get("_extractor_client"),
                "user_agent": player.get("_extractor_user_agent"),
                "po_token_used": player.get("_extractor_token_used", False),
            })
        output.sort(key=lambda x: (
            bool(x["has_audio"] and x["has_video"]), bool(x["is_direct"]),
            x.get("height") or 0, x.get("bitrate") or 0
        ), reverse=True)
        return output

    @staticmethod
    def _captions(player, automatic=False):
        renderer = ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
        tracks = renderer.get("captionTracks") or []
        output = []
        for item in tracks if isinstance(tracks, list) else []:
            if not isinstance(item, dict):
                continue
            is_auto = item.get("kind") == "asr"
            if is_auto and not automatic:
                continue
            output.append({
                "language": item.get("languageCode"),
                "name": YouTubeExtractor._text(item.get("name")),
                "url": item.get("baseUrl"), "automatic": is_auto,
                "vss_id": item.get("vssId"),
            })
        return output

    @staticmethod
    def _parse_player(video_id, player, options):
        details = player.get("videoDetails") or {}
        status = player.get("playabilityStatus") or {}
        micro = ((player.get("microformat") or {}).get("playerMicroformatRenderer") or {})
        status_code = status.get("status")
        if status_code not in (None, "OK"):
            reason = status.get("reason") or status_code or "Video unavailable."
            if status_code in ("LOGIN_REQUIRED", "AGE_CHECK_REQUIRED", "CONTENT_CHECK_REQUIRED",
                               "AGE_VERIFICATION_REQUIRED", "GEO_RESTRICTED"):
                raise YouTubeRestricted(str(reason))
            raise YouTubeUnavailable(str(reason))
        thumbs = (details.get("thumbnail") or {}).get("thumbnails")
        captions = YouTubeExtractor._captions(player, options.automatic_captions) if options.captions else []
        streaming = player.get("streamingData") or {}
        result = {
            "id": video_id, "title": details.get("title"),
            "url": f"{_BASE}/watch?v={video_id}",
            "author": details.get("author"), "channel_id": details.get("channelId"),
            "duration": YouTubeExtractor._duration(details.get("lengthSeconds")),
            "view_count": YouTubeExtractor._duration(details.get("viewCount")),
            "is_live": bool(details.get("isLiveContent")),
            "is_private": bool(details.get("isPrivate")),
            "is_unlisted": bool(details.get("isUnlisted")),
            "is_family_safe": details.get("isFamilySafe"),
            "category": micro.get("category"),
            "upload_date": micro.get("uploadDate"),
            "publish_date": micro.get("publishDate"),
            "owner_channel_url": micro.get("ownerProfileUrl"),
            "description": details.get("shortDescription") if options.description else None,
            "tags": (details.get("keywords") or []) if options.tags else [],
            "thumbnails": YouTubeExtractor._thumbnails(thumbs) if options.thumbnails else [],
            "formats": YouTubeExtractor._formats(player) if options.formats else [],
            "captions": [x for x in captions if not x["automatic"]],
            "automatic_captions": [x for x in captions if x["automatic"]] if options.automatic_captions else [],
            "chapters": (micro.get("chapters") or []) if options.chapters else [],
            "related_videos": [], "playlist": None,
            "playability_status": status_code, "playability_reason": status.get("reason"),
            "hls_manifest_url": streaming.get("hlsManifestUrl") if options.hls else None,
            "dash_manifest_url": streaming.get("dashManifestUrl") if options.dash else None,
            "extractor_client": player.get("_extractor_client"),
        }
        if options.raw_response:
            result["raw_response"] = player
        return result

    @staticmethod
    def _copy(value):
        return json.loads(json.dumps(value))

    async def extract(self, value, *, options=None, user_id=None, refresh=False):
        options = options or YouTubeOptions()
        video_id = self._video_id(value)
        # User-specific auth/token profiles must not share cached private results.
        profile = hashlib.sha256(
            f"{user_id!s}:{id(self)}".encode()
        ).hexdigest()[:16]
        key = f"video:{profile}:{video_id}"

        async def fetch():
            player = await self._player(video_id, options, user_id)
            result = self._parse_player(video_id, player, options)
            await self.stream_cache.put(f"{profile}:{video_id}", result.get("formats", []))
            cached = self._copy(result)
            cached["formats"] = [
                {**x, "url": None, "is_direct": False}
                for x in cached.get("formats", [])
            ]
            cached.pop("raw_response", None)
            return cached

        result = await fetch() if refresh else await self.metadata_cache.get_or_create(key, fetch)
        result = self._copy(result)
        if options.formats:
            streams = await self.stream_cache.get(f"{profile}:{video_id}")
            if streams is None or refresh:
                player = await self._player(video_id, options, user_id)
                streams = self._formats(player)
                await self.stream_cache.put(f"{profile}:{video_id}", streams)
            result["formats"] = self._copy(streams)
        else:
            result["formats"] = []
        if not options.metadata:
            for field in ("title", "author", "channel_id", "duration", "view_count"):
                result[field] = None
        if not options.thumbnails:
            result["thumbnails"] = []
        if not options.description:
            result["description"] = None
        if not options.tags:
            result["tags"] = []
        if not options.captions:
            result["captions"] = result["automatic_captions"] = []
        elif not options.automatic_captions:
            result["automatic_captions"] = []
        if not options.chapters:
            result["chapters"] = []
        if not options.hls:
            result["hls_manifest_url"] = None
        if not options.dash:
            result["dash_manifest_url"] = None
        if options.raw_response:
            player = await self._player(video_id, options, user_id)
            result["raw_response"] = player
        else:
            result.pop("raw_response", None)
        return result

    async def _fresh_format(self, video_id, itag, options, client_name=None, user_id=None):
        player = await self._player(video_id, options, user_id, preferred=client_name)
        selected = next((x for x in self._formats(player) if x.get("itag") == itag), None)
        if not selected:
            raise YouTubeUnavailable(f"Format {itag} is unavailable for {player.get('_extractor_client')}.")
        if not selected.get("url") or not selected.get("is_direct"):
            raise YouTubeExtractorError(
                "Format needs signature or n-parameter deciphering; refusing to download an invalid URL."
            )
        return selected

    @staticmethod
    def _safe_target(output):
        try:
            target = Path(output).expanduser()
        except (TypeError, ValueError) as exc:
            raise YouTubeExtractorError("Invalid output path.") from exc
        if not target.name or target.name in (".", "..") or "\x00" in str(target):
            raise YouTubeExtractorError("Invalid output filename.")
        return target

    @staticmethod
    def _read_resume_metadata(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _write_resume_metadata(path, video_id, itag, total):
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps({"video_id": video_id, "itag": itag, "total": total}), encoding="utf-8")
        temp.replace(path)

    async def _download_range(
        self, url, start, end, *, video_id, user_agent, timeout,
        output_path: Path | None = None,
    ):
        headers = {
            "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
            "User-Agent": user_agent, "Referer": f"{_BASE}/watch?v={video_id}",
            "Origin": _BASE,
        }
        async with self.client.stream(
            "GET", url, headers=headers, follow_redirects=True,
            timeout=httpx.Timeout(timeout, read=max(60.0, timeout)),
        ) as response:
            status = response.status_code
            if status == 416:
                return {"status": 416, "data": b"",
                        "total": _fast_unsatisfied_range(response.headers.get("Content-Range", ""))}
            if status == 403 or status in RETRYABLE_STATUS_CODES:
                return {
                    "status": status, "data": b"", "total": None,
                    "retry_after": response.headers.get("Retry-After"),
                }
            if status == 200:
                if output_path is None:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(256 * 1024):
                        body.extend(chunk)
                    return {"status": 200, "data": bytes(body), "total": len(body)}

                # Some servers ignore Range. Stream to disk and replace the
                # partial file instead of buffering a potentially multi-GB file.
                await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
                written = 0
                with output_path.open("wb") as file:
                    if response.is_stream_consumed:
                        body = response.content
                        for position in range(0, len(body), 256 * 1024):
                            chunk = body[position:position + 256 * 1024]
                            file.write(chunk)
                            written += len(chunk)
                    else:
                        async for chunk in response.aiter_raw():
                            if chunk:
                                file.write(chunk)
                                written += len(chunk)
                    file.flush()
                return {
                    "status": 200, "data": b"", "total": written,
                    "written_to_file": True,
                }
            if status != 206:
                raise YouTubeRequestError(f"Media server returned HTTP {status}.")
            parsed = _fast_content_range(response.headers.get("Content-Range", ""))
            if parsed is None:
                raise YouTubeRequestError("Invalid Content-Range from media server.")
            range_start, range_end, total = parsed
            if range_start != start or range_end < start or range_end > end:
                raise YouTubeRequestError("Media server returned an unexpected byte range.")
            expected = range_end - range_start + 1
            body = bytearray()
            async for chunk in response.aiter_bytes(256 * 1024):
                body.extend(chunk)
                if len(body) > expected:
                    raise YouTubeRequestError("Media server sent more bytes than requested.")
            if len(body) != expected:
                raise YouTubeRequestError("Incomplete media chunk received.")
            return {"status": 206, "data": bytes(body), "total": total}

    async def download_format(
        self, video_id, itag, output, *, options=None, user_id=None,
        chunk_size=1024 * 1024, resume=True, max_url_refreshes=4,
    ):
        video_id = self._video_id(video_id)
        options = options or YouTubeOptions()
        try:
            itag = int(itag)
            chunk_size = max(64 * 1024, min(int(chunk_size), 8 * 1024 * 1024))
            max_url_refreshes = max(1, min(int(max_url_refreshes), 10))
        except (TypeError, ValueError) as exc:
            raise YouTubeExtractorError("Invalid download parameters.") from exc

        target = self._safe_target(output)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        meta = target.with_name(target.name + ".part.json")

        async with self._download_locks_guard:
            lock = self._download_locks.setdefault(str(target.resolve()), asyncio.Lock())

        async with lock:
            saved = self._read_resume_metadata(meta) if resume else None
            downloaded = 0
            total = None
            if part.is_file() and saved and saved.get("video_id") == video_id and saved.get("itag") == itag:
                downloaded = await asyncio.to_thread(lambda: part.stat().st_size)
                total = saved.get("total") if isinstance(saved.get("total"), int) else None
            else:
                await asyncio.to_thread(part.unlink, missing_ok=True)
                downloaded = 0
                await asyncio.to_thread(self._write_resume_metadata, meta, video_id, itag, None)

            if total is not None and downloaded > total:
                await asyncio.to_thread(part.unlink, missing_ok=True)
                downloaded, total = 0, None

            current = await self._fresh_format(video_id, itag, options, user_id=user_id)
            attempts = 0
            while total is None or downloaded < total:
                if part.is_file():
                    downloaded = await asyncio.to_thread(lambda: part.stat().st_size)
                end = downloaded + chunk_size - 1
                try:
                    async with self._download_semaphore:
                        result = await self._download_range(
                            current["url"], downloaded, end, video_id=video_id,
                            user_agent=current.get("user_agent") or _WEB_UA,
                            timeout=options.timeout, output_path=part,
                        )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    attempts += 1
                    if attempts > max_url_refreshes:
                        raise YouTubeRequestError(
                            f"Media transfer failed after retries: {type(exc).__name__}."
                        ) from exc
                    downloaded = await asyncio.to_thread(
                        lambda: part.stat().st_size if part.is_file() else 0
                    )
                    await asyncio.sleep(
                        RetryPolicy(base_delay=0.5, max_delay=8.0).delay(None, attempts - 1)
                    )
                    names = list(self.client_order)
                    old = current.get("client_name")
                    start_index = names.index(old) if old in names else -1
                    preferred = names[(start_index + attempts) % len(names)]
                    current = await self._fresh_format(video_id, itag, options, preferred, user_id)
                    continue
                status = result["status"]
                if status == 403 or status in RETRYABLE_STATUS_CODES:
                    attempts += 1
                    if attempts > max_url_refreshes:
                        raise YouTubeRequestError(
                            f"Media server repeatedly returned HTTP {status}. "
                            "This may require a valid PO Token or a different access profile."
                        )
                    if status != 403:
                        retry_after = result.get("retry_after")
                        delay = RetryPolicy(base_delay=0.5, max_delay=20.0).delay(
                            retry_after, attempts - 1
                        )
                        await asyncio.sleep(delay)
                    names = list(self.client_order)
                    old = current.get("client_name")
                    start = names.index(old) if old in names else -1
                    preferred = names[(start + attempts) % len(names)]
                    current = await self._fresh_format(
                        video_id, itag, options, preferred, user_id
                    )
                    continue
                if status == 416:
                    actual = part.stat().st_size if part.exists() else 0
                    if result.get("total") is not None and actual == result["total"]:
                        total = result["total"]
                        break
                    raise YouTubeRequestError("HTTP 416 received for an incomplete download.")
                body = result["data"]
                if status == 200:
                    # Server ignored Range; _download_range streamed the complete
                    # response to .part without an unbounded in-memory buffer.
                    total = result["total"]
                    actual = await asyncio.to_thread(lambda: part.stat().st_size if part.exists() else 0)
                    if not result.get("written_to_file"):
                        await asyncio.to_thread(part.write_bytes, body)
                        actual = len(body)
                    downloaded = actual
                    if downloaded == 0 or (total is not None and downloaded != total):
                        raise YouTubeRequestError("Incomplete full media response.")
                    await asyncio.to_thread(self._write_resume_metadata, meta, video_id, itag, total)
                    break
                response_total = result.get("total")
                if response_total is not None:
                    if total is not None and total != response_total:
                        raise YouTubeRequestError("Media size changed during download.")
                    total = response_total
                    await asyncio.to_thread(self._write_resume_metadata, meta, video_id, itag, total)
                if not body:
                    raise YouTubeRequestError("YouTube returned an empty media chunk.")
                await asyncio.to_thread(self._append_bytes, part, body)
                downloaded += len(body)
                if total is not None and downloaded > total:
                    raise YouTubeRequestError("Downloaded bytes exceed expected file size.")

            actual = await asyncio.to_thread(lambda: part.stat().st_size)
            if total is None or actual != total:
                raise YouTubeRequestError(f"Incomplete download: {actual}/{total} bytes.")
            await asyncio.to_thread(part.replace, target)
            await asyncio.to_thread(meta.unlink, missing_ok=True)
            return {
                "id": video_id, "itag": itag, "format_id": str(itag),
                "path": str(target), "size": actual,
                "mime_type": current.get("mime_type"), "ext": current.get("ext"),
                "bitrate": current.get("bitrate"),
                "client_name": current.get("client_name"), "success": True,
            }

    @staticmethod
    def _append_bytes(path, data):
        with path.open("ab") as file:
            file.write(data)

    @staticmethod
    def _extract_json_assignment(page, variable):
        match = re.search(rf"(?:var\s+|let\s+|const\s+)?{re.escape(variable)}\s*=", page or "")
        if not match:
            return None
        start = page.find("{", match.end())
        if start < 0:
            return None
        depth = 0
        in_string = escaped = False
        for i in range(start, len(page)):
            char = page[i]
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
                        return json.loads(page[start:i + 1])
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _playlist_items(data):
        output, seen = [], set()
        for node in _fast_walk(data):
            renderer = node.get("playlistVideoRenderer") or node.get("playlistPanelVideoRenderer")
            if not isinstance(renderer, dict):
                continue
            video_id = renderer.get("videoId")
            if not isinstance(video_id, str) or not _ID_RE.fullmatch(video_id) or video_id in seen:
                continue
            seen.add(video_id)
            output.append({
                "id": video_id, "title": _fast_text(renderer.get("title")),
                "url": f"{_BASE}/watch?v={video_id}",
                "thumbnail": YouTubeExtractor._thumbnails((renderer.get("thumbnail") or {}).get("thumbnails")),
                "duration": _fast_text(renderer.get("lengthText")),
                "index": _fast_text(renderer.get("index")),
                "is_playable": renderer.get("isPlayable"),
                "short_byline": _fast_text(renderer.get("shortBylineText")),
            })
        return output

    @staticmethod
    def _continuation(data):
        for node in _fast_walk(data):
            item = node.get("continuationItemRenderer")
            if not isinstance(item, dict):
                continue
            endpoint = item.get("continuationEndpoint") or {}
            command = endpoint.get("continuationCommand") or {}
            token = command.get("token")
            if isinstance(token, str) and token:
                return token
        return None

    @staticmethod
    def _playlist_metadata(data):
        for node in _fast_walk(data):
            renderer = node.get("playlistMetadataRenderer")
            if isinstance(renderer, dict):
                return {"title": renderer.get("title"), "description": renderer.get("description")}
        return {"title": None, "description": None}

    async def _browse_continuation(self, continuation, options):
        await self._bootstrap()
        client = self._client_map["WEB"]
        payload = {
            "context": {"client": {
                "clientName": "WEB", "clientVersion": client["version"],
                "hl": "en", "gl": "US",
            }},
            "continuation": continuation,
        }
        response = await self._request(
            "POST", f"{_BASE}/youtubei/v1/browse", options=options,
            params={"key": self._api_key}, json=payload,
            headers={"User-Agent": _WEB_UA, "Content-Type": "application/json",
                     "Origin": _BASE, "Referer": f"{_BASE}/"},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise YouTubeRequestError("Invalid playlist continuation response.") from exc
        finally:
            await response.aclose()
        if not isinstance(data, dict):
            raise YouTubeRequestError("Unexpected playlist continuation response.")
        return data

    async def get_playlist(self, playlist_id, *, limit=100, flat=True, options=None, refresh=False):
        options = options or YouTubeOptions()
        if not isinstance(playlist_id, str) or not _PLAYLIST_RE.fullmatch(playlist_id):
            raise YouTubeExtractorError("Invalid YouTube playlist ID.")
        try:
            limit = max(1, min(int(limit), options.playlist_limit, 500))
        except (TypeError, ValueError) as exc:
            raise YouTubeExtractorError("Invalid playlist limit.") from exc
        key = f"playlist:{playlist_id}:{limit}:{bool(flat)}"

        async def fetch():
            response = await self._request(
                "GET", f"{_BASE}/playlist", options=options,
                params={"list": playlist_id}, headers={"User-Agent": _WEB_UA},
            )
            try:
                data = self._extract_json_assignment(response.text, "ytInitialData")
            finally:
                await response.aclose()
            if not isinstance(data, dict):
                raise YouTubeRequestError("Could not parse YouTube playlist page.")
            metadata = self._playlist_metadata(data)
            items = self._playlist_items(data)
            token = self._continuation(data)
            seen = {x["id"] for x in items}
            pages = 0
            while token and len(items) < limit and pages < 30:
                pages += 1
                next_data = await self._browse_continuation(token, options)
                next_items = self._playlist_items(next_data)
                for item in next_items:
                    if item["id"] not in seen:
                        items.append(item)
                        seen.add(item["id"])
                    if len(items) >= limit:
                        break
                token = self._continuation(next_data)
                if not next_items:
                    break
            return {
                "id": playlist_id, "title": metadata["title"],
                "description": metadata["description"],
                "url": f"{_BASE}/playlist?list={playlist_id}",
                "video_count": None, "items": items[:limit],
                "continuation": token, "has_more": bool(token), "flat": bool(flat),
            }

        result = await fetch() if refresh else await self.playlist_cache.get_or_create(key, fetch)
        return self._copy(result)

    async def get_playlist_next(self, playlist_id, continuation, *, limit=100, options=None):
        options = options or YouTubeOptions()
        if not isinstance(playlist_id, str) or not _PLAYLIST_RE.fullmatch(playlist_id):
            raise YouTubeExtractorError("Invalid YouTube playlist ID.")
        if not isinstance(continuation, str) or not continuation:
            raise YouTubeExtractorError("A valid continuation token is required.")
        limit = max(1, min(int(limit), options.playlist_limit, 500))
        data = await self._browse_continuation(continuation, options)
        items = self._playlist_items(data)
        token = self._continuation(data)
        return {"id": playlist_id, "items": items[:limit],
                "continuation": token, "has_more": bool(token)}

    async def cache_stats(self):
        return {
            "metadata": await self.metadata_cache.stats(),
            "playlists": await self.playlist_cache.stats(),
            "streams": await self.stream_cache.stats(),
            "cython": CYTHON_AVAILABLE,
        }

    async def clear_cache(self):
        await asyncio.gather(
            self.metadata_cache.clear(), self.playlist_cache.clear(),
            self.stream_cache.clear(),
        )


__all__ = [
    "YouTubeExtractor", "YouTubeExtractorError", "YouTubeUnavailable",
    "YouTubeRestricted", "YouTubeRequestError", "YouTubeOptions",
    "TTLCache", "CYTHON_AVAILABLE",
]
