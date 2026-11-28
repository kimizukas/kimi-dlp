# Kimi-DLP 0.2.0

Asynchronous YouTube search and media extraction, plus Instagram post/Reel extraction.
This archive keeps the existing YouTube search implementation unchanged and adds
shared cookie handling, retry policy, safe share-link resolution, and resumable
Instagram media downloads.

## What changed

- **YouTube search:** `kimi/search/youtube_search.py` is preserved byte-for-byte from
  the supplied project snapshot. Its API and ranking logic were not edited.
- **Cookies:** a shared Netscape/httpx cookie store supports cookie files, raw Netscape
  text, mappings, `httpx.Cookies`, domain/path/secure/expiry and HttpOnly attributes.
  Extractors can save updated cookies explicitly or on close when a cookie-file path
  is configured. Cookie files are written atomically with owner-only permissions.
- **YouTube extractor:** retains its current Innertube clients, PO-token provider,
  metadata/stream caches, playlists and range-resume downloader. Cookie loading now
  uses the shared store; cookie-file persistence and additional transient HTTP retries
  are available.
- **Instagram extractor:** retains logged-out and session-cookie media extraction.
  It accepts direct cookies/cookie files, uses the common retry policy for API calls,
  accepts Instagram AppLinks, and resumes interrupted CDN transfers using HTTP Range.
- **Share URLs:** `ShareResolver` follows only an explicit Instagram/YouTube host
  allowlist. It validates every redirect hop and refuses arbitrary external hosts.
- **Cython:** `_youtube_fast.pyx` is optional. Pure-Python fallback remains available.

## Install

```bash
python -m pip install -U pip setuptools wheel
python -m pip install .
```

Optional Cython accelerator:

```bash
python -m pip install '.[fast]'
python setup.py build_ext --inplace
```

Run offline tests:

```bash
python -m pip install '.[test]'
python -m pytest -q
```

## Cookies

Use a private Netscape-format cookie export. Never commit or share real login cookies.

```python
from kimi.extractor import YouTubeExtractor, InstagramExtractor

async with YouTubeExtractor(cookie_file="cookies.txt") as yt:
    info = await yt.extract("https://www.youtube.com/watch?v=VIDEO_ID")
    print(info["title"])
    # cookies.txt is saved on close if it was loaded as a file.

async with InstagramExtractor(cookie_file="instagram-cookies.txt") as ig:
    info = await ig.extract("https://www.instagram.com/reel/SHORTCODE/")
    files = await ig.download(info, output_dir="downloads")
```

You may also pass a mapping or an existing `httpx.AsyncClient`:

```python
import httpx
from kimi.extractor import InstagramExtractor

client = httpx.AsyncClient(cookies={"sessionid": "YOUR_SESSION_COOKIE"})
extractor = InstagramExtractor(client=client)
try:
    info = await extractor.extract("https://www.instagram.com/p/SHORTCODE/")
finally:
    await extractor.close()  # does not close an externally-owned client
    await client.aclose()
```

Cookie files contain account credentials. Store them outside the repository and
restrict filesystem access. A cookie does not grant access to private content the
account itself cannot access, and this package does not bypass login challenges,
CAPTCHAs, DRM or access controls.

## Retry and resume

- Retryable HTTP statuses: 408, 425, 429, selected 5xx and common CDN transient 52x.
- `Retry-After` delta-seconds and HTTP-date values are honored within a configured cap.
- 401/403 are not blindly retried as generic network failures.
- YouTube keeps its existing `.part` plus metadata range-resume logic.
- Instagram writes to `*.part`; a later `download()` call resumes when the CDN accepts
  the requested byte range. A server that ignores Range causes a safe restart, not append.

## Share URLs

```python
from kimi.extractor import ShareResolver, extract_supported_urls

links = extract_supported_urls(message_text)
resolver = ShareResolver()
final_url = await resolver.resolve(links[0])
```

`ShareResolver` currently supports Instagram and YouTube first-party/share hosts only.
It is intentionally not an arbitrary URL fetcher.

## Public API

```python
from kimi.extractor import (
    YouTubeExtractor, YouTubeOptions,
    InstagramExtractor, InstagramOptions,
    ShareResolver, extract_supported_urls,
    create_default_registry,
)
from kimi import CookieStore
from kimi.search import YouTubeSearch
```

The host registry currently registers Instagram and YouTube. Use
`registry = create_default_registry()` and `registry.create_for_url(url, cookies=...)`
to construct the matching extractor. The search module's implementation is
intentionally unchanged in this update.
Live platform endpoints are volatile; offline tests do not guarantee that a site's
current anti-bot/login policy or media URL will work for every account or region.
