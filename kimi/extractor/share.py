"""Safe share-link resolution for supported Instagram and YouTube URLs.

Only first-party domains are requested or followed. Instagram share tokens are
opaque: they are never treated as post shortcodes. A share URL is resolved only
when Instagram redirects it or the returned page identifies one unambiguous
canonical post/Reel URL.
"""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from ..network import RetryPolicy


_ALLOWED_HOSTS = frozenset({
    "instagram.com", "www.instagram.com", "m.instagram.com", "applink.instagram.com",
    "instagr.am", "www.instagr.am",
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
})
_IG_HOSTS = frozenset({
    "instagram.com", "www.instagram.com", "m.instagram.com", "applink.instagram.com",
    "instagr.am", "www.instagr.am",
})
_YT_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
})
_ABS_POST_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com(/(?:p|reel|reels|tv)/[A-Za-z0-9_-]{5,32})(?:/|[?&#\"']|$)",
    re.I,
)
_REL_POST_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])(/(?:p|reel|reels|tv)/[A-Za-z0-9_-]{5,32})(?:/|[?&#\"']|$)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


class ShareResolverError(ValueError):
    """Invalid or unsupported share URL, redirect or response."""


class _MetaParser(HTMLParser):
    """Read canonical/share targets without executing page JavaScript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical: str | None = None
        self.refresh_url: str | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(k).lower(): v for k, v in attrs}
        if tag == "link":
            rel = str(values.get("rel") or "").lower().split()
            href = values.get("href")
            if "canonical" in rel and isinstance(href, str) and href.strip():
                self.canonical = href.strip()
            return
        if tag != "meta":
            return
        key = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content")
        if key in {"og:url", "twitter:url"} and isinstance(content, str):
            if self.canonical is None:
                self.canonical = content.strip()
        elif str(values.get("http-equiv") or "").lower() == "refresh" and isinstance(content, str):
            match = re.search(r"(?:^|;)\s*url\s*=\s*['\"]?([^'\"]+)['\"]?", content, re.I)
            if match:
                self.refresh_url = match.group(1).strip()


def _host(value: str) -> str:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise ShareResolverError("Only credential-free HTTP(S) links are supported.")
        port = parsed.port
        if port not in (None, 443 if scheme == "https" else 80):
            raise ShareResolverError("Non-standard ports are not supported in share URLs.")
        if host not in _ALLOWED_HOSTS:
            raise ShareResolverError("Share URL belongs to an unsupported host.")
        return host
    except ValueError as exc:
        raise ShareResolverError("Invalid share URL.") from exc


def normalize_supported_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShareResolverError("Share URL is required.")
    value = value.strip().rstrip(".,!?:;)]}")
    host = _host(value)
    parsed = urlparse(value)
    if host in {"applink.instagram.com", "m.instagram.com", "instagram.com", "www.instagram.com"}:
        return urlunparse(parsed._replace(scheme="https", netloc="www.instagram.com"))
    if host in {"instagr.am", "www.instagr.am"}:
        return urlunparse(parsed._replace(scheme="https", netloc="www.instagr.am"))
    if host in _YT_HOSTS:
        return urlunparse(parsed._replace(scheme="https"))
    raise ShareResolverError("Unsupported share URL.")


def _canonical_post_url(value: str, base_url: str) -> str | None:
    """Validate a candidate and return a clean canonical Instagram post URL."""
    candidate = urljoin(base_url, html.unescape(value.strip()))
    try:
        if _host(candidate) not in _IG_HOSTS:
            return None
    except ShareResolverError:
        return None
    parsed = urlparse(candidate)
    match = re.fullmatch(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,32})/?", parsed.path, re.I)
    if not match:
        # Username-prefixed post links are valid, except reserved routes such
        # as /share/p/TOKEN: that token is not a post shortcode.
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 3 and parts[0].lower() not in {"share", "stories", "accounts", "explore", "reels"}:
            match = re.fullmatch(r"/[^/]+/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,32})/?", parsed.path, re.I)
    if not match:
        return None
    kind = match.group(1).lower()
    if kind == "reels":
        kind = "reel"
    return f"https://www.instagram.com/{kind}/{match.group(2)}/"


def _page_post_candidates(page: str, base_url: str) -> list[str]:
    """Find only explicit post URL candidates; never decode share tokens as IDs."""
    parser = _MetaParser()
    parser.feed(page)
    for target in (parser.canonical, parser.refresh_url):
        if target:
            candidate = _canonical_post_url(target, base_url)
            if candidate:
                return [candidate]

    # Instagram sometimes serializes URLs with escaped slashes in inline data.
    decoded = html.unescape(page[:2_000_000])
    decoded = (decoded.replace(r"\\/", "/")
                      .replace(r"\/", "/")
                      .replace(r"\u002F", "/")
                      .replace(r"\u003A", ":")
                      .replace(r"\u0026", "&"))
    found: list[str] = []
    seen: set[str] = set()
    matches = list(_ABS_POST_RE.finditer(decoded))
    matches.extend(_REL_POST_RE.finditer(decoded))
    for match in matches:
        candidate = _canonical_post_url(match.group(1), base_url)
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    # A share page can contain recommendations. Never guess if it exposes
    # multiple possible post URLs without an explicit canonical target.
    return found if len(found) == 1 else []


def extract_supported_urls(text: str) -> list[str]:
    """Extract supported Instagram/YouTube links from arbitrary message text."""
    if not isinstance(text, str) or not text:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(".,!?:;)]}")
        try:
            normalized = normalize_supported_url(value)
        except ShareResolverError:
            continue
        parsed = urlparse(normalized)
        key = (parsed.hostname or "") + parsed.path.rstrip("/")
        if key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


@dataclass(slots=True)
class ShareResolver:
    client: httpx.AsyncClient | None = None
    timeout: float = 12.0
    retries: int = 2
    max_redirects: int = 8

    async def _get(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]):
        retries = max(0, min(int(self.retries), 5))
        for attempt in range(retries + 1):
            try:
                response = await client.get(url, headers=headers, follow_redirects=False)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= retries:
                    raise ShareResolverError(f"Share resolution failed: {type(exc).__name__}.") from exc
                await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
                continue
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                await response.aclose()
                delay = RetryPolicy(base_delay=0.25, max_delay=8.0, jitter=0.0).delay(retry_after, attempt)
                await asyncio.sleep(delay)
                continue
            return response
        raise ShareResolverError("Share server did not return a response.")

    async def _resolve_head_chain(self, client: httpx.AsyncClient, start_url: str) -> str | None:
        """Resolve share redirects with HEAD before downloading the HTML page.

        Instagram's newer share URLs may expose their canonical target only
        through the redirect response to HEAD. A 2xx HEAD response is not
        enough to identify a post, so the caller falls back to GET parsing.
        """
        current = start_url
        limit = max(1, min(int(self.max_redirects), 12))
        for _ in range(limit + 1):
            _host(current)
            response = await self._head(client, current, {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.instagram.com/",
                "User-Agent": "Mozilla/5.0",
            })
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    target = urljoin(current, location)
                    _host(target)
                    candidate = _canonical_post_url(target, target)
                    if candidate:
                        return candidate
                    current = target
                    continue
                return None
            finally:
                await response.aclose()
        raise ShareResolverError("Share link exceeded the redirect limit.")

    async def _head(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]):
        retries = max(0, min(int(self.retries), 5))
        for attempt in range(retries + 1):
            try:
                response = await client.head(url, headers=headers, follow_redirects=False)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= retries:
                    raise ShareResolverError(f"Share resolution failed: {type(exc).__name__}.") from exc
                await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
                continue
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                await response.aclose()
                delay = RetryPolicy(base_delay=0.25, max_delay=8.0, jitter=0.0).delay(retry_after, attempt)
                await asyncio.sleep(delay)
                continue
            return response
        raise ShareResolverError("Share server did not return a HEAD response.")

    async def _resolve_chain(self, client: httpx.AsyncClient, start_url: str) -> tuple[str | None, str | None]:
        current = start_url
        for _ in range(max(1, min(int(self.max_redirects), 12)) + 1):
            _host(current)
            response = await self._get(client, current, {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.instagram.com/",
            })
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ShareResolverError("Share redirect is missing its Location header.")
                    target = urljoin(current, location)
                    _host(target)
                    current = target
                    continue
                if response.status_code >= 400:
                    raise ShareResolverError(f"Share server returned HTTP {response.status_code}.")
                final_url = str(response.url)
                final_host = _host(final_url)
                if final_host not in _ALLOWED_HOSTS:
                    raise ShareResolverError("Share link ended on an unsupported host.")
                if final_host in _IG_HOSTS:
                    page = response.text[:2_000_000]
                    final_path = urlparse(final_url).path.lower()
                    candidate = _canonical_post_url(final_url, final_url)
                    if candidate:
                        return candidate, page
                    candidates = _page_post_candidates(page, final_url)
                    if len(candidates) == 1:
                        return candidates[0], page
                    return None, page
                return normalize_supported_url(final_url), None
            finally:
                await response.aclose()
        raise ShareResolverError("Share link exceeded the redirect limit.")

    async def resolve(self, value: str) -> str:
        url = normalize_supported_url(value)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        is_ig_share = host in _IG_HOSTS and path.startswith("/share/")
        needs_request = (
            host in {"youtu.be", "instagr.am", "www.instagr.am", "applink.instagram.com"}
            or is_ig_share
            or path.startswith("/attribution_link")
        )
        if not needs_request:
            return url

        own_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(max(3.0, min(float(self.timeout), 60.0))),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        )
        try:
            # The same share token is tried through the canonical desktop and
            # mobile web routes. A trailing slash matters for some IG routes.
            attempts = [url]
            if is_ig_share:
                clean_path = parsed.path.rstrip("/")
                token_path = clean_path + "/"
                attempts = list(dict.fromkeys((
                    url,
                    urlunparse(parsed._replace(path=token_path)),
                    urlunparse(parsed._replace(netloc="m.instagram.com", path=token_path)),
                )))

            last_page: str | None = None
            if is_ig_share:
                # Instagram's share endpoint can redirect on HEAD even when
                # its HTML GET response stays on the opaque /share/ URL.
                for candidate_url in attempts:
                    resolved = await self._resolve_head_chain(client, candidate_url)
                    if resolved:
                        return resolved

            for candidate_url in attempts:
                resolved, page = await self._resolve_chain(client, candidate_url)
                if resolved:
                    return resolved
                if page:
                    last_page = page

            if is_ig_share:
                lowered = (last_page or "").lower()
                if any(marker in lowered for marker in (
                    "login_required", "login to continue", "log in to instagram",
                    "accounts/login", "challenge_required", "consent_required",
                )):
                    raise ShareResolverError(
                        "Instagram requires a valid logged-in session to resolve this share link."
                    )
                raise ShareResolverError(
                    "Instagram returned the share page but did not expose its destination Reel/Post. "
                    "Open the share link in a browser and copy the final /reel/, /p/ or /tv/ URL."
                )
            return url
        finally:
            if own_client:
                await client.aclose()


__all__ = ["ShareResolver", "ShareResolverError", "extract_supported_urls", "normalize_supported_url"]
