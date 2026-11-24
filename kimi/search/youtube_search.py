
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx


__all__ = ["YouTubeSearch", "YouTubeSearchError"]


class YouTubeSearchError(Exception):
    """YouTube search request or response error."""


_BASE = "https://www.youtube.com"
_MUSIC = "https://music.youtube.com"

_FILTERS = {
    "all": None,
    "videos": "EgIQAfABAQ==",
    "songs": "EgWKAQIIAWoKEAoQAxAEEAkQBQ==",
    "albums": "EgWKAQIYAWoKEAoQAxAEEAkQBQ==",
    "artists": "EgWKAQIgAWoKEAoQAxAEEAkQBQ==",
    "playlists": "EgWKAQIoAWoKEAoQAxAEEAkQBQ==",
    "community_playlists": "EgeKAQQoAEABagoQChADEAQQCRAF",
    "featured_playlists": "EgeKAQQoADgBagwQAxAJEAQQDhAKEAU==",
}

_CLIENTS = {
    "web": {
        "name": "WEB",
        "version": "2.20250101.00.00",
        "url": _BASE,
    },
    "music": {
        "name": "WEB_REMIX",
        "version": "1.20250101.00.00",
        "url": _MUSIC,
    },
}


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None

    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]

        runs = value.get("runs")
        if isinstance(runs, list):
            result = "".join(
                item.get("text", "")
                for item in runs
                if isinstance(item, dict)
            )
            return result or None

    return None


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            result = _text(item)
            if result:
                return result
    return _text(value)


def _number(value: str | None) -> int | None:
    if not value:
        return None

    value = value.lower().replace(",", "").strip()
    match = re.search(r"([\d.]+)\s*([kmb])?", value)

    if not match:
        return None

    number = float(match.group(1))
    suffix = match.group(2)

    multiplier = {
        None: 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }[suffix]

    return int(number * multiplier)


def _duration(value: str | None) -> int | None:
    if not value:
        return None

    parts = value.strip().split(":")
    if not all(part.isdigit() for part in parts):
        return None

    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)

    return seconds


def _thumbnail(renderer: dict) -> str | None:
    thumbs = renderer.get("thumbnail", {}).get("thumbnails", [])

    if not thumbs:
        thumbs = renderer.get("thumbnail", {}).get(
            "musicThumbnailRenderer", {}
        ).get("thumbnail", {}).get("thumbnails", [])

    if not isinstance(thumbs, list):
        return None

    for item in reversed(thumbs):
        if isinstance(item, dict) and item.get("url"):
            return item["url"]

    return None


def _runs_text(renderer: dict, key: str) -> str | None:
    return _text(renderer.get(key))


def _badges(renderer: dict) -> list[str]:
    result = []

    for badge in renderer.get("badges", []):
        if not isinstance(badge, dict):
            continue

        metadata = badge.get("metadataBadgeRenderer", {})
        label = metadata.get("label")

        if label:
            result.append(label)

    return result


def _channel_info(renderer: dict) -> dict:
    owner = (
        renderer.get("ownerText")
        or renderer.get("shortBylineText")
        or renderer.get("longBylineText")
        or renderer.get("subtitle")
    )

    channel_name = _text(owner)
    channel_id = None
    channel_url = None
    handle = None

    if isinstance(owner, dict):
        for run in owner.get("runs", []):
            if not isinstance(run, dict):
                continue

            endpoint = run.get("navigationEndpoint", {})
            browse = endpoint.get("browseEndpoint", {})

            channel_id = channel_id or browse.get("browseId")
            channel_url = channel_url or (
                browse.get("canonicalBaseUrl")
            )

            if run.get("text", "").startswith("@"):
                handle = run["text"]

    if channel_url and channel_url.startswith("@"):
        handle = channel_url

    return {
        "channel_name": channel_name,
        "channel_id": channel_id,
        "channel_url": (
            _BASE + channel_url
            if channel_url and channel_url.startswith("/")
            else None
        ),
        "channel_handle": handle,
    }


def _video(renderer: dict, source: str = "youtube") -> dict | None:
    video_id = renderer.get("videoId")

    if not video_id:
        return None

    title = _text(renderer.get("title"))
    duration_text = (
        _text(renderer.get("lengthText"))
        or _text(renderer.get("thumbnailOverlays", [{}])[0]
                 .get("thumbnailOverlayTimeStatusRenderer", {})
                 .get("text"))
        if renderer.get("thumbnailOverlays")
        else _text(renderer.get("lengthText"))
    )

    view_text = (
        _text(renderer.get("viewCountText"))
        or _text(renderer.get("shortViewCountText"))
    )

    published = _text(renderer.get("publishedTimeText"))
    description = _text(renderer.get("descriptionSnippet"))

    result = {
        "type": "video",
        "source": source,
        "id": video_id,
        "title": title,
        "url": f"{_BASE}/watch?v={video_id}",
        "thumbnail": _thumbnail(renderer),
        "duration": duration_text,
        "duration_seconds": _duration(duration_text),
        "views": _number(view_text),
        "views_text": view_text,
        "published": published,
        "description": description,
        "badges": _badges(renderer),
        **_channel_info(renderer),
        "is_live": False,
        "is_short": False,
        "raw_type": "videoRenderer",
    }

    labels = " ".join(result["badges"]).lower()

    if "live" in labels or renderer.get("badges"):
        if "live" in labels:
            result["is_live"] = True

    if result["duration_seconds"] is None and "short" in labels:
        result["is_short"] = True

    return result


def _channel(renderer: dict) -> dict | None:
    channel_id = renderer.get("channelId")

    if not channel_id:
        endpoint = renderer.get("navigationEndpoint", {})
        channel_id = endpoint.get("browseEndpoint", {}).get("browseId")

    if not channel_id:
        return None

    title = _text(renderer.get("title"))
    url = renderer.get("navigationEndpoint", {}).get(
        "browseEndpoint", {}
    ).get("canonicalBaseUrl")

    return {
        "type": "channel",
        "id": channel_id,
        "title": title,
        "url": _BASE + url if url else f"{_BASE}/channel/{channel_id}",
        "thumbnail": _thumbnail(renderer),
        "handle": url if url and url.startswith("@") else None,
        "subscribers": _text(renderer.get("subscriberCountText")),
        "video_count": _text(renderer.get("videoCountText")),
        "description": _text(renderer.get("descriptionSnippet")),
        "badges": _badges(renderer),
        "verified": bool(_badges(renderer)),
        "raw_type": "channelRenderer",
    }


def _playlist(renderer: dict) -> dict | None:
    playlist_id = renderer.get("playlistId")

    if not playlist_id:
        endpoint = renderer.get("navigationEndpoint", {})
        watch = endpoint.get("watchEndpoint", {})
        playlist_id = watch.get("playlistId")

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
        "video_count": _text(
            renderer.get("videoCountText")
            or renderer.get("videoCountShortText")
        ),
        "raw_type": "playlistRenderer",
    }


def _music_item(renderer: dict) -> dict | None:
    flex = renderer.get("flexColumns", [])
    title = None

    if flex:
        title = _text(
            flex[0].get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text")
        )

    if not title:
        title = _text(renderer.get("title"))

    thumbnail = _thumbnail(renderer)

    navigation = renderer.get("navigationEndpoint", {})
    watch = navigation.get("watchEndpoint", {})
    browse = navigation.get("browseEndpoint", {})

    video_id = watch.get("videoId")
    playlist_id = watch.get("playlistId")
    browse_id = browse.get("browseId")

    if video_id:
        result = {
            "type": "song",
            "id": video_id,
            "title": title,
            "url": f"{_BASE}/watch?v={video_id}",
            "thumbnail": thumbnail,
            "raw_type": "musicResponsiveListItemRenderer",
        }
        result.update(_channel_info(renderer))
        return result

    if playlist_id:
        return {
            "type": "playlist",
            "id": playlist_id,
            "title": title,
            "url": f"{_BASE}/playlist?list={playlist_id}",
            "thumbnail": thumbnail,
            "raw_type": "musicResponsiveListItemRenderer",
        }

    if browse_id:
        return {
            "type": "music_entity",
            "id": browse_id,
            "title": title,
            "url": f"{_MUSIC}/browse/{browse_id}",
            "thumbnail": thumbnail,
            "raw_type": "musicResponsiveListItemRenderer",
        }

    return None


def _continuation(data: Any) -> str | None:
    if isinstance(data, dict):
        endpoint = data.get("continuationEndpoint", {})
        token = endpoint.get("continuationCommand", {}).get("token")

        if token:
            return token

        command = data.get("continuationCommand", {})
        if command.get("token"):
            return command["token"]

        for key, value in data.items():
            if key == "continuation":
                if isinstance(value, str):
                    return value

            found = _continuation(value)
            if found:
                return found

    elif isinstance(data, list):
        for item in data:
            found = _continuation(item)
            if found:
                return found

    return None


def _walk(data: Any):
    """Walk dictionaries without converting the full response to strings."""
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)

    elif isinstance(data, list):
        for value in data:
            if isinstance(value, (dict, list)):
                yield from _walk(value)


def _parse(data: dict) -> tuple[list[dict], str | None]:
    results = []
    seen = set()

    for node in _walk(data):
        item = None

        if "videoRenderer" in node:
            item = _video(node["videoRenderer"])

        elif "channelRenderer" in node:
            item = _channel(node["channelRenderer"])

        elif "playlistRenderer" in node:
            item = _playlist(node["playlistRenderer"])

        elif "gridVideoRenderer" in node:
            item = _video(node["gridVideoRenderer"])

        elif "gridChannelRenderer" in node:
            item = _channel(node["gridChannelRenderer"])

        elif "gridPlaylistRenderer" in node:
            item = _playlist(node["gridPlaylistRenderer"])

        elif "musicResponsiveListItemRenderer" in node:
            item = _music_item(node["musicResponsiveListItemRenderer"])

        if not item:
            continue

        key = (item.get("type"), item.get("id"))
        if key in seen:
            continue

        seen.add(key)
        results.append(item)

    return results, _continuation(data)


class _TTLCache:
    def __init__(self, maxsize: int, ttl: float):
        self.maxsize = maxsize
        self.ttl = ttl
        self.data = OrderedDict()

    def get(self, key):
        item = self.data.get(key)
        if not item:
            return None

        expires, value = item
        if expires < time.monotonic():
            self.data.pop(key, None)
            return None

        self.data.move_to_end(key)
        return value

    def set(self, key, value):
        self.data[key] = (time.monotonic() + self.ttl, value)
        self.data.move_to_end(key)

        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)


class YouTubeSearch:
    """
    Async public YouTube / YouTube Music search.

    Requires:
        pip install httpx

    Example:
        async with YouTubeSearch() as yt:
            result = await yt.search("Arijit Singh", filter="songs")
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        concurrency: int = 10,
        cache_size: int = 256,
        cache_ttl: float = 30.0,
        retries: int = 2,
    ):
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(concurrency)
        self.cache = _TTLCache(cache_size, cache_ttl)
        self.inflight: dict[str, asyncio.Task] = {}

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 Chrome/131.0.0.0 "
                    "Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": _BASE,
            },
            follow_redirects=True,
        )

        self._config = None
        self._config_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if not self._closed:
            self._closed = True
            await self.client.aclose()

    async def _get_config(self):
        if self._config:
            return self._config

        async with self._config_lock:
            if self._config:
                return self._config

            try:
                response = await self.client.get(_BASE)
                response.raise_for_status()
                html = response.text

                api_key = re.search(
                    r'"INNERTUBE_API_KEY":"([^"]+)"', html
                )
                version = re.search(
                    r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html
                )
                visitor = re.search(
                    r'"VISITOR_DATA":"([^"]+)"', html
                )

                if not api_key or not version:
                    raise YouTubeSearchError(
                        "Could not read YouTube client configuration."
                    )

                self._config = {
                    "api_key": api_key.group(1),
                    "version": version.group(1),
                    "visitor": visitor.group(1) if visitor else None,
                }

                return self._config

            except httpx.HTTPError as exc:
                raise YouTubeSearchError(
                    f"YouTube configuration request failed: {exc}"
                ) from exc

    async def _request(
        self,
        query: str,
        *,
        filter_name: str,
        continuation: str | None = None,
    ) -> dict:
        music = filter_name in {
            "songs", "albums", "artists",
            "playlists", "community_playlists",
            "featured_playlists",
        }

        base = _MUSIC if music else _BASE
        config = await self._get_config()

        client = {
            "clientName": "WEB_REMIX" if music else "WEB",
            "clientVersion": config["version"],
            "hl": "en",
            "gl": "US",
        }

        if config.get("visitor"):
            client["visitorData"] = config["visitor"]

        body = {"context": {"client": client}}

        if continuation:
            body["continuation"] = continuation
        else:
            body["query"] = query
            params = _FILTERS.get(filter_name)

            if params:
                body["params"] = params

        url = (
            f"{base}/youtubei/v1/search"
            f"?key={quote(config['api_key'])}"
        )

        headers = {}
        if config.get("visitor"):
            headers["X-Goog-Visitor-Id"] = config["visitor"]

        async with self.semaphore:
            for attempt in range(self.retries + 1):
                try:
                    response = await self.client.post(
                        url,
                        json=body,
                        headers=headers,
                    )

                    if response.status_code in (429, 503):
                        if attempt < self.retries:
                            await asyncio.sleep(0.5 * (2 ** attempt))
                            continue

                    response.raise_for_status()
                    data = response.json()

                    if not isinstance(data, dict):
                        raise YouTubeSearchError(
                            "Unexpected YouTube response."
                        )

                    return data

                except httpx.HTTPStatusError as exc:
                    if attempt >= self.retries:
                        raise YouTubeSearchError(
                            f"YouTube returned HTTP {exc.response.status_code}"
                        ) from exc

                    await asyncio.sleep(0.5 * (2 ** attempt))

                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    if attempt >= self.retries:
                        raise YouTubeSearchError(
                            f"YouTube request failed: {exc}"
                        ) from exc

                    await asyncio.sleep(0.5 * (2 ** attempt))

        raise YouTubeSearchError("YouTube request failed.")

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

        filter = filter.lower().strip()

        if filter not in _FILTERS:
            raise ValueError(
                f"Unsupported filter: {filter}. "
                f"Available: {', '.join(_FILTERS)}"
            )

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        cache_key = (
            f"{query.casefold()}:{filter}:{limit}:{continuation or ''}"
        )

        if not continuation:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        task = self.inflight.get(cache_key)

        if task is None:
            async def load():
                data = await self._request(
                    query,
                    filter_name=filter,
                    continuation=continuation,
                )

                items, next_token = _parse(data)

                result = {
                    "query": query,
                    "filter": filter,
                    "results": items[:limit],
                    "continuation": next_token,
                    "has_more": bool(next_token),
                    "count": min(len(items), limit),
                }

                if not continuation:
                    self.cache.set(cache_key, result)

                return result

            task = asyncio.create_task(load())
            self.inflight[cache_key] = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.inflight.get(cache_key) is task:
                self.inflight.pop(cache_key, None)

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
