import json
import tempfile
import unittest
from pathlib import Path

import httpx

from kimi.extractor import InstagramExtractor, InstagramOptions
from kimi.extractor.instagram import (
    InstagramInvalidURL,
    _best_image,
    _best_video,
    _children,
    _normalise_post_url,
    _shortcode_to_media_id,
    extract_instagram_urls,
)


class InstagramParserTests(unittest.TestCase):
    def test_shortcode_decode(self):
        self.assertEqual(_shortcode_to_media_id("B1LbfVPlwIA"), "2110901750722920960")

    def test_normalise_reel(self):
        url, kind, shortcode = _normalise_post_url(
            "https://www.instagram.com/reel/ABC123xyz/?igsh=abc"
        )
        self.assertEqual(url, "https://www.instagram.com/reel/ABC123xyz/")
        self.assertEqual(kind, "reel")
        self.assertEqual(shortcode, "ABC123xyz")

    def test_accept_applink_host(self):
        url, kind, shortcode = _normalise_post_url(
            "https://applink.instagram.com/reel/ABC123xyz/"
        )
        self.assertEqual(url, "https://www.instagram.com/reel/ABC123xyz/")
        self.assertEqual(kind, "reel")
        self.assertEqual(shortcode, "ABC123xyz")

    def test_reject_non_instagram_host(self):
        with self.assertRaises(InstagramInvalidURL):
            _normalise_post_url("https://example.com/p/ABC123xyz/")

    def test_best_image_selects_largest(self):
        node = {"image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/small.jpg", "width": 320, "height": 320},
            {"url": "https://scontent.cdninstagram.com/large.jpg", "width": 1080, "height": 1080},
        ]}}
        self.assertIn("large.jpg", _best_image(node)["url"])

    def test_best_video_selects_highest_resolution(self):
        node = {"video_versions": [
            {"url": "https://scontent.cdninstagram.com/480.mp4", "width": 480, "height": 854},
            {"url": "https://scontent.cdninstagram.com/1080.mp4", "width": 1080, "height": 1920},
        ]}
        self.assertIn("1080.mp4", _best_video(node)["url"])

    def test_carousel_children(self):
        node = {"carousel_media": [{"media_type": 1}, {"media_type": 2}]}
        self.assertEqual(len(_children(node)), 2)

    def test_url_detection(self):
        urls = extract_instagram_urls(
            "Watch https://www.instagram.com/reel/ABC123xyz/?igsh=xx "
            "and https://instagram.com/p/DEF456xyz/"
        )
        self.assertEqual(urls, [
            "https://www.instagram.com/reel/ABC123xyz/",
            "https://www.instagram.com/p/DEF456xyz/",
        ])

    def test_url_detection_includes_applink(self):
        urls = extract_instagram_urls("https://applink.instagram.com/reel/ABC123xyz/")
        self.assertEqual(urls, ["https://www.instagram.com/reel/ABC123xyz/"])


class InstagramAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_public_video_json(self):
        payload = {"data": {"media": {
            "code": "ABC123xyz", "is_video": True, "video_duration": 12.5,
            "user": {"username": "demo"}, "caption": {"text": "Public demo"},
            "video_versions": [{"url": "https://scontent.cdninstagram.com/video.mp4", "width": 720, "height": 1280}],
            "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/thumb.jpg", "width": 720, "height": 1280}]},
        }}}
        page = '<html><script type="application/json">' + json.dumps(payload) + '</script></html>'

        async def handler(request):
            return httpx.Response(200, text=page, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        extractor = InstagramExtractor(client=client)
        try:
            result = await extractor.extract("https://www.instagram.com/reel/ABC123xyz/")
            self.assertEqual(result["type"], "video")
            self.assertEqual(result["username"], "demo")
            self.assertEqual(result["media"][0]["format"], "mp4")
        finally:
            await extractor.close()
            await client.aclose()

    async def test_logged_out_graphql_extract(self):
        product = {
            "code": "B1LbfVPlwIA", "pk": "2110901750722920960",
            "is_video": True, "video_duration": 8.2,
            "user": {"username": "public_user"}, "caption": {"text": "Public reel"},
            "video_versions": [{"url": "https://scontent.cdninstagram.com/reel.mp4", "width": 1080, "height": 1920}],
            "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/cover.jpg", "width": 1080, "height": 1920}]},
        }

        async def handler(request):
            if request.url.path == "/":
                return httpx.Response(200, text='<script id="__eqmc">{"l":"LSD_TEST"}</script>', headers={
                    "Set-Cookie": "csrftoken=CSRF_TEST; Domain=www.instagram.com; Path=/; Secure"
                }, request=request)
            if request.url.path.endswith("/web/get_ruling_for_content/"):
                return httpx.Response(200, json={"status": "ok"}, request=request)
            if request.url.path == "/api/graphql":
                return httpx.Response(200, json={"data": {"xig_polaris_media": {"if_not_gated_logged_out": product}}}, request=request)
            return httpx.Response(404, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        extractor = InstagramExtractor(client=client)
        try:
            result = await extractor.extract("https://www.instagram.com/p/B1LbfVPlwIA/")
            self.assertEqual(result["type"], "video")
            self.assertEqual(result["username"], "public_user")
            self.assertEqual(result["caption"], "Public reel")
        finally:
            await extractor.close()
            await client.aclose()

    async def test_cookie_file_load_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "instagram-cookies.txt"
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
            extractor = InstagramExtractor(
                client=client,
                cookies={"sessionid": "session-test"},
                cookie_file=cookie_file,
            )
            self.assertEqual(extractor.cookie_store.get_for_url("sessionid", "https://www.instagram.com/"), "session-test")
            client.cookies.set("csrftoken", "csrf-test", domain=".instagram.com", path="/")
            await extractor.close()
            self.assertTrue(cookie_file.is_file())
            self.assertEqual(cookie_file.stat().st_mode & 0o777, 0o600)
            saved = cookie_file.read_text(encoding="utf-8")
            self.assertIn("sessionid", saved)
            self.assertIn("csrftoken", saved)
            await client.aclose()

    async def test_chunked_download_writes_file(self):
        payload = b"instagram-test-media" * 5000

        async def handler(request):
            return httpx.Response(200, content=payload, headers={"Content-Length": str(len(payload))}, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        extractor = InstagramExtractor(client=client)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "media.mp4"
            try:
                await extractor._download_one("https://scontent.cdninstagram.com/media.mp4", destination)
                self.assertEqual(destination.read_bytes(), payload)
            finally:
                await extractor.close()
                await client.aclose()

    async def test_download_resumes_after_transport_interruption(self):
        payload = b"abcdefghij" * 10
        calls = []

        class InterruptedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield payload[:40]
                raise httpx.ReadError("simulated connection drop")

            async def aclose(self):
                return None

        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    200, stream=InterruptedStream(),
                    headers={"Content-Length": str(len(payload))}, request=request,
                )
            self.assertEqual(request.headers.get("Range"), "bytes=40-")
            return httpx.Response(
                206, content=payload[40:],
                headers={"Content-Range": f"bytes 40-99/{len(payload)}", "Content-Length": "60"},
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        extractor = InstagramExtractor(client=client, options=InstagramOptions(retries=1))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "resume.mp4"
            try:
                await extractor._download_one("https://scontent.cdninstagram.com/media.mp4", destination)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(len(calls), 2)
                self.assertFalse(destination.with_name(destination.name + ".part").exists())
            finally:
                await extractor.close()
                await client.aclose()


if __name__ == "__main__":
    unittest.main()
