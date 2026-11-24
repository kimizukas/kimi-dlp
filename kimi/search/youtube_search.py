from __future__ import annotations

import asyncio
import copy
import random
import re
import time
import uuid

from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import httpx





class YouTubeSearchError(Exception):
    """YouTube search request or response error."""


_BASE = "https://www.youtube.com"
_MUSIC = "https://music.youtube.com"

_FILTERS: dict[str, tuple[str | None, str]] = {
    "all": (None, "web"),
    "videos": ("EgIQAfABAQ==", "web"),
    "channels": ("EgIQAg==", "web"),
    "songs": ("EgWKAQIIAWoKEAoQAxAEEAkQBQ==", "music"),
    "albums": ("EgWKAQIYAWoKEAoQAxAEEAkQBQ==", "music"),
    "artists": ("EgWKAQIgAWoKEAoQAxAEEAkQBQ==", "music"),
    "playlists": ("EgWKAQIoAWoKEAoQAxAEEAkQBQ==", "music"),
    "community_playlists": ("EgeKAQQoAEABagoQChADEAQQCRAF", "music"),
    "featured_playlists": ("EgeKAQQoADgBagoQChADEAQQCRAF", "music"),
}

_CLIENTS = {
    "web": {
        "name": "WEB",
        "id": "1",
        "version": "2.20261123.00.00",
        "url": _BASE,
    },
    "music": {
        "name": "WEB_REMIX",
        "id": "67",
        "version": "1.20260707.12.00",
        "url": _MUSIC,
    },
}

_RE_API_KEY = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_RE_VERSION = re.compile(
    r'"INNERTUBE_(?:CONTEXT_)?CLIENT_VERSION"\s*:\s*"([^"]+)"'
)
_RE_VISITOR = re.compile(r'"(?:VISITOR_DATA|visitorData)"\s*:\s*"([^"]+)"')
_DURATION_RE = re.compile(r"\d+(?::\d{2}){1,2}")
_NUMBER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([kmb])?(?![a-z])",
    re.IGNORECASE,
)
_VIEWS_RE = re.compile(r"\b(?:views?|plays?)\b", re.IGNORECASE)

_CURSOR_PREFIX = "yts:"


def _dig(data: Any, *path: str | int) -> Any:
    for key in path:
        if isinstance(key, int):
            if not isinstance(data, list) or key >= len(data):
                return None
            data = data[key]
        elif isinstance(data, dict):
            data = data.get(key)
        else:
            return None
    return data


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None

    if not isinstance(value, dict):
        return None

    for key in ("simpleText", "content"):
        result = value.get(key)
        if isinstance(result, str) and result:
            return result

    runs = value.get("runs")
    if isinstance(runs, list):
        result = "".join(
            item.get("text", "")
            for item in runs
            if isinstance(item, dict)
        )
        return result or None

    return None


def _number(value: str | None) -> int | None:
    if not value:
        return None

    value = value.lower().replace(",", "").strip()
    match = _NUMBER_RE.search(value)

    if not match:
        return 0 if re.search(r"\bno\b", value) else None

    multiplier = {
        None: 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }[match.group(2)]

    return round(float(match.group(1)) * multiplier)


def _duration(value: str | None) -> int | None:
    if not value:
        return None

    value = value.strip()
    if not _DURATION_RE.fullmatch(value):
        return None

    seconds = 0
    for part in value.split(":"):
        seconds = seconds * 60 + int(part)

    return seconds


def _abs_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return _BASE + url
    return url


def _pick_thumbnail(thumbs: Any) -> str | None:
    if isinstance(thumbs, list):
        for item in reversed(thumbs):
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    return _abs_url(url)
    return None


def _thumbnail(renderer: dict) -> str | None:
    candidates = (
        _dig(renderer, "thumbnail", "thumbnails"),
        _dig(
            renderer,
            "thumbnail",
            "musicThumbnailRenderer",
            "thumbnail",
            "thumbnails",
        ),
        _dig(renderer, "thumbnails", 0, "thumbnails"),
        _dig(
            renderer,
            "thumbnailRenderer",
            "playlistVideoThumbnailRenderer",
            "thumbnail",
            "thumbnails",
        ),
        _dig(
            renderer,
            "thumbnailRenderer",
            "playlistCustomThumbnailRenderer",
            "thumbnail",
            "thumbnails",
        ),
    )

    for thumbs in candidates:
        result = _pick_thumbnail(thumbs)
        if result:
            return result

    return None


def _badges(renderer: dict) -> list[str]:
    result = []

    for badge in renderer.get("badges") or []:
        meta = _dig(badge, "metadataBadgeRenderer")
        if not isinstance(meta, dict):
            continue

        label = (
            meta.get("label")
            or _dig(meta, "accessibilityData", "label")
            or meta.get("tooltip")
        )

        if isinstance(label, str) and label:
            result.append(label)

    return result


def _is_verified(renderer: dict) -> bool:
    for key in ("ownerBadges", "badges"):
        for badge in renderer.get(key) or []:
            meta = _dig(badge, "metadataBadgeRenderer")
            if not isinstance(meta, dict):
                continue

            blob = " ".join(
                str(meta.get(k, ""))
                for k in ("style", "tooltip", "label")
            ).lower()

            if (
                "verified" in blob
                or "official artist" in blob
                or "official_artist" in blob
            ):
                return True

    return False


def _channel_info(renderer: dict) -> dict:
    owner = (
        renderer.get("ownerText")
        or renderer.get("shortBylineText")
        or renderer.get("longBylineText")
    )

    channel_id = None
    path = None
    handle = None

    runs = owner.get("runs") if isinstance(owner, dict) else None

    for run in runs or []:
        if not isinstance(run, dict):
            continue

        browse = _dig(
            run,
            "navigationEndpoint",
            "browseEndpoint",
        )

        if isinstance(browse, dict):
            channel_id = channel_id or browse.get("browseId")
            path = path or browse.get("canonicalBaseUrl")

        text = run.get("text") or ""
        if text.startswith("@"):
            handle = handle or text

    if path and path.startswith("/@"):
        handle = handle or path[1:]

    if not path and channel_id:
        path = f"/channel/{channel_id}"

    return {
        "channel_name": _text(owner),
        "channel_id": channel_id,
        "channel_url": _BASE + path if path and path.startswith("/") else None,
        "channel_handle": handle,
        "channel_verified": _is_verified(renderer),
    }


def _video(renderer: dict, source: str = "youtube") -> dict | None:
    video_id = renderer.get("videoId")
    if not isinstance(video_id, str) or not video_id:
        return None

    status = next(
        (
            item["thumbnailOverlayTimeStatusRenderer"]
            for item in renderer.get("thumbnailOverlays") or []
            if isinstance(item, dict)
            and isinstance(
                item.get("thumbnailOverlayTimeStatusRenderer"),
                dict,
            )
        ),
        {},
    )

    style = status.get("style")

    duration_text = _text(renderer.get("lengthText")) or _text(
        status.get("text")
    )

    if duration_text and not _DURATION_RE.fullmatch(duration_text.strip()):
        duration_text = None

    view_text = (
        _text(renderer.get("viewCountText"))
        or _text(renderer.get("shortViewCountText"))
    )

    description = (
        _text(renderer.get("descriptionSnippet"))
        or _text(
            _dig(
                renderer,
                "detailedMetadataSnippets",
                0,
                "snippetText",
            )
        )
    )

    badges = _badges(renderer)
    labels = {item.strip().lower() for item in badges}

    nav = renderer.get("navigationEndpoint")
    nav_url = (
        _dig(nav, "commandMetadata", "webCommandMetadata", "url")
        or ""
    )

    is_live = (
        style == "LIVE"
        or bool(labels & {"live", "live now"})
        or "watching" in (view_text or "").lower()
    )

    is_short = (
        style == "SHORTS"
        or (
            isinstance(nav, dict)
            and "reelWatchEndpoint" in nav
        )
        or nav_url.startswith("/shorts/")
    )

    return {
        "type": "video",
        "source": source,
        "id": video_id,
        "title": _text(renderer.get("title")),
        "url": f"{_BASE}/watch?v={video_id}",
        "thumbnail": _thumbnail(renderer),
        "duration": duration_text,
        "duration_seconds": _duration(duration_text),
        "views": _number(view_text),
        "views_text": view_text,
        "published": _text(renderer.get("publishedTimeText")),
        "description": description,
        "badges": badges,
        **_channel_info(renderer),
        "is_live": is_live,
        "is_short": is_short,
        "raw_type": "videoRenderer",
    }


def _channel(renderer: dict) -> dict | None:
    channel_id = (
        renderer.get("channelId")
        or _dig(
            renderer,
            "navigationEndpoint",
            "browseEndpoint",
            "browseId",
        )
    )

    if not channel_id:
        return None

    path = _dig(
        renderer,
        "navigationEndpoint",
        "browseEndpoint",
        "canonicalBaseUrl",
    )

    handle = subscribers = video_count = None

    for value in (
        _text(renderer.get("subscriberCountText")),
        _text(renderer.get("videoCountText")),
    ):
        if not value:
            continue

        lowered = value.lower()

        if value.startswith("@"):
            handle = value
        elif "subscriber" in lowered:
            subscribers = value
        elif "video" in lowered:
            video_count = value

    if not handle and path and path.startswith("/@"):
        handle = path[1:]

    return {
        "type": "channel",
        "id": channel_id,
        "title": _text(renderer.get("title")),
        "url": (
            _BASE + path
            if path and path.startswith("/")
            else f"{_BASE}/channel/{channel_id}"
        ),
        "thumbnail": _thumbnail(renderer),
        "handle": handle,
        "subscribers": subscribers,
        "subscribers_count": _number(subscribers),
        "video_count": video_count,
        "description": _text(renderer.get("descriptionSnippet")),
        "badges": _badges(renderer),
        "verified": _is_verified(renderer),
        "raw_type": "channelRenderer",
    }


def _playlist(renderer: dict) -> dict | None:
    playlist_id = (
        renderer.get("playlistId")
        or _dig(
            renderer,
            "navigationEndpoint",
            "watchEndpoint",
            "playlistId",
        )
    )

    if not playlist_id:
        return None

    owner = (
        renderer.get("shortBylineText")
        or renderer.get("longBylineText")
        or renderer.get("ownerText")
    )

    return {
        "type": "playlist",
        "id": playlist_id,
        "title": _text(renderer.get("title")),
        "url": f"{_BASE}/playlist?list={playlist_id}",
        "thumbnail": _thumbnail(renderer),
        "owner": _text(owner),
        "video_count": (
            _text(renderer.get("videoCountText"))
            or _text(renderer.get("videoCountShortText"))
            or _text(renderer.get("videoCount"))
        ),
        "raw_type": "playlistRenderer",
    }


def _lockup(renderer: dict) -> dict | None:
    if renderer.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
        return None

    playlist_id = renderer.get("contentId")

    if not isinstance(playlist_id, str) or not playlist_id:
        return None

    meta = _dig(renderer, "metadata", "lockupMetadataViewModel")

    video_count = None

    for node in _walk(renderer.get("contentImage") or {}):
        badge = node.get("thumbnailBadgeViewModel")
        if isinstance(badge, dict):
            video_count = _text(badge.get("text"))
            if video_count:
                break

    return {
        "type": "playlist",
        "id": playlist_id,
        "title": _text(_dig(meta, "title")),
        "url": f"{_BASE}/playlist?list={playlist_id}",
        "thumbnail": _pick_thumbnail(
            _dig(
                renderer,
                "contentImage",
                "collectionThumbnailViewModel",
                "primaryThumbnail",
                "thumbnailViewModel",
                "image",
                "sources",
            )
        ),
        "owner": _text(
            _dig(
                meta,
                "metadata",
                "contentMetadataViewModel",
                "metadataRows",
                0,
                "metadataParts",
                0,
                "text",
            )
        ),
        "video_count": video_count,
        "raw_type": "lockupViewModel",
    }


def _music_video_id(renderer: dict, title_cell: Any) -> str | None:
    candidates = (
        _dig(renderer, "playlistItemData", "videoId"),
        _dig(
            renderer,
            "overlay",
            "musicItemThumbnailOverlayRenderer",
            "content",
            "musicPlayButtonRenderer",
            "playNavigationEndpoint",
            "watchEndpoint",
            "videoId",
        ),
        _dig(
            renderer,
            "navigationEndpoint",
            "watchEndpoint",
            "videoId",
        ),
        _dig(
            title_cell,
            "runs",
            0,
            "navigationEndpoint",
            "watchEndpoint",
            "videoId",
        ),
    )

    return next(
        (
            item
            for item in candidates
            if isinstance(item, str) and item
        ),
        None,
    )


def _music_item(renderer: dict) -> dict | None:
    columns = renderer.get("flexColumns") or []

    def cell(index: int) -> Any:
        return _dig(
            columns,
            index,
            "musicResponsiveListItemFlexColumnRenderer",
            "text",
        )

    title_cell = cell(0)
    title = _text(title_cell) or _text(renderer.get("title"))
    sub_cell = cell(1)

    runs = sub_cell.get("runs") if isinstance(sub_cell, dict) else None

    artists = []
    album = None
    duration = None
    extras = []

    for run in runs or []:
        if not isinstance(run, dict):
            continue

        value = (run.get("text") or "").strip()

        if not value or value == "•":
            continue

        browse_id = (
            _dig(
                run,
                "navigationEndpoint",
                "browseEndpoint",
                "browseId",
            )
            or ""
        )

        if browse_id.startswith(("UC", "MPLA")):
            artists.append({"name": value, "id": browse_id})
        elif browse_id.startswith("MPRE"):
            album = {"name": value, "id": browse_id}
        elif _DURATION_RE.fullmatch(value):
            duration = value
        else:
            extras.append(value)

    subtitle = " • ".join(extras) or None
    first_artist = artists[0] if artists else {}

    video_id = _music_video_id(renderer, title_cell)

    if video_id:
        views = next(
            (
                _number(value)
                for value in extras
                if _VIEWS_RE.search(value)
            ),
            None,
        )

        return {
            "type": "song",
            "id": video_id,
            "title": title,
            "url": f"{_BASE}/watch?v={video_id}",
            "thumbnail": _thumbnail(renderer),
            "artists": [item["name"] for item in artists],
            "album": album["name"] if album else None,
            "album_id": album["id"] if album else None,
            "duration": duration,
            "duration_seconds": _duration(duration),
            "views": views,
            "subtitle": subtitle,
            "channel_name": first_artist.get("name"),
            "channel_id": first_artist.get("id"),
            "channel_url": (
                f"{_MUSIC}/channel/{first_artist['id']}"
                if first_artist
                else None
            ),
            "channel_handle": None,
            "raw_type": "musicResponsiveListItemRenderer",
        }

    nav = renderer.get("navigationEndpoint")
    browse_id = _dig(nav, "browseEndpoint", "browseId") or ""
    playlist_id = _dig(nav, "watchEndpoint", "playlistId")

    if browse_id.startswith("VL"):
        playlist_id = browse_id[2:]

    if playlist_id:
        return {
            "type": "playlist",
            "id": playlist_id,
            "title": title,
            "url": f"{_BASE}/playlist?list={playlist_id}",
            "thumbnail": _thumbnail(renderer),
            "owner": first_artist.get("name"),
            "subtitle": subtitle,
            "raw_type": "musicResponsiveListItemRenderer",
        }

    if browse_id:
        if browse_id.startswith("MPRE"):
            kind = "album"
        elif browse_id.startswith(("UC", "MPLA")):
            kind = "artist"
        else:
            kind = "music_entity"

        return {
            "type": kind,
            "id": browse_id,
            "title": title,
            "url": f"{_MUSIC}/browse/{browse_id}",
            "thumbnail": _thumbnail(renderer),
            "artists": [item["name"] for item in artists],
            "subscribers": next(
                (
                    value
                    for value in extras
                    if "subscriber" in value.lower()
                ),
                None,
            ),
            "subtitle": subtitle,
            "raw_type": "musicResponsiveListItemRenderer",
        }

    return None


def _walk(data: Any) -> Iterator[dict]:
    stack = [data]

    while stack:
        node = stack.pop()

        if isinstance(node, dict):
            yield node
            children = [
                value
                for value in node.values()
                if isinstance(value, (dict, list))
            ]
        elif isinstance(node, list):
            children = [
                value
                for value in node
                if isinstance(value, (dict, list))
            ]
        else:
            continue

        stack.extend(reversed(children))


def _continuation(data: Any) -> str | None:
    for node in _walk(data):
        token = (
            _dig(
                node,
                "continuationEndpoint",
                "continuationCommand",
                "token",
            )
            or _dig(node, "continuationCommand", "token")
            or _dig(node, "nextContinuationData", "continuation")
            or node.get("continuation")
        )

        if isinstance(token, str) and token:
            return token

    return None


_HANDLERS = (
    ("videoRenderer", _video),
    ("gridVideoRenderer", _video),
    ("channelRenderer", _channel),
    ("gridChannelRenderer", _channel),
    ("playlistRenderer", _playlist),
    ("gridPlaylistRenderer", _playlist),
    ("lockupViewModel", _lockup),
    ("musicResponsiveListItemRenderer", _music_item),
)


def _parse(data: dict) -> tuple[list[dict], str | None]:
    results = []
    seen = set()

    for node in _walk(data):
        for key, handler in _HANDLERS:
            renderer = node.get(key)

            if not isinstance(renderer, dict):
                continue

            item = handler(renderer)

            if item and item.get("id"):
                identity = (item["type"], item["id"])

                if identity not in seen:
                    seen.add(identity)
                    results.append(item)

            break

    return results, _continuation(data)


class _TTLCache:
    """Bounded TTL cache."""

    def __init__(self, maxsize: int, ttl: float):
        if maxsize < 1:
            raise ValueError("cache maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("cache ttl must be > 0")

        self.maxsize = maxsize
        self.ttl = ttl
        self.data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key):
        entry = self.data.get(key)

        if entry is None:
            return None

        expires, value = entry

        if expires <= time.monotonic():
            self.data.pop(key, None)
            return None

        self.data.move_to_end(key)
        return value

    def set(self, key, value):
        now = time.monotonic()
        self.data.pop(key, None)
        self.data[key] = (now + self.ttl, value)

        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)

    def clear(self):
        self.data.clear()

    def stats(self):
        now = time.monotonic()
        expired = [
            key
            for key, (expires, _) in self.data.items()
            if expires <= now
        ]

        for key in expired:
            self.data.pop(key, None)

        return {
            "size": len(self.data),
            "maxsize": self.maxsize,
            "ttl": self.ttl,
        }


class YouTubeSearch:
    """Async YouTube and YouTube Music search client."""

    _CONFIG_TTL = 3600.0
    _MAX_PAGES = 5
    _RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        concurrency: int = 10,
        cache_size: int = 256,
        cache_ttl: float = 30.0,
        retries: int = 2,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise ValueError("retries must be an integer")

        self.timeout = timeout
        self.retries = max(0, retries)
        self.semaphore = asyncio.Semaphore(concurrency)

        self.cache = _TTLCache(cache_size, cache_ttl)
        self._leftovers = _TTLCache(cache_size, 600.0)

        self.inflight: dict[tuple, asyncio.Task] = {}

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )

        self.client.cookies.set("SOCS", "CAI", domain=".youtube.com")

        self._configs: dict[str, tuple[float, dict]] = {}
        self._config_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self._closed:
            return

        self._closed = True

        tasks = list(self.inflight.values())

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self.client.aclose()

    async def close_client(self):
        await self.close()

    # --------------------------------------------------
    # Configuration
    # --------------------------------------------------

    async def _get_config(self, site: str) -> dict:
        cached = self._configs.get(site)

        if cached and cached[0] > time.monotonic():
            return cached[1]

        async with self._config_lock:
            cached = self._configs.get(site)

            if cached and cached[0] > time.monotonic():
                return cached[1]

            spec = _CLIENTS[site]

            try:
                response = await self.client.get(
                    spec["url"],
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                response.raise_for_status()

            except httpx.HTTPError as exc:
                raise YouTubeSearchError(
                    f"YouTube configuration request failed: {exc}"
                ) from exc

            html = response.text

            api_key = _RE_API_KEY.search(html)
            version = _RE_VERSION.search(html)
            visitor = _RE_VISITOR.search(html)

            # Music homepage frequently omits these fields.
            # Use its configured WEB_REMIX version and obtain the
            # public Innertube API key from the regular homepage.
            if site == "music":
                web_config = await self._get_config("web")

                config = {
                    "api_key": web_config["api_key"],
                    "version": (
                        version.group(1)
                        if version
                        else spec["version"]
                    ),
                    "visitor": (
                        visitor.group(1)
                        if visitor
                        else web_config["visitor"]
                    ),
                }

            else:
                if not api_key:
                    raise YouTubeSearchError(
                        "Could not find YouTube Innertube API key."
                    )

                config = {
                    "api_key": api_key.group(1),
                    "version": (
                        version.group(1)
                        if version
                        else spec["version"]
                    ),
                    "visitor": visitor.group(1) if visitor else None,
                }

            self._configs[site] = (
                time.monotonic() + self._CONFIG_TTL,
                config,
            )

            return config

    # --------------------------------------------------
    # HTTP
    # --------------------------------------------------

    @staticmethod
    def _delay(
        attempt: int,
        response: httpx.Response | None = None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")

            try:
                return min(float(retry_after), 10.0)
            except (TypeError, ValueError):
                pass

        return 0.4 * (2 ** attempt) + random.uniform(0, 0.2)

    async def _post(
        self,
        url: str,
        body: dict,
        params: dict,
        headers: dict,
    ) -> dict:
        last_error = None

        for attempt in range(self.retries + 1):
            try:
                async with self.semaphore:
                    response = await self.client.post(
                        url,
                        params=params,
                        json=body,
                        headers=headers,
                    )

                if (
                    response.status_code in self._RETRY_STATUS
                    and attempt < self.retries
                ):
                    await asyncio.sleep(self._delay(attempt, response))
                    continue

                response.raise_for_status()

                try:
                    data = response.json()
                except ValueError as exc:
                    raise YouTubeSearchError(
                        "YouTube returned invalid JSON."
                    ) from exc

                if not isinstance(data, dict):
                    raise YouTubeSearchError(
                        "Unexpected YouTube response."
                    )

                return data

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code

                raise YouTubeSearchError(
                    f"YouTube returned HTTP {status}: "
                    f"{exc.response.text[:500]}"
                ) from exc

            except httpx.RequestError as exc:
                last_error = exc

                if attempt < self.retries:
                    await asyncio.sleep(self._delay(attempt))
                    continue

                break

        raise YouTubeSearchError(
            f"YouTube request failed: {last_error}"
        ) from last_error

    async def _request(
        self,
        query: str,
        *,
        filter_name: str,
        continuation: str | None = None,
    ) -> dict:
        search_params, site = _FILTERS[filter_name]
        spec = _CLIENTS[site]
        config = await self._get_config(site)

        client = {
            "clientName": spec["name"],
            "clientVersion": config["version"],
            "hl": "en",
            "gl": "US",
        }

        headers = {
            "Origin": spec["url"],
            "Referer": spec["url"] + "/",
            "X-Youtube-Client-Name": spec["id"],
            "X-Youtube-Client-Version": config["version"],
        }

        if config.get("visitor"):
            client["visitorData"] = config["visitor"]
            headers["X-Goog-Visitor-Id"] = config["visitor"]

        params = {"prettyPrint": "false"}

        if config.get("api_key"):
            params["key"] = config["api_key"]

        body: dict[str, Any] = {
            "context": {
                "client": client,
                "user": {},
            }
        }

        if continuation:
            body["continuation"] = continuation

            if site == "music":
                params["ctoken"] = continuation
                params["continuation"] = continuation

        else:
            body["query"] = query

            if search_params:
                body["params"] = search_params

        return await self._post(
            f"{spec['url']}/youtubei/v1/search",
            body,
            params,
            headers,
        )

    async def _fetch_page(
        self,
        query: str,
        filter_name: str,
        continuation: str | None = None,
    ) -> tuple[list[dict], str | None]:
        data = await self._request(
            query,
            filter_name=filter_name,
            continuation=continuation,
        )
        return _parse(data)

    # --------------------------------------------------
    # Cache and concurrent requests
    # --------------------------------------------------

    async def _first_page(
        self,
        query: str,
        filter_name: str,
    ) -> tuple[list[dict], str | None]:
        key = (
            " ".join(query.split()).casefold(),
            filter_name,
        )

        cached = self.cache.get(key)

        if cached is not None:
            return copy.deepcopy(cached)

        task = self.inflight.get(key)

        if task is None:
            task = asyncio.create_task(
                self._load_first(key, query, filter_name)
            )

            self.inflight[key] = task
            task.add_done_callback(
                lambda current, cache_key=key:
                self._forget(cache_key, current)
            )

        return copy.deepcopy(await asyncio.shield(task))

    async def _load_first(self, key, query, filter_name):
        result = await self._fetch_page(query, filter_name)
        self.cache.set(key, result)
        return result

    def _forget(self, key, task):
        if self.inflight.get(key) is task:
            self.inflight.pop(key, None)

        if not task.cancelled():
            task.exception()

    def clear_cache(self):
        self.cache.clear()
        self._leftovers.clear()

    def get_cache_stats(self):
        return {
            "search": self.cache.stats(),
            "continuations": self._leftovers.stats(),
            "inflight": len(self.inflight),
            "configs": len(self._configs),
        }

    # --------------------------------------------------
    # Pagination
    # --------------------------------------------------

    def _stash(self, token, leftover):
        cursor = _CURSOR_PREFIX + uuid.uuid4().hex
        self._leftovers.set(cursor, (token, leftover))
        return cursor

    def _resume(self, cursor):
        if not cursor.startswith(_CURSOR_PREFIX):
            return [], cursor

        entry = self._leftovers.get(cursor)

        if entry is None:
            raise YouTubeSearchError(
                "Continuation expired. Please search again."
            )

        token, items = entry
        return copy.deepcopy(items), token

    # --------------------------------------------------
    # Public search API
    # --------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        filter: str = "all",
        limit: int = 20,
        continuation: str | None = None,
    ) -> dict:
        if self._closed:
            raise YouTubeSearchError("Client is closed.")

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        if not isinstance(filter, str):
            raise ValueError("filter must be a string")

        filter = filter.lower().strip()

        if filter not in _FILTERS:
            raise ValueError(
                f"Unsupported filter: {filter}. "
                f"Available: {', '.join(_FILTERS)}"
            )

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError(
                "limit must be an integer between 1 and 100"
            )

        if continuation is not None and not isinstance(
            continuation, str
        ):
            raise ValueError("continuation must be a string")

        query = query.strip()

        if continuation:
            items, token = self._resume(continuation)
        else:
            items, token = await self._first_page(query, filter)

        results = []
        seen = set()

        def add(batch):
            for item in batch:
                identity = (item["type"], item["id"])

                if identity not in seen:
                    seen.add(identity)
                    results.append(item)

        add(items)

        pages = 1

        while (
            len(results) < limit
            and token
            and pages < self._MAX_PAGES
        ):
            items, token = await self._fetch_page(
                query,
                filter,
                token,
            )

            add(items)
            pages += 1

        leftover = results[limit:]
        results = results[:limit]

        cursor = self._stash(token, leftover) if leftover else token

        return {
            "query": query,
            "filter": filter,
            "results": results,
            "continuation": cursor,
            "has_more": bool(cursor),
            "count": len(results),
        }

    async def next_page(
        self,
        previous_result: dict,
        *,
        limit: int = 20,
    ) -> dict:
        token = previous_result.get("continuation")

        if not token:
            return {
                "query": previous_result.get("query"),
                "filter": previous_result.get("filter"),
                "results": [],
                "continuation": None,
                "has_more": False,
                "count": 0,
            }

        return await self.search(
            previous_result.get("query", ""),
            filter=previous_result.get("filter", "all"),
            limit=limit,
            continuation=token,
        )

    async def search_music(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="songs", limit=limit)

    async def search_videos(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="videos", limit=limit)

    async def search_songs(self, query: str, *, limit: int = 20):
        return await self.search_music(query, limit=limit)

    async def search_channels(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="channels", limit=limit)

    async def search_albums(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="albums", limit=limit)

    async def search_artists(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="artists", limit=limit)

    async def search_playlists(self, query: str, *, limit: int = 20):
        return await self.search(query, filter="playlists", limit=limit)

    async def ytsearch(self, query: str, *, limit: int = 20):
        return await self.search(query, limit=limit)
