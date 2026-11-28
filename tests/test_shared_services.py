from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from kimi.cookies import CookieStore, CookieStoreError
from kimi.extractor.share import (
    ShareResolver, ShareResolverError, extract_supported_urls,
    normalize_supported_url,
)
from kimi.extractor.registry import (
    ExtractorRegistry, UnsupportedSiteError, create_default_registry,
)
from kimi.extractor.youtube import YouTubeExtractor
from kimi.network import RETRYABLE_STATUS_CODES, RetryPolicy


class CookieStoreTests(unittest.TestCase):
    def test_loads_netscape_flags_and_http_only(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue-one\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tvalue-two\n"
            "www.youtube.com\tFALSE\t/watch\tFALSE\t0\tHOSTONLY\tvalue-three\n"
        )
        store = CookieStore(text, default_domain=".youtube.com")
        self.assertEqual(store.get_for_url("SID", "https://www.youtube.com/"), "value-one")
        self.assertEqual(store.get_for_url("HSID", "https://www.youtube.com/"), "value-two")
        self.assertEqual(store.get_for_url("HOSTONLY", "http://www.youtube.com/watch"), "value-three")
        stats = store.stats()
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["http_only"], 1)
        self.assertEqual(stats["secure"], 2)

    def test_mapping_is_scoped_to_requested_domain(self):
        store = CookieStore({"sessionid": "sensitive"}, default_domain=".instagram.com")
        self.assertEqual(store.get_for_url("sessionid", "https://www.instagram.com/"), "sensitive")
        self.assertIsNone(store.cookies.get("sessionid", domain="youtube.com"))

    def test_atomic_private_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            store = CookieStore({"SID": "abc"}, default_domain=".youtube.com", path=path)
            store.cookies.set("HTTPONLY", "secret", domain=".youtube.com", path="/")
            store.load("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tTOKEN\txyz\n")
            store.save()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = CookieStore(path, default_domain=".youtube.com")
            self.assertEqual(loaded.get_for_url("SID", "https://www.youtube.com/"), "abc")
            self.assertEqual(loaded.get_for_url("TOKEN", "https://www.youtube.com/"), "xyz")
            self.assertEqual(loaded.stats()["http_only"], 1)

    def test_nonexistent_path_object_is_not_treated_as_cookie_text(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CookieStoreError):
                CookieStore(Path(directory) / "missing.txt")


class RetryPolicyTests(unittest.TestCase):
    def test_common_transient_codes_are_enabled(self):
        policy = RetryPolicy(jitter=0)
        for status in (408, 425, 429, 500, 502, 503, 504, 520, 524):
            self.assertIn(status, RETRYABLE_STATUS_CODES)
            self.assertTrue(policy.should_retry(status))
        self.assertFalse(policy.should_retry(403))
        self.assertFalse(policy.should_retry(404))

    def test_retry_after_delta_is_respected_and_capped(self):
        response = httpx.Response(429, headers={"Retry-After": "4"})
        self.assertEqual(RetryPolicy(max_delay=3, jitter=0).delay(response, 0), 3)

    def test_retry_after_http_date_is_respected(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = httpx.Response(503, headers={"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")})
        delay = RetryPolicy(max_delay=5, jitter=0).delay(response, 0)
        self.assertGreaterEqual(delay, 0)
        self.assertLessEqual(delay, 2.2)

    def test_exponential_backoff_is_capped(self):
        policy = RetryPolicy(base_delay=0.5, max_delay=2, jitter=0)
        self.assertEqual(policy.delay(None, 0), 0.5)
        self.assertEqual(policy.delay(None, 1), 1.0)
        self.assertEqual(policy.delay(None, 5), 2.0)


class ShareResolverTests(unittest.TestCase):
    def test_normalizes_instagram_applink(self):
        self.assertEqual(
            normalize_supported_url("https://applink.instagram.com/reel/ABC123xyz/?x=1"),
            "https://www.instagram.com/reel/ABC123xyz/?x=1",
        )

    def test_extracts_only_allowlisted_social_urls(self):
        urls = extract_supported_urls(
            "IG https://applink.instagram.com/reel/ABC123xyz/ "
            "YT https://youtu.be/abcdefghijk?t=3 "
            "bad https://example.com/watch/1"
        )
        self.assertEqual(len(urls), 2)
        self.assertIn("https://www.instagram.com/reel/ABC123xyz/", urls)
        self.assertTrue(any("youtu.be" in url for url in urls))

    def test_rejects_arbitrary_host(self):
        with self.assertRaises(ShareResolverError):
            normalize_supported_url("https://evil.example/reel/ABC123xyz")

    async def _unused(self):
        return None

    def test_redirects_are_checked_at_every_hop(self):
        async def run():
            async def handler(request):
                if request.url.host == "youtu.be":
                    return httpx.Response(302, headers={"Location": "https://www.youtube.com/watch?v=abcdefghijk"}, request=request)
                return httpx.Response(200, text="ok", request=request)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            resolver = ShareResolver(client=client)
            try:
                result = await resolver.resolve("https://youtu.be/abcdefghijk")
                self.assertEqual(result, "https://www.youtube.com/watch?v=abcdefghijk")
            finally:
                await client.aclose()

        asyncio.run(run())

    def test_external_redirect_is_rejected(self):
        async def run():
            async def handler(request):
                return httpx.Response(302, headers={"Location": "https://evil.example/collect"}, request=request)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            resolver = ShareResolver(client=client)
            try:
                with self.assertRaises(ShareResolverError):
                    await resolver.resolve("https://youtu.be/abcdefghijk")
            finally:
                await client.aclose()

        asyncio.run(run())


class ExtractorRegistryTests(unittest.TestCase):
    def test_builtin_registry_maps_youtube_and_instagram(self):
        registry = create_default_registry()
        self.assertEqual(
            registry.platform_for_url("https://applink.instagram.com/reel/ABC123xyz/"),
            "instagram",
        )
        self.assertEqual(registry.platform_for_url("https://youtu.be/abcdefghijk"), "youtube")
        self.assertIn("instagram", registry.registered_sites())

    def test_unknown_host_and_credentials_are_rejected(self):
        registry = create_default_registry()
        with self.assertRaises(UnsupportedSiteError):
            registry.platform_for_url("https://example.com/video")
        with self.assertRaises(UnsupportedSiteError):
            registry.platform_for_url("https://user:pass@youtube.com/watch?v=abcdefghijk")

    def test_register_duplicate_host_requires_explicit_replace(self):
        registry = ExtractorRegistry()
        registry.register("one", {"example.com"}, lambda: object())
        with self.assertRaises(ValueError):
            registry.register("two", {"example.com"}, lambda: object())
        self.assertTrue(registry.unregister("one"))
        registry.register("two", {"example.com"}, lambda: object())
        self.assertEqual(registry.platform_for_url("https://example.com/x"), "two")


class YouTubeCookieAndRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_file_is_merged_into_external_client_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "youtube-cookies.txt"
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
            extractor = YouTubeExtractor(
                client=client, cookies={"SAPISID": "cookie-value"}, cookie_file=cookie_file
            )
            self.assertEqual(extractor.cookie_store.get_for_url("SAPISID", "https://www.youtube.com/"), "cookie-value")
            client.cookies.set("VISITOR_INFO1_LIVE", "visitor", domain=".youtube.com", path="/")
            await extractor.close()
            self.assertTrue(cookie_file.is_file())
            saved = CookieStore(cookie_file, default_domain=".youtube.com")
            self.assertEqual(saved.get_for_url("VISITOR_INFO1_LIVE", "https://www.youtube.com/"), "visitor")
            await client.aclose()


    async def test_range_ignored_fallback_streams_to_disk(self):
        payload = b"streamed-media" * 4000
        async def handler(request):
            return httpx.Response(200, content=payload, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = YouTubeExtractor(client=client)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ignored-range.part"
            try:
                result = await extractor._download_range(
                    "https://r.googlevideo.com/media", 0, 1023,
                    video_id="abcdefghijk", user_agent="Kimi-Test", timeout=5,
                    output_path=output,
                )
                self.assertTrue(result["written_to_file"])
                self.assertEqual(result["total"], len(payload))
                self.assertEqual(output.read_bytes(), payload)
                self.assertEqual(result["data"], b"")
            finally:
                await extractor.close()
                await client.aclose()

    async def test_request_retries_408_and_returns_success(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(408, request=request)
            return httpx.Response(200, text="ok", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = YouTubeExtractor(client=client)
        try:
            with patch("kimi.extractor.youtube.asyncio.sleep", new=AsyncMock()):
                response = await extractor._request("GET", "https://www.youtube.com/test")
                try:
                    self.assertEqual(response.status_code, 200)
                finally:
                    await response.aclose()
            self.assertEqual(attempts, 2)
        finally:
            await extractor.close()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()

class HardeningRegressionTests(unittest.TestCase):
    def test_explicit_http_only_mapping_survives_cookie_file_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            store = CookieStore(
                {"session": {"value": "abc", "domain": ".instagram.com", "httponly": True}},
                default_domain=".instagram.com",
                path=path,
            )
            store.save()
            content = path.read_text(encoding="utf-8")
            self.assertIn("#HttpOnly_.instagram.com", content)
            self.assertEqual(store.stats()["http_only"], 1)

    def test_share_and_registry_reject_nonstandard_ports(self):
        with self.assertRaises(ShareResolverError):
            normalize_supported_url("https://www.instagram.com:8080/reel/ABC123xyz/")
        with self.assertRaises(UnsupportedSiteError):
            create_default_registry().platform_for_url("https://youtu.be:444/abcdefghijk")

    def test_instagram_media_urls_require_https_and_standard_port(self):
        from kimi.extractor.instagram import _is_allowed_media_url
        self.assertTrue(_is_allowed_media_url("https://cdninstagram.com/media.mp4"))
        self.assertFalse(_is_allowed_media_url("http://cdninstagram.com/media.mp4"))
        self.assertFalse(_is_allowed_media_url("https://cdninstagram.com:8443/media.mp4"))

class ExtractorCookieFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_cookie_file_is_loaded_on_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "youtube.txt"
            cookie_file.write_text(
                ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tloaded-value\n",
                encoding="utf-8",
            )
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
            extractor = YouTubeExtractor(client=client, cookie_file=cookie_file)
            try:
                request = client.build_request("GET", "https://www.youtube.com/")
                self.assertIn("SAPISID=loaded-value", request.headers.get("cookie", ""))
            finally:
                await extractor.close()
                await client.aclose()

    async def test_instagram_cookie_file_is_loaded_and_saved(self):
        from kimi.extractor.instagram import InstagramExtractor
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "instagram.txt"
            cookie_file.write_text(
                ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\told-value\n",
                encoding="utf-8",
            )
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
            extractor = InstagramExtractor(client=client, cookie_file=cookie_file)
            try:
                request = client.build_request("GET", "https://www.instagram.com/")
                self.assertIn("sessionid=old-value", request.headers.get("cookie", ""))
                client.cookies.set("sessionid", "updated-value", domain=".instagram.com", path="/")
                await extractor.close()
                saved = CookieStore(cookie_file, default_domain=".instagram.com")
                self.assertEqual(
                    saved.get_for_url("sessionid", "https://www.instagram.com/"),
                    "updated-value",
                )
            finally:
                await client.aclose()
