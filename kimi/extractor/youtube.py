
from __future__ import annotations

import asyncio
import html
import json
import re
import time

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


_BASE = "https://www.youtube.com"
_MUSIC = "https://music.youtube.com"

_DEFAULT_API_KEY = (
    "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
)

_CLIENT_VERSION = "2.20261123.00.00"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_API_KEY_RE = re.compile(
    r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"'
)
_VERSION_RE = re.compile(
    r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"'
)

_PLAYER_RE = re.compile(
    r"ytInitialPlayerResponse\s*=\s*({.*?})\s*;"
)
_DATA_RE = re.compile(
    r"ytInitialData\s*=\s*({.*?})\s*;"
)


class YouTubeExtractorError(Exception):
    """Base extractor error."""


class YouTubeUnavailable(YouTubeExtractorError):
    """Video or playlist is unavailable."""


class YouTubeRestricted(YouTubeExtractorError):
    """YouTube requires authentication or additional verification."""


class YouTubeRequestError(YouTubeExtractorError):
    """Network or HTTP request failed."""


@dataclass(slots=True)
class YouTubeOptions:
    # Main data
    metadata: bool = True
    formats: bool = True
    thumbnails: bool = True
    description: bool = True
    tags: bool = True
    chapters: bool = True
    captions: bool = True

    # Additional data
    automatic_captions: bool = False
    watch_playlist: bool = True
    related_videos: bool = False
    raw_response: bool = False

    # Resource-heavy features
    comments: bool = False
    hls: bool = False
    dash: bool = False

    # Playlist
    flat_playlist: bool = True
    playlist_limit: int = 100

    # Request behaviour
    retries: int = 2
    timeout: float = 12.0

    def __post_init__(self):
        self.retries = max(0, min(int(self.retries), 5))
        self.playlist_limit = max(
            1, min(int(self.playlist_limit), 500)
        )
        self.timeout = max(3.0, min(float(self.timeout), 60.0))


@dataclass(slots=True)
class CacheEntry:
    value: Any
    expires: float


class TTLCache:
    """Bounded LRU cache with TTL and request de-duplication."""

    def __init__(
        self,
        maxsize: int = 512,
        ttl: int = 21600,
    ):
        self.maxsize = max(1, maxsize)
        self.ttl = max(1, ttl)
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
        async with self._lock:
            self._data[key] = CacheEntry(
                value=value,
                expires=time.monotonic() + (
                    self.ttl if ttl is None else max(1, ttl)
                ),
            )
            self._data.move_to_end(key)

            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def get_or_create(self, key: str, factory, ttl: int | None = None):
        cached = await self.get(key)
        if cached is not None:
            return cached

        async with self._lock:
            task = self._inflight.get(key)

            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[key] = task
                creator = True
            else:
                creator = False

        try:
            result = await asyncio.shield(task)

            if creator:
                await self.put(key, result, ttl)

            return result

        finally:
            if creator:
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._data.clear()

    async def stats(self):
        async with self._lock:
            return {
                "entries": len(self._data),
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

    Metadata and playlist cache: 6 hours.
    Stream URL cache: 5 minutes.
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

        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            proxy=proxy,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies=self._load_cookies(cookies),
        )

        self.metadata_cache = TTLCache(
            maxsize=cache_size,
            ttl=metadata_ttl,
        )
        self.playlist_cache = TTLCache(
            maxsize=max(32, cache_size // 4),
            ttl=metadata_ttl,
        )
        self.stream_cache = TTLCache(
            maxsize=cache_size,
            ttl=stream_ttl,
        )

        self._api_key = self.api_key
        self._client_version = _CLIENT_VERSION
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

                    if len(parts) >= 7:
                        cookies.set(
                            parts[5],
                            parts[6],
                            domain=parts[0],
                            path=parts[2],
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
                        await asyncio.sleep(
                            min(0.4 * (2 ** attempt), 2.0)
                        )
                        continue

                if response.status_code in (401, 403):
                    raise YouTubeRestricted(
                        "YouTube restricted this request. "
                        "Authentication or verification may be required."
                    )

                response.raise_for_status()
                return response

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= retries:
                    raise YouTubeRequestError(str(exc)) from exc

                await asyncio.sleep(
                    min(0.3 * (2 ** attempt), 2.0)
                )

        raise YouTubeRequestError("Request failed.")

    async def _bootstrap(self):
        if self._bootstrapped:
            return

        async with self._bootstrap_lock:
            if self._bootstrapped:
                return

            try:
                response = await self._request(
                    "GET",
                    _BASE,
                )

                page = response.text

                key = _API_KEY_RE.search(page)
                version = _VERSION_RE.search(page)

                if key:
                    self._api_key = key.group(1)

                if version:
                    self._client_version = version.group(1)

            except YouTubeExtractorError:
                # Existing known client configuration remains fallback.
                pass

            self._bootstrapped = True

    async def _player(self, video_id: str, options: YouTubeOptions):
        await self._bootstrap()

        url = f"{_BASE}/youtubei/v1/player"

        payload = {
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": self._client_version,
                    "hl": "en",
                    "gl": "US",
                }
            },
        }

        response = await self._request(
            "POST",
            url,
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
            return response.json()
        except ValueError as exc:
            raise YouTubeRequestError(
                "Invalid player response."
            ) from exc

    @staticmethod
    def _video_id(value: str) -> str:
        value = value.strip()

        if _ID_RE.fullmatch(value):
            return value

        parsed = urlparse(value)
        host = parsed.netloc.lower()

        if host.endswith("youtu.be"):
            video_id = parsed.path.strip("/").split("/")[0]

        elif "youtube.com" in host:
            query = parse_qs(parsed.query)

            video_id = (
                query.get("v", [None])[0]
                or (
                    parsed.path.split("/")[2]
                    if parsed.path.startswith(("/shorts/", "/live/", "/embed/"))
                    and len(parsed.path.split("/")) > 2
                    else None
                )
            )

        else:
            video_id = None

        if not video_id or not _ID_RE.fullmatch(video_id):
            raise YouTubeExtractorError("Invalid YouTube video URL or ID.")

        return video_id

    @staticmethod
    def _text(value: Any) -> str | None:
        if isinstance(value, str):
            return value or None

        if not isinstance(value, dict):
            return None

        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]

        runs = value.get("runs")

        if isinstance(runs, list):
            result = "".join(
                item.get("text", "")
                for item in runs
                if isinstance(item, dict)
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
                stack.extend(item.values())

            elif isinstance(item, list):
                stack.extend(item)

    @staticmethod
    def _duration(value: Any):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _thumbnails(data: Any):
        if not isinstance(data, list):
            return []

        return [
            {
                "url": item["url"],
                "width": item.get("width"),
                "height": item.get("height"),
            }
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
        ]

    @staticmethod
    def _formats(player: dict):
        streaming = player.get("streamingData") or {}

        output = []

        for item in (
            streaming.get("formats", [])
            + streaming.get("adaptiveFormats", [])
        ):
            if not isinstance(item, dict):
                continue

            cipher = item.get("signatureCipher") or item.get("cipher")
            direct_url = item.get("url")
            cipher_data = {}

            if isinstance(cipher, str):
                cipher_data = {
                    key: values[0]
                    for key, values in parse_qs(cipher).items()
                    if values
                }

                # Encrypted signatures must not be treated as direct URLs.
                if cipher_data.get("s"):
                    direct_url = None
                else:
                    direct_url = cipher_data.get("url") or direct_url

            parsed_url = urlparse(direct_url) if direct_url else None
            query = parse_qs(parsed_url.query) if parsed_url else {}

            output.append({
                "itag": item.get("itag"),
                "mime_type": item.get("mimeType"),
                "quality": item.get("quality"),
                "quality_label": item.get("qualityLabel"),
                "width": item.get("width"),
                "height": item.get("height"),
                "fps": item.get("fps"),
                "bitrate": item.get("bitrate"),
                "average_bitrate": item.get("averageBitrate"),
                "audio_quality": item.get("audioQuality"),
                "audio_sample_rate": item.get("audioSampleRate"),
                "audio_channels": item.get("audioChannels"),
                "content_length": item.get("contentLength"),
                "approximate_content_length": item.get(
                    "approximateContentLength"
                ),
                "codecs": (
                    item.get("mimeType", "").split("codecs=")[-1]
                    .strip('"')
                    if "codecs=" in item.get("mimeType", "")
                    else None
                ),
                "has_audio": item.get("audioQuality") is not None
                    or item.get("audioChannels") is not None,
                "has_video": item.get("width") is not None,
                "url": direct_url,
                "requires_signature": bool(cipher_data.get("s")),
                "requires_n_transform": bool(query.get("n")),
                "signature_cipher": cipher if cipher_data.get("s") else None,
            })

        return output

    @staticmethod
    def _captions(player: dict, automatic: bool):
        tracks = (
            player.get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks", [])
        )

        output = []

        for item in tracks:
            if not isinstance(item, dict):
                continue

            is_auto = bool(item.get("kind") == "asr")

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
    def _parse_player(
        video_id: str,
        player: dict,
        options: YouTubeOptions,
    ):
        details = player.get("videoDetails") or {}
        status = player.get("playabilityStatus") or {}
        micro = player.get("microformat", {}).get(
            "playerMicroformatRenderer", {}
        )

        if status.get("status") not in (None, "OK"):
            reason = status.get("reason") or status.get("status")

            if status.get("status") in (
                "LOGIN_REQUIRED",
                "AGE_CHECK_REQUIRED",
                "CONTENT_CHECK_REQUIRED",
            ):
                raise YouTubeRestricted(str(reason))

            raise YouTubeUnavailable(str(reason))

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
                details.get("keywords", [])
                if options.tags else []
            ),
            "thumbnails": (
                YouTubeExtractor._thumbnails(
                    details.get("thumbnail", {}).get("thumbnails")
                )
                if options.thumbnails else []
            ),
            "formats": [],
            "captions": [],
            "automatic_captions": [],
            "chapters": [],
            "related_videos": [],
            "playlist": None,
            "playability_status": status.get("status"),
            "playability_reason": status.get("reason"),
        }

        if options.formats:
            result["formats"] = YouTubeExtractor._formats(player)

        if options.captions:
            captions = YouTubeExtractor._captions(
                player,
                options.automatic_captions,
            )

            result["captions"] = [
                item for item in captions if not item["automatic"]
            ]

            if options.automatic_captions:
                result["automatic_captions"] = [
                    item for item in captions if item["automatic"]
                ]

        if options.chapters:
            result["chapters"] = (
                micro.get("chapters") or []
            )

        if options.raw_response:
            result["raw_response"] = player

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

        # Keep cache keys independent of caller-specific settings.
        cache_key = f"video:{video_id}"

        async def fetch():
            player = await self._player(video_id, options)

            return self._parse_player(
                video_id,
                player,
                options,
            )

        if refresh:
            result = await fetch()
            await self.metadata_cache.put(cache_key, result)
            return result

        result = await self.metadata_cache.get_or_create(
            cache_key,
            fetch,
        )

        # Do not mutate the shared cached object.
        result = json.loads(json.dumps(result))

        # Options control returned fields too.
        if not options.formats:
            result["formats"] = []

        if not options.thumbnails:
            result["thumbnails"] = []

        if not options.description:
            result["description"] = None

        if not options.tags:
            result["tags"] = []

        if not options.captions:
            result["captions"] = []
            result["automatic_captions"] = []

        if not options.automatic_captions:
            result["automatic_captions"] = []

        if not options.chapters:
            result["chapters"] = []

        if options.raw_response is False:
            result.pop("raw_response", None)

        # Stream URLs have their own short cache.
        if options.formats:
            stream_key = f"streams:{video_id}"

            async def fetch_streams():
                player = await self._player(video_id, options)
                return self._formats(player)

            streams = await self.stream_cache.get_or_create(
                stream_key,
                fetch_streams,
                ttl=300,
            )

            result["formats"] = streams

        return result

    @staticmethod
    def _playlist_items(data: Any):
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

            if not isinstance(video_id, str) or not _ID_RE.fullmatch(video_id):
                continue

            if video_id in seen:
                continue

            seen.add(video_id)

            output.append({
                "id": video_id,
                "title": YouTubeExtractor._text(
                    renderer.get("title")
                ),
                "url": f"{_BASE}/watch?v={video_id}",
                "thumbnail": YouTubeExtractor._thumbnails(
                    renderer.get("thumbnail", {}).get("thumbnails")
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
    def _continuation(data: Any):
        for node in YouTubeExtractor._walk(data):
            endpoint = node.get("continuationEndpoint")

            if not isinstance(endpoint, dict):
                continue

            token = (
                endpoint.get("continuationCommand", {})
                .get("token")
            )

            if token:
                return token

        return None

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
        limit = max(1, min(int(limit), options.playlist_limit))

        if not playlist_id:
            raise YouTubeExtractorError("Playlist ID is required.")

        cache_key = f"playlist:{playlist_id}:{limit}:{flat}"

        async def fetch():
            url = f"{_BASE}/playlist"
            response = await self._request(
                "GET",
                url,
                options=options,
                params={"list": playlist_id},
            )

            page = response.text
            match = _DATA_RE.search(page)

            if not match:
                raise YouTubeRequestError(
                    "Could not find playlist data in YouTube response."
                )

            try:
                data = json.loads(match.group(1))
            except ValueError as exc:
                raise YouTubeRequestError(
                    "Invalid playlist response."
                ) from exc

            metadata = {}
            for node in self._walk(data):
                renderer = node.get("playlistMetadataRenderer")
                if isinstance(renderer, dict):
                    metadata = renderer
                    break

            items = self._playlist_items(data)
            token = self._continuation(data)

            # Flat mode intentionally avoids fetching every video.
            return {
                "id": playlist_id,
                "title": metadata.get("title"),
                "description": metadata.get("description"),
                "url": f"{_BASE}/playlist?list={playlist_id}",
                "video_count": metadata.get("videoCount"),
                "items": items[:limit],
                "continuation": token if len(items) < limit else None,
                "has_more": bool(token and len(items) < limit),
                "flat": flat,
            }

        if refresh:
            result = await fetch()
            await self.playlist_cache.put(cache_key, result)
            return result

        result = await self.playlist_cache.get_or_create(
            cache_key,
            fetch,
        )

        return json.loads(json.dumps(result))

    async def get_playlist_next(
        self,
        playlist_id: str,
        continuation: str,
        *,
        limit: int = 100,
        options: YouTubeOptions | None = None,
    ):
        options = options or YouTubeOptions()
        limit = max(1, min(int(limit), options.playlist_limit))

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
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise YouTubeRequestError(
                "Invalid playlist continuation response."
            ) from exc

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
