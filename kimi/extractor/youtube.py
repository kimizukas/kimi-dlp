from __future__ import annotations

import asyncio
import copy
import json
import re
import time

from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import hashlib

import httpx

# Mandatory compiled extension. No Python fallback.
try:
    from .youtube_core import (
        is_video_id,
        parse_content_range,
        parse_unsatisfied_range,
        safe_int,
        text_value,
        format_sort_key,
        normalize_content_length,
        calculate_range,
        deduplicate_ids,
    )
except ImportError as exc:
    raise ImportError(
        "The compiled kimi.extractor.youtube_core extension is required. "
        "Build it with: python setup.py build_ext --inplace"
    ) from exc


_BASE = "https://www.youtube.com"
_PLAYER_URL = f"{_BASE}/youtubei/v1/player"
_BROWSE_URL = f"{_BASE}/youtubei/v1/browse"

_ANDROID_VERSION = "20.10.38"
_WEB_VERSION = "2.20261123.00.00"

_ANDROID_UA = (
    "com.google.android.youtube/20.10.38 "
    "(Linux; U; Android 15) gzip"
)

_WEB_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_VERSION_RE = re.compile(
    r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"'
)

_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}

_RESTRICTED_STATUSES = {
    "LOGIN_REQUIRED",
    "AGE_CHECK_REQUIRED",
    "CONTENT_CHECK_REQUIRED",
    "AGE_VERIFICATION_REQUIRED",
}

_UNAVAILABLE_STATUSES = {
    "ERROR",
    "LIVE_STREAM_OFFLINE",
    "UNPLAYABLE",
    "VIDEO_UNAVAILABLE",
    "COPYRIGHT",
}

_PLAYER_CLIENTS = (
    {
        "name": "ANDROID",
        "version": _ANDROID_VERSION,
        "user_agent": _ANDROID_UA,
        "client_id": "3",
        "priority": 0,
    },
    {
        "name": "WEB",
        "version": _WEB_VERSION,
        "user_agent": _WEB_UA,
        "client_id": "1",
        "priority": 1,
    },
)


class YouTubeExtractorError(Exception):
    """Base extractor exception."""


class YouTubeUnavailable(YouTubeExtractorError):
    """The requested video or playlist is unavailable."""


class YouTubeRestricted(YouTubeExtractorError):
    """The requested content requires authentication or verification."""


class YouTubeRequestError(YouTubeExtractorError):
    """HTTP, network, or response parsing error."""


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

    def __post_init__(self) -> None:
        self.retries = max(0, min(int(self.retries), 5))
        self.playlist_limit = max(1, min(int(self.playlist_limit), 500))
        self.timeout = max(3.0, min(float(self.timeout), 60.0))


@dataclass(slots=True)
class CacheEntry:
    value: Any
    expires: float


class TTLCache:
    """Bounded asynchronous LRU cache with TTL and request de-duplication."""

    def __init__(self, maxsize: int = 512, ttl: int = 21600):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(1, int(ttl))

        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

        self.hits = 0
        self.misses = 0
        self.expired = 0

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._data.get(key)

            if entry is None:
                self.misses += 1
                return None

            if entry.expires <= time.monotonic():
                self._data.pop(key, None)
                self.expired += 1
                self.misses += 1
                return None

            self._data.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(entry.value)

    async def put(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        lifetime = self.ttl if ttl is None else max(1, int(ttl))

        async with self._lock:
            self._data[key] = CacheEntry(
                value=copy.deepcopy(value),
                expires=time.monotonic() + lifetime,
            )
            self._data.move_to_end(key)

            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def _produce(
        self,
        key: str,
        factory,
        ttl: int | None,
    ) -> Any:
        try:
            result = await factory()
            await self.put(key, result, ttl)
            return result
        finally:
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def get_or_create(
        self,
        key: str,
        factory,
        ttl: int | None = None,
    ) -> Any:
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

        return copy.deepcopy(await asyncio.shield(task))

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            now = time.monotonic()

            return {
                "entries": sum(
                    entry.expires > now
                    for entry in self._data.values()
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
    Async YouTube metadata, format, playlist and media extractor.

    Network operations use httpx.
    CPU-oriented parsing helpers use the mandatory Cython extension.
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
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        request_concurrency: int = 8,
        download_concurrency: int = 3,
        po_token: str | None = None,
        options: YouTubeOptions | None = None,
    ):
        self.options = options or YouTubeOptions(timeout=timeout)
        self.api_key = api_key or _DEFAULT_API_KEY
        self._api_key = self.api_key
        self._client_version = _WEB_VERSION

        self.timeout = max(3.0, min(float(timeout), 60.0))
        self.po_token = po_token.strip() if po_token else None

        self._external_client = client is not None
        self._closed = False

        if client is not None:
            self.client = client
        else:
            max_connections = max(1, int(max_connections))
            keepalive = max(
                1,
                min(int(max_keepalive_connections), max_connections),
            )

            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.timeout,
                    connect=min(self.timeout, 10.0),
                ),
                follow_redirects=True,
                proxy=proxy,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=keepalive,
                    keepalive_expiry=30.0,
                ),
                headers={
                    "User-Agent": _WEB_UA,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                },
                cookies=self._load_cookies(cookies),
            )

        self.metadata_cache = TTLCache(
            cache_size,
            metadata_ttl,
        )
        self.playlist_cache = TTLCache(
            max(32, cache_size // 4),
            metadata_ttl,
        )
        self.stream_cache = TTLCache(
            cache_size,
            stream_ttl,
        )

        self._bootstrap_lock = asyncio.Lock()
        self._bootstrapped = False

        self._request_semaphore = asyncio.Semaphore(
            max(1, int(request_concurrency))
        )
        self._download_semaphore = asyncio.Semaphore(
            max(1, int(download_concurrency))
        )

    @staticmethod
    def _load_cookies(cookie_file: str | None):
        if not cookie_file:
            return None

        path = Path(cookie_file).expanduser()

        if not path.is_file():
            raise YouTubeExtractorError(
                f"Cookie file does not exist: {path}"
            )

        cookies = httpx.Cookies()

        try:
            with path.open("r", encoding="utf-8") as file:
                for raw_line in file:
                    line = raw_line.rstrip("\r\n")

                    if not line:
                        continue

                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]
                    elif line.startswith("#"):
                        continue

                    parts = line.split("\t")
                    if len(parts) != 7:
                        continue

                    domain, _, cookie_path, _, _, name, value = parts

                    if not domain or not name:
                        continue

                    cookies.set(
                        name,
                        value,
                        domain=domain,
                        path=cookie_path or "/",
                    )

        except OSError as exc:
            raise YouTubeExtractorError(
                f"Unable to read cookie file: {exc}"
            ) from exc

        return cookies

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        if not self._external_client:
            await self.client.aclose()

    async def __aenter__(self):
        if self._closed:
            raise YouTubeExtractorError("Extractor is already closed.")
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise YouTubeExtractorError("Extractor is closed.")

    @staticmethod
    def _retry_delay(
        response: httpx.Response | None,
        attempt: int,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")

            if retry_after:
                try:
                    return min(20.0, max(0.0, float(retry_after)))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        delay = date.timestamp() - time.time()
                        return min(20.0, max(0.0, delay))
                    except (TypeError, ValueError, OverflowError):
                        pass

        return min(0.4 * (2 ** attempt), 4.0)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        retries: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        self._ensure_open()

        retries = (
            self.options.retries
            if retries is None
            else max(0, min(int(retries), 5))
        )

        async with self._request_semaphore:
            for attempt in range(retries + 1):
                response = None

                try:
                    response = await self.client.request(
                        method,
                        url,
                        **kwargs,
                    )

                    status = response.status_code

                    if status in _TRANSIENT_HTTP:
                        if attempt < retries:
                            delay = self._retry_delay(response, attempt)
                            await response.aclose()
                            response = None
                            await asyncio.sleep(delay)
                            continue

                        body = response.text[:500]
                        await response.aclose()
                        response = None
                        raise YouTubeRequestError(
                            f"Transient HTTP {status}: {body}"
                        )

                    if status in (401, 403):
                        body = response.text[:500]
                        await response.aclose()
                        response = None
                        raise YouTubeRestricted(
                            f"YouTube rejected the request "
                            f"(HTTP {status}): {body}"
                        )

                    if status >= 400:
                        body = response.text[:500]
                        await response.aclose()
                        response = None
                        raise YouTubeRequestError(
                            f"HTTP {status}: {body}"
                        )

                    # Caller owns and must close a successful response.
                    return response

                except YouTubeExtractorError:
                    raise

                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if response is not None:
                        await response.aclose()

                    if attempt >= retries:
                        raise YouTubeRequestError(
                            f"Request failed after {attempt + 1} attempts: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                    await asyncio.sleep(
                        self._retry_delay(None, attempt)
                    )

        raise YouTubeRequestError("Request failed.")

    async def _bootstrap(self) -> None:
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

                key_match = _API_KEY_RE.search(page)
                version_match = _VERSION_RE.search(page)

                if key_match:
                    self._api_key = key_match.group(1)

                if version_match:
                    self._client_version = version_match.group(1)

            except YouTubeExtractorError:
                # Configured defaults remain available.
                pass

            self._bootstrapped = True

    @staticmethod
    def _player_payload(
        video_id: str,
        client_name: str,
        client_version: str,
        po_token: str | None = None,
    ) -> dict[str, Any]:
        client = {
            "clientName": client_name,
            "clientVersion": client_version,
            "hl": "en",
            "gl": "US",
        }

        if client_name == "ANDROID":
            client["androidSdkVersion"] = 35

        if po_token:
            client["poToken"] = po_token

        return {
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "context": {"client": client},
 
             }
             
             
    def _sapisid_authorization(self) -> str | None:
        """Build YouTube SAPISIDHASH authorization from loaded cookies."""

        cookie_names = (
            ("SAPISID", "SAPISIDHASH"),
            ("__Secure-1PAPISID", "SAPISID1PHASH"),
            ("__Secure-3PAPISID", "SAPISID3PHASH"),
        )

        jar = self.client.cookies.jar

        wanted_names = {name for name, _ in cookie_names}
        cookie_values = {}

        for cookie in jar:
            domain = (cookie.domain or "").lstrip(".").lower()

            if domain == "youtube.com" or domain.endswith(".youtube.com"):
                if cookie.name in wanted_names:
                    cookie_values[cookie.name] = cookie.value

        timestamp = str(int(time.time()))
        hashes = []

        for cookie_name, hash_name in cookie_names:
            value = cookie_values.get(cookie_name)

            if not value:
                continue

            digest = hashlib.sha1(
                f"{timestamp} {value} {_BASE}".encode("utf-8")
            ).hexdigest()

            hashes.append(
                f"{hash_name} {timestamp}_{digest}"
            )

        return " ".join(hashes) or None

    async def _player_request(
        self,
        video_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:

        payload = self._player_payload(
            video_id,
            config["name"],
            config["version"],
            self.po_token,
        )

        headers = {
            "User-Agent": config["user_agent"],
            "Content-Type": "application/json",
            "Origin": _BASE,
            "Referer": f"{_BASE}/",
            "X-YouTube-Client-Name": config["client_id"],
            "X-YouTube-Client-Version": config["version"],
        }

        authorization = self._sapisid_authorization()

        if authorization:
            headers["Authorization"] = authorization

        response = await self._request(
            "POST",
            _PLAYER_URL,
            params={"key": self._api_key},
            json=payload,
            headers=headers,
        )

        try:
            try:
                data = response.json()
            except ValueError as exc:
                raise YouTubeRequestError(
                    "Invalid YouTube player JSON."
                ) from exc
        finally:
            await response.aclose()

        if not isinstance(data, dict):
            raise YouTubeRequestError(
                "Unexpected YouTube player response."
            )

        data["_extractor_client"] = config["name"]
        data["_extractor_user_agent"] = config["user_agent"]

        return data



    @staticmethod
    def _player_status(data: dict[str, Any]) -> str | None:
        status = data.get("playabilityStatus") or {}
        if not isinstance(status, dict):
            return None
        return status.get("status")

    @staticmethod
    def _player_formats(data: dict[str, Any]) -> list:
        streaming = data.get("streamingData") or {}
        if not isinstance(streaming, dict):
            return []

        regular = streaming.get("formats") or []
        adaptive = streaming.get("adaptiveFormats") or []

        if not isinstance(regular, list):
            regular = []
        if not isinstance(adaptive, list):
            adaptive = []

        return regular + adaptive

    async def _player(
        self,
        video_id: str,
        options: YouTubeOptions | None = None,
    ) -> dict[str, Any]:
        await self._bootstrap()

        errors: list[Exception] = []
        restricted_reason = None

        for config in _PLAYER_CLIENTS:
            try:
                data = await self._player_request(video_id, config)
            except YouTubeRestricted as exc:
                errors.append(exc)
                continue
            except YouTubeExtractorError as exc:
                errors.append(exc)
                continue

            status = self._player_status(data)
            formats = self._player_formats(data)

            if status in (None, "OK") and formats:
                return data

            playability = data.get("playabilityStatus") or {}
            reason = (
                playability.get("reason")
                if isinstance(playability, dict)
                else None
            )

            if status in _RESTRICTED_STATUSES:
                restricted_reason = reason or status
                continue

            if status in _UNAVAILABLE_STATUSES:
                errors.append(
                    YouTubeUnavailable(str(reason or status))
                )
                continue

            errors.append(
                YouTubeRequestError(
                    f"{config['name']} returned no usable formats."
                )
            )

        if restricted_reason:
            raise YouTubeRestricted(str(restricted_reason))

        if errors:
            raise errors[-1]

        raise YouTubeRequestError(
            "Unable to obtain a usable YouTube player response."
        )

    @staticmethod
    def _video_id(value: str) -> str:
        if not isinstance(value, str):
            raise YouTubeExtractorError(
                "Video ID or URL must be a string."
            )

        value = value.strip()

        if is_video_id(value):
            return value

        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise YouTubeExtractorError("Invalid YouTube URL.") from exc

        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
        }

        if host not in allowed_hosts:
            raise YouTubeExtractorError(
                "Invalid YouTube video URL or ID."
            )

        video_id = None

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]

        elif host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        }:
            video_id = parse_qs(parsed.query).get("v", [None])[0]

            if not video_id:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {
                    "shorts",
                    "live",
                    "embed",
                    "v",
                }:
                    video_id = parts[1]

        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] == "embed":
                video_id = parts[1]

        if not is_video_id(video_id):
            raise YouTubeExtractorError(
                "Invalid YouTube video URL or ID."
            )

        return video_id

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _list(value: Any) -> list:
        return value if isinstance(value, list) else []

    @staticmethod
    def _text(value: Any) -> str | None:
        return text_value(value)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _duration(value: Any) -> int | None:
        result = safe_int(value)
        return result if result is not None and result >= 0 else None

    @staticmethod
    def _thumbnails(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            data = data.get("thumbnails")

        if not isinstance(data, list):
            return []

        output = []
        seen = set()

        for item in data:
            if not isinstance(item, dict):
                continue

            url = item.get("url")
            if not isinstance(url, str):
                continue
            if not url.startswith(("https://", "http://")):
                continue
            if url in seen:
                continue

            seen.add(url)
            output.append({
                "url": url,
                "width": safe_int(item.get("width")),
                "height": safe_int(item.get("height")),
            })

        return output

    @staticmethod
    def _mime_info(
        mime_type: str | None,
    ) -> tuple[str | None, str | None]:
        if not isinstance(mime_type, str):
            return None, None

        mime = mime_type.split(";", 1)[0].strip().lower()
        match = re.search(r'codecs="([^"]+)"', mime_type)
        codecs = match.group(1) if match else None

        extension = {
            "audio/mp4": "m4a",
            "audio/webm": "webm",
            "video/mp4": "mp4",
            "video/webm": "webm",
            "application/vnd.apple.mpegurl": "m3u8",
            "application/x-mpegurl": "m3u8",
        }.get(mime)

        return extension, codecs

    @staticmethod
    def _codec_family(codecs: str | None) -> list[str]:
        if not codecs:
            return []

        output = []

        for codec in codecs.lower().split(","):
            codec = codec.strip()

            if codec.startswith(("avc", "h264")):
                family = "h264"
            elif codec.startswith(("hev", "hvc", "h265")):
                family = "h265"
            elif codec.startswith("vp8"):
                family = "vp8"
            elif codec.startswith(("vp9", "vp09")):
                family = "vp9"
            elif codec.startswith(("av01", "av1")):
                family = "av1"
            elif codec.startswith(("mp4a", "aac")):
                family = "aac"
            elif codec.startswith("opus"):
                family = "opus"
            elif codec.startswith("vorbis"):
                family = "vorbis"
            else:
                family = codec.split(".", 1)[0] or "unknown"

            if family not in output:
                output.append(family)

        return output

    def _parse_format(
        self,
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        itag = safe_int(raw.get("itag"))
        if itag is None:
            return None

        mime_type = raw.get("mimeType")
        extension, codecs = self._mime_info(mime_type)
        codec_families = self._codec_family(codecs)

        cipher = raw.get("signatureCipher") or raw.get("cipher")
        signature_data = None

        if isinstance(cipher, str):
            parsed_cipher = parse_qs(cipher)
            signature_data = {
                key: values[0]
                for key, values in parsed_cipher.items()
                if values
            }

        direct_url = raw.get("url")
        if not isinstance(direct_url, str):
            direct_url = None

        if direct_url and not direct_url.startswith("https://"):
            direct_url = None

        requires_signature = bool(
            signature_data and signature_data.get("s")
        )

        requires_n_transform = bool(
            direct_url
            and re.search(r"(?:^|[&?])n=[^&]+", direct_url)
        )

        if requires_signature:
            direct_url = None

        has_audio = raw.get("audioQuality") is not None or any(
            codec in {"aac", "opus", "vorbis"}
            for codec in codec_families
        )

        has_video = (
            raw.get("width") is not None
            or raw.get("height") is not None
            or any(
                codec in {"h264", "h265", "vp8", "vp9", "av1"}
                for codec in codec_families
            )
        )

        if has_audio and has_video:
            media_type = "video"
            format_type = "muxed"
        elif has_video:
            media_type = "video"
            format_type = "video"
        elif has_audio:
            media_type = "audio"
            format_type = "audio"
        else:
            media_type = "unknown"
            format_type = "unknown"

        return {
            "itag": itag,
            "url": direct_url,
            "mime_type": mime_type,
            "extension": extension,
            "codecs": codecs,
            "codec_families": codec_families,
            "type": media_type,
            "format_type": format_type,
            "width": safe_int(raw.get("width")),
            "height": safe_int(raw.get("height")),
            "fps": safe_int(raw.get("fps")),
            "quality": self._first_text(
                raw.get("qualityLabel"),
                raw.get("quality"),
                raw.get("audioQuality"),
            ),
            "bitrate": safe_int(raw.get("bitrate")),
            "average_bitrate": safe_int(raw.get("averageBitrate")),
            "audio_bitrate": safe_int(raw.get("audioBitrate")),
            "audio_quality": raw.get("audioQuality"),
            "sample_rate": safe_int(raw.get("audioSampleRate")),
            "channels": safe_int(raw.get("audioChannels")),
            "content_length": normalize_content_length(
                raw.get("contentLength")
            ),
            "language": raw.get("language"),
            "is_drc": bool(raw.get("isDrc")),
            "is_live": bool(raw.get("isLive")),
            "is_hls": bool(raw.get("isHls")),
            "is_dash": bool(raw.get("isDash")),
            "has_audio": bool(has_audio),
            "has_video": bool(has_video),
            "is_direct": bool(direct_url),
            "requires_signature": requires_signature,
            "requires_n_transform": requires_n_transform,
            "signature_data": signature_data,
            "init_range": raw.get("initRange"),
            "index_range": raw.get("indexRange"),
            "last_modified": raw.get("lastModified"),
            "approx_duration_ms": safe_int(raw.get("approxDurationMs")),
            "projection_type": raw.get("projectionType"),
            "audio_track": raw.get("audioTrack"),
            "source": "player_response",
        }

    def _extract_formats(
        self,
        player: dict[str, Any],
    ) -> list[dict[str, Any]]:
        streaming = self._mapping(player.get("streamingData"))

        raw_formats = (
            self._list(streaming.get("formats"))
            + self._list(streaming.get("adaptiveFormats"))
        )

        output = []
        seen = set()

        for raw in raw_formats:
            parsed = self._parse_format(raw)
            if not parsed:
                continue

            key = (
                parsed["itag"],
                parsed["mime_type"],
                parsed["quality"],
            )

            if key in seen:
                continue

            seen.add(key)
            output.append(parsed)

        output.sort(
            key=format_sort_key,
            reverse=True,
        )
        return output

    def _extract_captions(
        self,
        player: dict[str, Any],
    ) -> list[dict[str, Any]]:
        captions = self._mapping(
            self._mapping(player.get("captions")).get(
                "playerCaptionsTracklistRenderer"
            )
        )

        output = []
        seen = set()

        for track in self._list(captions.get("captionTracks")):
            if not isinstance(track, dict):
                continue

            base_url = track.get("baseUrl")
            language_code = track.get("languageCode")

            if not isinstance(base_url, str) or not language_code:
                continue

            key = (language_code, track.get("kind"))
            if key in seen:
                continue
            seen.add(key)

            output.append({
                "language_code": language_code,
                "language_name": self._text(track.get("name")),
                "kind": track.get("kind"),
                "is_auto_generated": track.get("kind") == "asr",
                "is_translatable": bool(track.get("isTranslatable")),
                "base_url": base_url,
                "vss_id": track.get("vssId"),
                "source": "player_response",
            })

        return output

    def _extract_translation_languages(
        self,
        player: dict[str, Any],
    ) -> list[dict[str, str]]:
        captions = self._mapping(
            self._mapping(player.get("captions")).get(
                "playerCaptionsTracklistRenderer"
            )
        )

        output = []

        for item in self._list(captions.get("translationLanguages")):
            if not isinstance(item, dict):
                continue

            code = item.get("languageCode")
            if not code:
                continue

            name = self._text(item.get("languageName"))

            output.append({
                "language_code": str(code),
                "language_name": name or str(code),
            })

        return output

    def _extract_chapters(
        self,
        player: dict[str, Any],
        duration: int | None,
    ) -> list[dict[str, Any]]:
        chapters = []
        seen = set()

        def visit(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item)
                return

            if not isinstance(node, dict):
                return

            renderer = (
                node.get("macroMarkersListItemRenderer")
                or node.get("chapterRenderer")
            )

            if isinstance(renderer, dict):
                title = self._text(
                    renderer.get("title")
                    or renderer.get("header")
                )

                start_ms = safe_int(
                    renderer.get("timeRangeStartMillis")
                    or renderer.get("timeStartMs")
                )

                if start_ms is None:
                    start_seconds = safe_int(
                        renderer.get("timeStart")
                        or renderer.get("startTime")
                    )
                    if start_seconds is not None:
                        start_ms = start_seconds * 1000

                if start_ms is not None and title:
                    start = max(0, start_ms // 1000)

                    if duration is None or start <= duration:
                        if start not in seen:
                            seen.add(start)
                            chapters.append({
                                "start_time": start,
                                "start_time_ms": start_ms,
                                "title": title,
                            })

            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)

        visit(player.get("chapters"))
        visit(player.get("frameworkUpdates"))

        chapters.sort(key=lambda item: item["start_time"])

        for index, chapter in enumerate(chapters):
            chapter["end_time"] = (
                chapters[index + 1]["start_time"]
                if index + 1 < len(chapters)
                else duration
            )

        return chapters

    def _extract_details(
        self,
        player: dict[str, Any],
        video_id: str,
    ) -> dict[str, Any]:
        details = self._mapping(player.get("videoDetails"))
        micro = self._mapping(
            self._mapping(player.get("microformat")).get(
                "playerMicroformatRenderer"
            )
        )

        duration = self._duration(details.get("lengthSeconds"))
        if duration is None:
            duration = self._duration(micro.get("lengthSeconds"))

        thumbnails = self._thumbnails(
            details.get("thumbnail") or micro.get("thumbnail")
        )

        live_details = self._mapping(
            micro.get("liveBroadcastDetails")
        )

        return {
            "id": video_id,
            "title": self._first_text(
                details.get("title"),
                self._text(micro.get("title")),
            ),
            "description": details.get("shortDescription"),
            "channel": self._first_text(
                details.get("author"),
                micro.get("ownerChannelName"),
            ),
            "channel_id": self._first_text(
                details.get("channelId"),
                micro.get("externalChannelId"),
            ),
            "duration": duration,
            "duration_string": str(duration) if duration is not None else None,
            "view_count": safe_int(details.get("viewCount")),
            "is_live": bool(
                details.get("isLiveContent")
                or live_details
            ),
            "is_private": bool(details.get("isPrivate")),
            "is_unlisted": bool(details.get("isUnlisted")),
            "is_family_safe": details.get("isFamilySafe"),
            "category": micro.get("category"),
            "publish_date": micro.get("publishDate"),
            "upload_date": micro.get("uploadDate"),
            "available_countries": self._list(
                micro.get("availableCountries")
            ),
            "thumbnails": thumbnails,
            "thumbnail": thumbnails[-1]["url"] if thumbnails else None,
            "keywords": self._list(details.get("keywords")),
            # Do not infer age restriction from unrelated player fields.
            "age_restricted": None,
            "live_start_timestamp": live_details.get("startTimestamp"),
            "live_end_timestamp": live_details.get("endTimestamp"),
        }

    async def extract(
        self,
        url_or_id: str,
        *,
        force: bool = False,
        options: YouTubeOptions | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()

        video_id = self._video_id(url_or_id)
        opts = options or self.options
        cache_key = f"video:{video_id}"

        async def produce():
            player = await self._player(video_id, opts)
            details = self._extract_details(player, video_id)

            formats = (
                self._extract_formats(player)
                if opts.formats
                else []
            )

            captions = (
                self._extract_captions(player)
                if opts.captions
                else []
            )

            translations = (
                self._extract_translation_languages(player)
                if opts.captions
                else []
            )

            chapters = (
                self._extract_chapters(
                    player,
                    details.get("duration"),
                )
                if opts.chapters
                else []
            )

            status_data = self._mapping(
                player.get("playabilityStatus")
            )

            result = {
                **details,
                "formats": formats,
                "captions": captions,
                "translation_languages": translations,
                "chapters": chapters,
                "format_count": len(formats),
                "caption_count": len(captions),
                "player": {
                    "status": status_data.get("status"),
                    "reason": status_data.get("reason"),
                    "client": player.get("_extractor_client"),
                },
                "extractor": "youtube",
                "webpage_url": f"{_BASE}/watch?v={video_id}",
            }

            if not opts.thumbnails:
                result["thumbnails"] = []
                result["thumbnail"] = None

            if not opts.description:
                result["description"] = None

            if not opts.tags:
                result["keywords"] = []

            if opts.raw_response:
                result["raw_player_response"] = player

            if not opts.metadata:
                result = {
                    "id": video_id,
                    "formats": formats,
                    "format_count": len(formats),
                    "extractor": "youtube",
                    "webpage_url": f"{_BASE}/watch?v={video_id}",
                }

            if opts.formats and not formats:
                raise YouTubeUnavailable(
                    "YouTube returned no playable formats."
                )

            return result

        if force:
            result = await produce()
            await self.metadata_cache.put(cache_key, result)
            return result

        return await self.metadata_cache.get_or_create(
            cache_key,
            produce,
        )

    async def get_info(
        self,
        url_or_id: str,
        *,
        force: bool = False,
        options: YouTubeOptions | None = None,
    ) -> dict[str, Any]:
        return await self.extract(
            url_or_id,
            force=force,
            options=options,
        )

    async def get_formats(
        self,
        url_or_id: str,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        info = await self.extract(url_or_id, force=force)
        return info.get("formats", [])

    async def get_captions(
        self,
        url_or_id: str,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        info = await self.extract(url_or_id, force=force)
        return info.get("captions", [])

    async def get_chapters(
        self,
        url_or_id: str,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        info = await self.extract(url_or_id, force=force)
        return info.get("chapters", [])

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
    def _continuation(data: Any) -> str | None:
        for node in YouTubeExtractor._walk(data):
            renderer = node.get("continuationItemRenderer")
            if not isinstance(renderer, dict):
                continue

            endpoint = renderer.get("continuationEndpoint") or {}
            command = endpoint.get("continuationCommand") or {}
            token = command.get("token")

            if isinstance(token, str) and token:
                return token

        return None

    @staticmethod
    def _playlist_metadata(data: dict[str, Any]) -> dict[str, Any]:
        metadata = data.get("metadata") or {}
        renderer = metadata.get("playlistMetadataRenderer") or {}

        if not isinstance(renderer, dict):
            renderer = {}

        return {
            "title": renderer.get("title"),
            "description": renderer.get("description"),
            "privacy": renderer.get("privacy"),
        }

    def _playlist_items(
        self,
        data: dict[str, Any],
        *,
        start_index: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        output = []
        seen = set()

        for node in self._walk(data):
            renderer = node.get("playlistVideoRenderer")
            if not isinstance(renderer, dict):
                continue

            video_id = renderer.get("videoId")
            if not is_video_id(video_id) or video_id in seen:
                continue

            seen.add(video_id)

            title = self._text(renderer.get("title"))
            thumbnails = self._thumbnails(renderer.get("thumbnail"))

            length_data = self._mapping(renderer.get("lengthText"))
            duration_text = length_data.get("simpleText")

            duration = None
            if isinstance(duration_text, str):
                try:
                    duration = 0
                    for part in duration_text.split(":"):
                        duration = duration * 60 + int(part)
                except ValueError:
                    duration = None

            index_data = self._mapping(renderer.get("index"))
            index_value = safe_int(index_data.get("simpleText"))

            output.append({
                "id": video_id,
                "title": title,
                "url": f"{_BASE}/watch?v={video_id}",
                "thumbnail": (
                    thumbnails[-1]["url"] if thumbnails else None
                ),
                "duration": duration,
                "duration_string": duration_text,
                "index": (
                    index_value
                    if index_value is not None
                    else start_index + len(output) + 1
                ),
                "is_playable": renderer.get("isPlayable", True) is not False,
                "short_byline": (
                    self._text(renderer.get("shortBylineText"))
                    or self._text(renderer.get("longBylineText"))
                ),
            })

            if len(output) >= limit:
                break

        return output

    @staticmethod
    def _innertube_context() -> dict[str, Any]:
        return {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": _ANDROID_VERSION,
                "androidSdkVersion": 35,
                "hl": "en",
                "gl": "US",
            }
        }

    async def _browse_request(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            _BROWSE_URL,
            params={"key": self._api_key},
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": _BASE,
                "Referer": f"{_BASE}/",
                "User-Agent": _ANDROID_UA,
            },
        )

        try:
            try:
                data = response.json()
            except ValueError as exc:
                raise YouTubeRequestError(
                    "YouTube returned invalid browse JSON."
                ) from exc
        finally:
            await response.aclose()

        if not isinstance(data, dict):
            raise YouTubeRequestError(
                "Unexpected playlist response."
            )

        return data

    async def get_playlist_next(
        self,
        playlist_id: str,
        continuation: str,
        *,
        limit: int = 100,
        options: YouTubeOptions | None = None,
    ) -> dict[str, Any]:
        if not isinstance(playlist_id, str) or not playlist_id.strip():
            raise YouTubeExtractorError("Invalid playlist ID.")

        if not isinstance(continuation, str) or not continuation:
            raise YouTubeExtractorError("Invalid continuation token.")

        limit = max(1, min(int(limit), 500))

        data = await self._browse_request({
            "context": self._innertube_context(),
            "continuation": continuation,
        })

        items = self._playlist_items(data, limit=limit)
        next_token = self._continuation(data)

        return {
            "id": playlist_id,
            "items": items,
            "continuation": next_token,
            "has_more": bool(next_token),
        }

    async def get_playlist(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        flat: bool = True,
        options: YouTubeOptions | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        self._ensure_open()

        if not isinstance(playlist_id, str):
            raise YouTubeExtractorError("Invalid playlist ID.")

        playlist_id = playlist_id.strip()

        if playlist_id.startswith(("http://", "https://")):
            parsed = urlparse(playlist_id)
            playlist_id = parse_qs(parsed.query).get("list", [None])[0]

        if not playlist_id or not _PLAYLIST_RE.fullmatch(playlist_id):
            raise YouTubeExtractorError("Invalid YouTube playlist ID.")

        limit = max(1, min(int(limit), 500))
        cache_key = f"playlist:{playlist_id}:{limit}:{bool(flat)}"

        if not refresh:
            cached = await self.playlist_cache.get(cache_key)
            if cached is not None:
                return cached

        data = await self._browse_request({
            "context": self._innertube_context(),
            "browseId": f"VL{playlist_id}",
        })

        metadata = self._playlist_metadata(data)
        items = self._playlist_items(data, limit=limit)
        continuation = self._continuation(data)

        result = {
            "id": playlist_id,
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "privacy": metadata.get("privacy"),
            "url": f"{_BASE}/playlist?list={playlist_id}",
            "video_count": None,
            "items": items,
            "continuation": continuation,
            "has_more": bool(continuation),
            "flat": bool(flat),
        }

        await self.playlist_cache.put(cache_key, result)
        return result



    async def download_format(
        self,
        video_id: str,
        itag: int,
        output: str | Path,
        *,
        options: YouTubeOptions | None = None,
        chunk_size: int = 1024 * 1024,
        resume: bool = True,
        max_url_refreshes: int = 3,
    ) -> dict[str, Any]:
        self._ensure_open()

        video_id = self._video_id(video_id)
        itag = safe_int(itag)

        if itag is None:
            raise YouTubeExtractorError("Invalid format itag.")

        chunk_size = max(
            64 * 1024,
            min(int(chunk_size), 8 * 1024 * 1024),
        )
        max_url_refreshes = max(
            0,
            min(int(max_url_refreshes), 5),
        )

        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        part_path = output_path.with_name(
            output_path.name + ".part"
        )

        async with self._download_semaphore:
            info = await self.extract(
                video_id,
                options=options,
            )

            def find_format(data):
                return next(
                    (
                        item
                        for item in data.get("formats", [])
                        if item.get("itag") == itag
                        and item.get("url")
                    ),
                    None,
                )

            selected = find_format(info)

            if not selected:
                raise YouTubeUnavailable(
                    f"Format itag {itag} is unavailable."
                )

            expected_size = safe_int(
                selected.get("content_length")
            )

            if expected_size is not None and expected_size <= 0:
                expected_size = None

            if resume and part_path.is_file():
                downloaded = part_path.stat().st_size
            else:
                downloaded = 0
                part_path.unlink(missing_ok=True)

            if (
                expected_size is not None
                and downloaded > expected_size
            ):
                part_path.unlink(missing_ok=True)
                downloaded = 0

            if (
                expected_size is not None
                and downloaded == expected_size
            ):
                part_path.replace(output_path)

                return {
                    "id": video_id,
                    "itag": itag,
                    "path": str(output_path),
                    "size": downloaded,
                    "format": selected,
                    "resumed": True,
                    "url_refreshes": 0,
                }

            refreshes = 0
            initial_downloaded = downloaded

            while True:
                if (
                    expected_size is not None
                    and downloaded >= expected_size
                ):
                    break

                current_url = selected.get("url")

                if not current_url:
                    raise YouTubeUnavailable(
                        "Refreshed media URL is missing."
                    )

                range_end = downloaded + chunk_size - 1

                if expected_size is not None:
                    range_end = min(
                        range_end,
                        expected_size - 1,
                    )

                headers = {
                    "User-Agent": _ANDROID_UA,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Range": f"bytes={downloaded}-{range_end}",
                }

                refresh_needed = False

                try:
                    async with self.client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        follow_redirects=True,
                    ) as response:
                        status = response.status_code

                        if status in (401, 403):
                            refresh_needed = True

                        elif status in _TRANSIENT_HTTP:
                            raise YouTubeRequestError(
                                f"Media server returned HTTP {status}."
                            )

                        elif status == 416:
                            total = parse_unsatisfied_range(
                                response.headers.get(
                                    "Content-Range", ""
                                )
                            )

                            if (
                                total is not None
                                and downloaded == total
                            ):
                                expected_size = total
                                break

                            raise YouTubeRequestError(
                                "Media server rejected the requested "
                                "byte range (HTTP 416)."
                            )

                        elif status == 206:
                            parsed = parse_content_range(
                                response.headers.get(
                                    "Content-Range", ""
                                )
                            )

                            if not parsed:
                                raise YouTubeRequestError(
                                    "Missing or invalid Content-Range."
                                )

                            start, end, total = parsed

                            if start != downloaded:
                                raise YouTubeRequestError(
                                    "Unexpected media range offset."
                                )

                            if end > range_end:
                                raise YouTubeRequestError(
                                    "Server returned data beyond "
                                    "the requested range."
                                )

                            if total is not None:
                                if (
                                    expected_size is not None
                                    and expected_size != total
                                ):
                                    raise YouTubeRequestError(
                                        "Media size changed during "
                                        "download."
                                    )

                                expected_size = total

                            expected_chunk = end - start + 1
                            received = 0

                            mode = "ab" if downloaded else "wb"

                            with part_path.open(mode) as file:
                                async for chunk in response.aiter_bytes(
                                    chunk_size
                                ):
                                    if not chunk:
                                        continue

                                    file.write(chunk)
                                    received += len(chunk)

                                file.flush()

                            if received != expected_chunk:
                                raise YouTubeRequestError(
                                    "Incomplete media range: "
                                    f"expected {expected_chunk} bytes, "
                                    f"received {received}."
                                )

                            downloaded += received

                        elif status == 200:
                            # Server ignored Range and returned
                            # the complete resource. Restart safely.
                            content_length = safe_int(
                                response.headers.get(
                                    "Content-Length"
                                )
                            )

                            if content_length is not None:
                                expected_size = content_length

                            downloaded = 0
                            received = 0

                            with part_path.open("wb") as file:
                                async for chunk in response.aiter_bytes(
                                    chunk_size
                                ):
                                    if not chunk:
                                        continue

                                    file.write(chunk)
                                    received += len(chunk)

                                file.flush()

                            downloaded = received

                            if (
                                expected_size is not None
                                and downloaded != expected_size
                            ):
                                raise YouTubeRequestError(
                                    "Full response size mismatch."
                                )

                        else:
                            raise YouTubeRequestError(
                                f"Media download failed: HTTP {status}."
                            )

                except YouTubeExtractorError:
                    raise

                except (
                    httpx.TimeoutException,
                    httpx.TransportError,
                ) as exc:
                    raise YouTubeRequestError(
                        "Media download connection failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

                if refresh_needed:
                    if refreshes >= max_url_refreshes:
                        raise YouTubeRestricted(
                            "Media URL rejected with HTTP 403/401 "
                            f"after {refreshes} URL refreshes."
                        )

                    refreshes += 1

                    info = await self.extract(
                        video_id,
                        options=options,
                        force=True,
                    )

                    selected = find_format(info)

                    if not selected:
                        raise YouTubeUnavailable(
                            "The format is unavailable after "
                            "refreshing its media URL."
                        )

                    refreshed_size = safe_int(
                        selected.get("content_length")
                    )

                    if (
                        refreshed_size is not None
                        and refreshed_size > 0
                    ):
                        if (
                            expected_size is not None
                            and expected_size != refreshed_size
                        ):
                            raise YouTubeRequestError(
                                "Media size changed after URL refresh."
                            )

                        expected_size = refreshed_size

                    continue

                if (
                    expected_size is not None
                    and downloaded > expected_size
                ):
                    raise YouTubeRequestError(
                        "Downloaded data exceeds expected size."
                    )

            if not part_path.is_file():
                raise YouTubeRequestError(
                    "Download produced no output file."
                )

            actual_size = part_path.stat().st_size

            if actual_size != downloaded:
                raise YouTubeRequestError(
                    "Partial file size verification failed."
                )

            if (
                expected_size is not None
                and actual_size != expected_size
            ):
                raise YouTubeRequestError(
                    "Final download size verification failed. "
                    "Partial data has been preserved."
                )

            part_path.replace(output_path)

            return {
                "id": video_id,
                "itag": itag,
                "path": str(output_path),
                "size": actual_size,
                "format": selected,
                "resumed": initial_downloaded > 0,
                "url_refreshes": refreshes,
            }


    async def cache_stats(self) -> dict[str, Any]:
        return {
            "metadata": await self.metadata_cache.stats(),
            "playlists": await self.playlist_cache.stats(),
            "streams": await self.stream_cache.stats(),
        }

    async def clear_cache(self) -> None:
        await asyncio.gather(
            self.metadata_cache.clear(),
            self.playlist_cache.clear(),
            self.stream_cache.clear(),
        )


__all__ = [
    "YouTubeExtractorError",
    "YouTubeUnavailable",
    "YouTubeRestricted",
    "YouTubeRequestError",
    "YouTubeOptions",
    "TTLCache",
    "YouTubeExtractor",
]