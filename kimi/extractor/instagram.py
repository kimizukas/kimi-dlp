from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx


_BASE_URL = "https://www.instagram.com"
_ALLOWED_PAGE_HOSTS = {
    "instagram.com", "www.instagram.com", "m.instagram.com",
    "instagr.am", "www.instagr.am",
}
_ALLOWED_MEDIA_SUFFIXES = ("cdninstagram.com", "fbcdn.net", "instagram.com")
_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_PUBLIC_APP_ID = "936619743392459"
_LOGGED_OUT_DOC_ID = "27130156389949648"
_LOGGED_OUT_QUERY_NAME = "PolarisLoggedOutDesktopWWWPostRootContentQuery"

_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
_URL_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?instagram\.com/"
    r"(?:"
    r"(?:p|reel|reels|tv)/[A-Za-z0-9_-]+"
    r"|[^/?#]+/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+"
    r"|share/(?:p/|reel/)?[A-Za-z0-9_-]+"

    r")"
    r"|https?://(?:www\.)?instagr\.am/[A-Za-z0-9_-]+",
    re.I,
)


class InstagramExtractorError(Exception):
    """Base Instagram extractor exception."""


class InstagramInvalidURL(InstagramExtractorError):
    """The supplied URL is not a supported Instagram post URL."""


class InstagramUnavailable(InstagramExtractorError):
    """The post is unavailable or has no supported media."""


class InstagramLoginRequired(InstagramExtractorError):
    """Instagram requires authentication to access the requested media."""


class InstagramRateLimited(InstagramExtractorError):
    """Instagram temporarily rate-limited the request."""


class InstagramRequestError(InstagramExtractorError):
    """Network or response error while contacting Instagram."""


@dataclass(slots=True)
class InstagramOptions:
    timeout: float = 15.0
    retries: int = 2
    max_media: int = 30
    chunk_size: int = 256 * 1024
    max_download_size: int = 2 * 1024 * 1024 * 1024
    request_concurrency: int = 4
    download_concurrency: int = 2
    follow_redirects: bool = True
    graphql_doc_id: str = _LOGGED_OUT_DOC_ID
    app_id: str = _PUBLIC_APP_ID

    def __post_init__(self) -> None:
        self.timeout = max(3.0, min(float(self.timeout), 60.0))
        self.retries = max(0, min(int(self.retries), 5))
        self.max_media = max(1, min(int(self.max_media), 100))
        self.chunk_size = max(16 * 1024, min(int(self.chunk_size), 2 * 1024 * 1024))
        self.max_download_size = max(1, int(self.max_download_size))
        self.request_concurrency = max(1, min(int(self.request_concurrency), 32))
        self.download_concurrency = max(1, min(int(self.download_concurrency), 8))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[str] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag.lower() == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[key.strip().lower()] = html.unescape(content.strip())
        elif tag.lower() == "script":
            self._in_script = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            body = "".join(self._script_parts).strip()
            if body:
                self.scripts.append(body)
            self._script_parts = []
            self._in_script = False


def _shortcode_to_media_id(shortcode: str) -> str:
    number = 0
    for char in shortcode:
        try:
            digit = _SHORTCODE_ALPHABET.index(char)
        except ValueError as exc:
            raise InstagramInvalidURL("Invalid Instagram shortcode.") from exc
        number = number * 64 + digit
    if number <= 0:
        raise InstagramInvalidURL("Invalid Instagram shortcode.")
    return str(number)


def _normalise_post_url(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise InstagramInvalidURL("Instagram URL is required.")
    try:
        parsed = urlparse(value.strip())
    except ValueError as exc:
        raise InstagramInvalidURL("Invalid Instagram URL.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise InstagramInvalidURL("Only HTTP(S) Instagram URLs are supported.")
    if parsed.username or parsed.password:
        raise InstagramInvalidURL("Credentials are not allowed in Instagram URLs.")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_PAGE_HOSTS:
        raise InstagramInvalidURL("URL must belong to Instagram.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise InstagramInvalidURL("Unsupported Instagram URL.")

    # Accept /p/CODE, /reel/CODE, /username/p/CODE and /username/reel/CODE.
    if parts[0].lower() in {"p", "reel", "reels", "tv"}:
        kind, shortcode = parts[0].lower(), parts[1]
    elif len(parts) >= 3 and parts[1].lower() in {"p", "reel", "reels", "tv"}:
        kind, shortcode = parts[1].lower(), parts[2]
    else:
        raise InstagramInvalidURL(
            "Supported URLs: /p/, /reel/, /reels/, /tv/ and username-based post URLs."
        )

    if not _SHORTCODE_RE.fullmatch(shortcode):
        raise InstagramInvalidURL("Invalid Instagram shortcode.")
    if kind == "reels":
        kind = "reel"

    canonical = urlunparse((
        "https", "www.instagram.com", f"/{kind}/{shortcode}/", "", "", ""
    ))
    return canonical, kind, shortcode


def _balanced_json_after(source: str, marker: str) -> str | None:
    start = source.find(marker)
    if start < 0:
        return None
    start += len(marker)
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] not in "{[":
        return None

    opening = source[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = escaped = False

    for index in range(start, len(source)):
        char = source[index]
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
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return None


def _json_documents(scripts: list[str]) -> list[Any]:
    documents: list[Any] = []
    markers = (
        "window._sharedData =",
        "window.__additionalDataLoaded(",
        "window.__initialData =",
    )
    for script in scripts:
        candidate = script.strip()
        try:
            documents.append(json.loads(candidate))
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        for marker in markers:
            blob = _balanced_json_after(script, marker)
            if blob:
                try:
                    documents.append(json.loads(blob))
                except json.JSONDecodeError:
                    pass
        if "application/ld+json" in script[:300].lower():
            try:
                documents.append(json.loads(candidate))
            except json.JSONDecodeError:
                pass
    return documents


def _walk_json(root: Any, *, max_nodes: int = 100_000, max_depth: int = 40):
    stack = [(root, 0)]
    visited = 0
    while stack and visited < max_nodes:
        node, depth = stack.pop()
        visited += 1
        if isinstance(node, dict):
            yield node
            if depth < max_depth:
                stack.extend(
                    (value, depth + 1)
                    for value in node.values()
                    if isinstance(value, (dict, list))
                )
        elif isinstance(node, list) and depth < max_depth:
            stack.extend(
                (value, depth + 1)
                for value in node
                if isinstance(value, (dict, list))
            )


def _media_signature(node: dict[str, Any]) -> bool:
    return any(key in node for key in (
        "video_versions", "image_versions2", "image_versions",
        "display_url", "display_resources", "carousel_media",
        "edge_sidecar_to_children",
    ))


def _find_media_node(documents: list[Any], shortcode: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for document in documents:
        for node in _walk_json(document):
            if _media_signature(node):
                candidates.append(node)

    exact = [
        node for node in candidates
        if (node.get("code") or node.get("shortcode")) == shortcode
    ]
    if exact:
        return max(
            exact,
            key=lambda node: (
                bool(node.get("video_versions")),
                bool(node.get("carousel_media") or node.get("edge_sidecar_to_children")),
            ),
        )

    unidentified = [
        node for node in candidates
        if not (node.get("code") or node.get("shortcode"))
    ]
    if len(candidates) == 1 and len(unidentified) == 1:
        return unidentified[0]
    return None


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        simple = value.get("text") or value.get("simple_text") or value.get("simpleText")
        if isinstance(simple, str):
            return simple.strip() or None
        edges = value.get("edges")
        if isinstance(edges, list):
            parts = []
            for edge in edges:
                node = edge.get("node", {}) if isinstance(edge, dict) else {}
                text = node.get("text") if isinstance(node, dict) else None
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts).strip() or None
    return None


def _caption(node: dict[str, Any]) -> str | None:
    text = _text_value(node.get("caption"))
    if text:
        return text
    edge = node.get("edge_media_to_caption")
    if isinstance(edge, dict) and isinstance(edge.get("edges"), list):
        for item in edge["edges"]:
            child = item.get("node", {}) if isinstance(item, dict) else {}
            text = _text_value(child.get("text") if isinstance(child, dict) else None)
            if text:
                return text
    return None


def _user_name(node: dict[str, Any]) -> str | None:
    user = node.get("user") or node.get("owner") or node.get("coauthor_producers")
    if isinstance(user, dict):
        return user.get("username") or user.get("name")
    if isinstance(user, list) and user and isinstance(user[0], dict):
        return user[0].get("username")
    return None


def _number(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _best_image(node: dict[str, Any]) -> dict[str, Any] | None:
    versions: list[dict[str, Any]] = []
    image_data = node.get("image_versions2") or node.get("image_versions")
    if isinstance(image_data, dict):
        candidates = image_data.get("candidates") or image_data.get("additional_candidates")
        if isinstance(candidates, list):
            versions.extend(item for item in candidates if isinstance(item, dict))
    resources = node.get("display_resources")
    if isinstance(resources, list):
        versions.extend(item for item in resources if isinstance(item, dict))
    display_url = node.get("display_url")
    if isinstance(display_url, str):
        dimensions = node.get("dimensions")
        dimensions = dimensions if isinstance(dimensions, dict) else {}
        versions.append({
            "url": display_url,
            "width": node.get("original_width") or dimensions.get("width"),
            "height": node.get("original_height") or dimensions.get("height"),
        })
    valid = [item for item in versions if isinstance(item.get("url"), str)]
    if not valid:
        return None
    return max(
        valid,
        key=lambda item: (
            (_number(item.get("width")) or 0) * (_number(item.get("height")) or 0),
            _number(item.get("width")) or 0,
        ),
    )


def _best_video(node: dict[str, Any]) -> dict[str, Any] | None:
    versions = node.get("video_versions")
    if isinstance(versions, list):
        valid = [
            item for item in versions
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
        if valid:
            return max(
                valid,
                key=lambda item: (
                    (_number(item.get("width")) or 0) * (_number(item.get("height")) or 0),
                    _number(item.get("bitrate")) or 0,
                ),
            )
    url = node.get("video_url")
    if isinstance(url, str) and url:
        return {
            "url": url,
            "width": node.get("original_width"),
            "height": node.get("original_height"),
        }
    return None


def _is_video(node: dict[str, Any]) -> bool:
    media_type = node.get("media_type")
    return bool(
        node.get("is_video")
        or node.get("video_versions")
        or node.get("video_url")
        or media_type == 2
        or str(media_type).lower() == "video"
        or node.get("product_type") in {"clips", "igtv"}
    )


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    children = node.get("carousel_media")
    if isinstance(children, list):
        return [item for item in children if isinstance(item, dict)]
    sidecar = node.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict) and isinstance(sidecar.get("edges"), list):
        return [
            edge["node"] for edge in sidecar["edges"]
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
    return []


def _extension(url: str, media_type: str) -> str:
    suffix = Path(urlparse(url).path.lower()).suffix.lstrip(".")
    if suffix in {"mp4", "mov", "m4v", "jpg", "jpeg", "png", "webp", "heic", "heif"}:
        return suffix
    return "mp4" if media_type == "video" else "jpg"


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value[:100] or "instagram_media"


def _is_allowed_media_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme.lower() == "https"
            and not parsed.username
            and not parsed.password
            and any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_MEDIA_SUFFIXES)
        )
    except ValueError:
        return False


class InstagramExtractor:
    """Async extractor for accessible Instagram posts, Reels and carousels.

    Pass an httpx.AsyncClient with your own cookies to use an existing session.
    This class does not log in or bypass private-account restrictions.
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
        options: InstagramOptions | None = None,
    ) -> None:
        self.options = options or InstagramOptions(timeout=timeout, retries=retries)
        self._external_client = client is not None
        self._closed = False
        if client is not None:
            self.client = client
        else:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.options.timeout, connect=min(self.options.timeout, 10.0)),
                follow_redirects=self.options.follow_redirects,
                proxy=proxy,
                limits=httpx.Limits(
                    max_connections=max(4, self.options.request_concurrency + self.options.download_concurrency),
                    max_keepalive_connections=max(2, self.options.request_concurrency),
                    keepalive_expiry=30.0,
                ),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        self._request_semaphore = asyncio.Semaphore(self.options.request_concurrency)
        self._download_semaphore = asyncio.Semaphore(self.options.download_concurrency)
        self._bootstrap_lock = asyncio.Lock()
        self._lsd_token: str | None = None

    async def __aenter__(self) -> InstagramExtractor:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._external_client:
            await self.client.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InstagramExtractorError("Extractor is closed.")

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(15.0, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        return min(0.5 * (2 ** attempt), 4.0)

    def _cookie(self, name: str) -> str | None:
        try:
            value = self.client.cookies.get(name, domain="www.instagram.com")
            if value:
                return value
        except (KeyError, httpx.CookieConflict):
            pass
        try:
            value = self.client.cookies.get(name)
            return value or None
        except (KeyError, httpx.CookieConflict):
            return None

    async def _request_page(self, url: str) -> httpx.Response:
        self._ensure_open()
        async with self._request_semaphore:
            for attempt in range(self.options.retries + 1):
                response = None
                try:
                    response = await self.client.get(url, headers={"Referer": _BASE_URL + "/"})
                    status = response.status_code
                    if status in _TRANSIENT_STATUSES:
                        if attempt < self.options.retries:
                            delay = self._retry_delay(response, attempt)
                            await response.aclose()
                            response = None
                            await asyncio.sleep(delay)
                            continue
                        if status == 429:
                            raise InstagramRateLimited("Instagram rate limit reached.")
                        raise InstagramRequestError(f"Instagram returned HTTP {status}.")
                    if status in {401, 403}:
                        raise InstagramLoginRequired("Instagram requires authentication for this content.")
                    if status == 404:
                        raise InstagramUnavailable("Instagram content was not found.")
                    if status >= 400:
                        raise InstagramRequestError(f"Instagram returned HTTP {status}.")
                    final_host = (response.url.host or "").lower()
                    if final_host not in _ALLOWED_PAGE_HOSTS:
                        raise InstagramRequestError("Instagram redirected to an unsupported host.")
                    result = response
                    response = None  # Transfer ownership to the caller.
                    return result
                except InstagramExtractorError:
                    if response is not None:
                        await response.aclose()
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if response is not None:
                        await response.aclose()
                    if attempt >= self.options.retries:
                        raise InstagramRequestError(
                            f"Instagram request failed: {type(exc).__name__}: {exc}"
                        ) from exc
                    await asyncio.sleep(self._retry_delay(None, attempt))
                finally:
                    if response is not None and not response.is_closed:
                        await response.aclose()
        raise InstagramRequestError("Unable to retrieve Instagram page.")

    async def _resolve_url(self, value: str) -> str:
        """Resolve share/short links, then validate the final Instagram URL."""
        try:
            parsed = urlparse(value.strip())
        except (AttributeError, ValueError) as exc:
            raise InstagramInvalidURL("Invalid Instagram URL.") from exc
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
            raise InstagramInvalidURL("Only valid HTTP(S) Instagram URLs are supported.")
        if (parsed.hostname or "").lower() not in _ALLOWED_PAGE_HOSTS:
            raise InstagramInvalidURL("URL must belong to Instagram.")

        path = parsed.path.lower()
        is_short = (parsed.hostname or "").lower().endswith("instagr.am")
        is_share = path.startswith("/share/")
        if not (is_short or is_share):
            return value

        response = await self._request_page(value)
        try:
            final_url = str(response.url)
            final_host = (response.url.host or "").lower()
            if final_host not in _ALLOWED_PAGE_HOSTS:
                raise InstagramInvalidURL("Share link redirected outside Instagram.")
            # Some deployments serve a share landing page without an HTTP redirect.
            if final_url == value and (is_short or is_share):
                canonical = response.headers.get("Link", "")
                match = re.search(r'<([^>]+)>;\s*rel=["\']?canonical', canonical, re.I)
                if match:
                    final_url = match.group(1)
                else:
                    parser = _PageParser()
                    parser.feed(response.text)
                    final_url = parser.meta.get("og:url") or final_url
            return final_url
        finally:
            await response.aclose()

    async def _get_page(self, url: str) -> str:
        response = await self._request_page(url)
        try:
            return response.text
        finally:
            await response.aclose()

    async def _bootstrap_public_session(self) -> None:
        if self._lsd_token:
            return
        async with self._bootstrap_lock:
            if self._lsd_token:
                return
            page = await self._get_page(_BASE_URL + "/")
            eqmc = re.search(
                r'<script\b[^>]*\bid="__eqmc"[^>]*>(.*?)</script>', page, re.I | re.S
            )
            if eqmc:
                try:
                    payload = json.loads(eqmc.group(1))
                    token = payload.get("l") if isinstance(payload, dict) else None
                    if isinstance(token, str) and token:
                        self._lsd_token = token
                except json.JSONDecodeError:
                    pass
            if not self._lsd_token:
                match = re.search(r'\["LSD",\[\],\{"token":"([^"\\]+)"', page)
                if match:
                    self._lsd_token = match.group(1)

    async def _extract_logged_out_graphql(
        self, shortcode: str, referer: str
    ) -> dict[str, Any] | None:
        await self._bootstrap_public_session()
        if not self._lsd_token:
            return None
        media_id = _shortcode_to_media_id(shortcode)
        api_headers = {
            "X-IG-App-ID": self.options.app_id,
            "X-ASBD-ID": "359341",
            "X-IG-WWW-Claim": "0",
            "Origin": _BASE_URL,
            "Accept": "*/*",
            "Referer": referer,
            "X-FB-LSD": self._lsd_token,
            "X-Requested-With": "XMLHttpRequest",
        }
        async with self._request_semaphore:
            try:
                access_response = await self.client.get(
                    f"{_BASE_URL}/api/v1/web/get_ruling_for_content/",
                    params={"content_type": "MEDIA", "target_id": media_id},
                    headers=api_headers,
                )
                try:
                    if access_response.status_code == 429:
                        raise InstagramRateLimited("Instagram rate limit reached.")
                    access_data = access_response.json() if access_response.status_code < 400 else {}
                finally:
                    await access_response.aclose()

                csrf = self._cookie("csrftoken")
                if not isinstance(access_data, dict) or access_data.get("status") != "ok":
                    csrf = None

                form = {
                    "lsd": self._lsd_token,
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": _LOGGED_OUT_QUERY_NAME,
                    "server_timestamps": "true",
                    "variables": json.dumps({"media_id": media_id}, separators=(",", ":")),
                    "doc_id": self.options.graphql_doc_id,
                }
                headers = {
                    **api_headers,
                    "X-FB-Friendly-Name": _LOGGED_OUT_QUERY_NAME,
                }
                if csrf:
                    headers["X-CSRFToken"] = csrf

                response = await self.client.post(
                    f"{_BASE_URL}/api/graphql", data=form, headers=headers
                )
                try:
                    if response.status_code == 429:
                        raise InstagramRateLimited("Instagram GraphQL rate limit reached.")
                    if response.status_code in {401, 403}:
                        return None
                    if response.status_code >= 400:
                        return None
                    payload = response.json()
                finally:
                    await response.aclose()
            except InstagramExtractorError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError, ValueError):
                return None

        data = payload.get("data") if isinstance(payload, dict) else None
        media = data.get("xig_polaris_media") if isinstance(data, dict) else None
        if not isinstance(media, dict):
            return None
        product = media.get("if_not_gated_logged_out")
        if isinstance(product, dict):
            return product
        if isinstance(product, list):
            return next((item for item in product if isinstance(item, dict)), None)
        return None

    async def _extract_authenticated_media_info(
        self, shortcode: str, referer: str
    ) -> dict[str, Any] | None:
        if not self._cookie("sessionid"):
            return None
        media_id = _shortcode_to_media_id(shortcode)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "*/*",
            "X-IG-App-ID": self.options.app_id,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
        }
        csrf = self._cookie("csrftoken")
        if csrf:
            headers["X-CSRFToken"] = csrf
        endpoint = f"{_BASE_URL}/api/v1/media/{media_id}/info/"
        async with self._request_semaphore:
            for attempt in range(self.options.retries + 1):
                response = None
                try:
                    response = await self.client.get(
                        endpoint,
                        params={"media_id": media_id, "igtv_feed_preview": "false"},
                        headers=headers,
                    )
                    status = response.status_code
                    if status in _TRANSIENT_STATUSES:
                        if attempt < self.options.retries:
                            delay = self._retry_delay(response, attempt)
                            await response.aclose()
                            response = None
                            await asyncio.sleep(delay)
                            continue
                        if status == 429:
                            raise InstagramRateLimited("Instagram media lookup rate limited.")
                        return None
                    if status in {401, 403, 404} or status >= 400:
                        return None
                    try:
                        payload = response.json()
                    except (json.JSONDecodeError, ValueError):
                        return None
                    if not isinstance(payload, dict):
                        return None
                    items = payload.get("items")
                    if isinstance(items, list):
                        return next((item for item in items if isinstance(item, dict)), None)
                    item = payload.get("media")
                    return item if isinstance(item, dict) else None
                except InstagramExtractorError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt >= self.options.retries:
                        return None
                    await asyncio.sleep(self._retry_delay(None, attempt))
                finally:
                    if response is not None:
                        await response.aclose()
        return None

    @staticmethod
    def _normalise_authenticated_node(node: dict[str, Any]) -> dict[str, Any]:
        result = dict(node)
        if "pk" not in result and result.get("id") is not None:
            result["pk"] = result["id"]
        if "code" not in result and result.get("shortcode"):
            result["code"] = result["shortcode"]
        # Older and newer API payloads use different image containers.
        if "image_versions2" not in result and isinstance(result.get("image_versions"), dict):
            result["image_versions2"] = result["image_versions"]
        return result

    async def extract(self, url: str) -> dict[str, Any]:
        self._ensure_open()
        resolved = await self._resolve_url(url)
        canonical, kind, shortcode = _normalise_post_url(resolved)

        node = None
        try:
            node = await self._extract_logged_out_graphql(shortcode, canonical)
        except InstagramLoginRequired:
            node = None
        if node is None:
            node = await self._extract_authenticated_media_info(shortcode, canonical)

        page = None
        parser = _PageParser()
        if node is None:
            page = await self._get_page(canonical)
            parser.feed(page)
            node = _find_media_node(_json_documents(parser.scripts), shortcode)

        if node is None:
            og_video = parser.meta.get("og:video:secure_url") or parser.meta.get("og:video")
            og_image = parser.meta.get("og:image:secure_url") or parser.meta.get("og:image")
            if og_video or og_image:
                media_url = og_video or og_image
                media_type = "video" if og_video else "photo"
                if not _is_allowed_media_url(media_url):
                    raise InstagramUnavailable("Instagram exposed an unsupported media URL.")
                thumbnail = og_image if isinstance(og_image, str) and _is_allowed_media_url(og_image) else None
                return {
                    "id": shortcode, "shortcode": shortcode, "platform": "instagram",
                    "type": media_type, "title": parser.meta.get("og:title"),
                    "caption": parser.meta.get("og:description"), "username": None,
                    "thumbnail": thumbnail, "duration": None, "source_url": canonical,
                    "media_count": 1,
                    "media": [{
                        "url": media_url, "type": media_type,
                        "format": _extension(media_url, media_type),
                        "width": None, "height": None, "bitrate": None,
                    }],
                }
            lowered = (page or "").lower()
            if any(marker in lowered for marker in (
                "login_required", "login to continue", "log in to instagram",
                "challenge_required", "consent_required",
            )):
                raise InstagramLoginRequired("Instagram did not expose this post without login.")
            raise InstagramUnavailable("Instagram did not expose media for this URL.")

        node = self._normalise_authenticated_node(node)
        children = _children(node)
        source_items = children if children else [node]
        media: list[dict[str, Any]] = []

        for child in source_items[:self.options.max_media]:
            is_video = _is_video(child)
            selected = _best_video(child) if is_video else _best_image(child)
            if selected is None and is_video:
                selected = _best_image(child)
                is_video = False
            if selected is None and not is_video:
                selected = _best_video(child)
                is_video = selected is not None
            if selected is None:
                continue
            media_url = selected.get("url")
            if not isinstance(media_url, str) or not _is_allowed_media_url(media_url):
                continue
            media_type = "video" if is_video else "photo"
            media.append({
                "url": media_url, "type": media_type,
                "format": _extension(media_url, media_type),
                "width": _number(selected.get("width")),
                "height": _number(selected.get("height")),
                "bitrate": _number(selected.get("bitrate")),
            })

        if not media:
            raise InstagramUnavailable(
                "Post found, but Instagram did not expose downloadable media URLs."
            )

        owner = node.get("owner") if isinstance(node.get("owner"), dict) else {}
        user = node.get("user") if isinstance(node.get("user"), dict) else {}
        dimensions = node.get("dimensions") if isinstance(node.get("dimensions"), dict) else {}
        types = {item["type"] for item in media}
        post_type = "carousel" if children else ("video" if "video" in types else "photo")

        thumbnail = media[0]["url"]
        if post_type == "video":
            image = _best_image(node)
            if image and _is_allowed_media_url(image.get("url", "")):
                thumbnail = image["url"]

        return {
            "id": str(node.get("pk") or node.get("id") or shortcode),
            "shortcode": shortcode, "platform": "instagram", "type": post_type,
            "title": None, "caption": _caption(node),
            "username": user.get("username") or owner.get("username") or _user_name(node),
            "duration": node.get("video_duration") or node.get("video_duration_seconds"),
            "width": _number(dimensions.get("width") or node.get("original_width")),
            "height": _number(dimensions.get("height") or node.get("original_height")),
            "thumbnail": thumbnail, "source_url": canonical,
            "media_count": len(media), "media": media,
        }

    async def download(
        self,
        result: dict[str, Any],
        *,
        output_dir: str | Path = "downloads",
        filename_prefix: str | None = None,
    ) -> list[Path]:
        self._ensure_open()
        if not isinstance(result, dict) or result.get("platform") != "instagram":
            raise InstagramExtractorError("Pass the dictionary returned by extract().")
        items = result.get("media")
        if not isinstance(items, list) or not items:
            raise InstagramUnavailable("The extraction result contains no media.")

        directory = Path(output_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        prefix = _safe_filename(filename_prefix or result.get("shortcode") or result.get("id") or "instagram")
        jobs: list[tuple[str, Path]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            media_url = item.get("url")
            media_type = item.get("type")
            if not isinstance(media_url, str) or not _is_allowed_media_url(media_url):
                raise InstagramExtractorError("Invalid or unsupported media URL in result.")
            extension = _extension(media_url, media_type if media_type in {"video", "photo"} else "photo")
            name = f"{prefix}_{index:02d}.{extension}" if len(items) > 1 else f"{prefix}.{extension}"
            jobs.append((media_url, directory / name))

        if not jobs:
            raise InstagramUnavailable("No valid media download jobs were found.")
        destinations = [path for _, path in jobs]
        if len(set(destinations)) != len(destinations):
            raise InstagramExtractorError("Duplicate output filenames detected.")

        tasks = [
            asyncio.create_task(self._download_one(media_url, destination))
            for media_url, destination in jobs
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for destination in destinations:
                for path in (destination, destination.with_name(destination.name + ".part")):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise
        return destinations

    async def _download_one(self, url: str, destination: Path) -> None:
        self._ensure_open()
        async with self._download_semaphore:
            temporary = destination.with_name(destination.name + ".part")
            for attempt in range(self.options.retries + 1):
                written = 0
                try:
                    async with self.client.stream(
                        "GET", url,
                        headers={
                            "Referer": _BASE_URL + "/",
                            "Accept": "*/*",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        status = response.status_code
                        if status in _TRANSIENT_STATUSES:
                            if attempt < self.options.retries:
                                delay = self._retry_delay(response, attempt)
                                await asyncio.sleep(delay)
                                continue
                            if status == 429:
                                raise InstagramRateLimited("Instagram CDN rate limit reached.")
                            raise InstagramRequestError(f"Media download failed with HTTP {status}.")
                        if status in {401, 403, 404}:
                            raise InstagramUnavailable(f"Media URL is unavailable (HTTP {status}).")
                        if status >= 400:
                            raise InstagramRequestError(f"Media download failed with HTTP {status}.")

                        final_host = (response.url.host or "").lower()
                        if not any(final_host == suffix or final_host.endswith("." + suffix) for suffix in _ALLOWED_MEDIA_SUFFIXES):
                            raise InstagramRequestError("Media redirected to an unsupported host.")
                        length_header = response.headers.get("Content-Length")
                        length = _number(length_header) if length_header is not None else None
                        if length is not None and length > self.options.max_download_size:
                            raise InstagramRequestError("Media exceeds configured maximum download size.")

                        with temporary.open("wb") as file:
                            async for chunk in response.aiter_bytes(self.options.chunk_size):
                                if not chunk:
                                    continue
                                written += len(chunk)
                                if written > self.options.max_download_size:
                                    raise InstagramRequestError("Media exceeded maximum download size.")
                                file.write(chunk)

                        if written == 0:
                            raise InstagramRequestError("Instagram returned an empty media file.")
                        if length is not None and written != length:
                            raise InstagramRequestError(
                                f"Incomplete media response: expected {length}, received {written} bytes."
                            )
                    temporary.replace(destination)
                    return
                except InstagramExtractorError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= self.options.retries:
                        raise InstagramRequestError(
                            f"Media download failed: {type(exc).__name__}: {exc}"
                        ) from exc
                    await asyncio.sleep(self._retry_delay(None, attempt))
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise InstagramRequestError("Media download failed after retries.")


def extract_instagram_urls(text: str) -> list[str]:
    """Find post/Reel URLs. Share links remain raw until network resolution."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(".,!?:;)]}")
        try:
            parsed = urlparse(value)
            path = parsed.path.lower()
            if path.startswith("/share/") or (parsed.hostname or "").lower().endswith("instagr.am"):
                key = value.rstrip("/")
                output = value
            else:
                output, _, _ = _normalise_post_url(value)
                key = output
        except (InstagramInvalidURL, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            found.append(output)
    return found


__all__ = [
    "InstagramExtractor", "InstagramExtractorError", "InstagramInvalidURL",
    "InstagramUnavailable", "InstagramLoginRequired", "InstagramRateLimited",
    "InstagramRequestError", "InstagramOptions", "extract_instagram_urls",
]
