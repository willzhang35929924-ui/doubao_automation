"""Offline contract tests for the optional HTTPS caption provider."""

import base64
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request

from doubao_automation import caption_cloud


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "ScL/nwAAAABJRU5ErkJggg=="
)
SECRET = "fixture-key-never-echo-123"
REPLY = {"choices": [{"message": {"content": "红色产品的近景。"}}]}


class FakeResponse:
    def __init__(self, payload=REPLY, *, status=200):
        self.body = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status
        self.read_sizes = []
        self.bytes_returned = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def read(self, size=-1):
        self.read_sizes.append(size)
        data = self.body.read(size)
        self.bytes_returned += len(data)
        return data

    def getcode(self):
        return self.status

    def close(self):
        self.body.close()


class FakeOpener:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.response


class CaptionCloudTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="caption-cloud-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.image = self.root / "product.png"
        self.image.write_bytes(PNG)

    def call(self, paths=None, kind="images", role="product", **options):
        options.setdefault("base_url", "https://caption.example.invalid/v1")
        options.setdefault("model", "fixture-vision")
        options.setdefault("api_key", SECRET)
        return caption_cloud.generate_cloud_caption(
            paths if paths is not None else [self.image], kind, role, **options
        )

    def captured(self, **options):
        opener = FakeOpener()
        with patch.object(caption_cloud, "build_opener", return_value=opener):
            answer = self.call(**options)
        self.assertEqual(answer, "红色产品的近景。")
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(opener.timeouts, [60])
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET}")
        return request, json.loads(request.data)

    def test_product_image_uses_data_url_and_expected_chat_endpoint(self):
        request, body = self.captured(instructions="只描述可见的外观")
        self.assertEqual(request.full_url, "https://caption.example.invalid/v1/chat/completions")
        self.assertEqual(body["model"], "fixture-vision")
        content = body["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertTrue(any(item.get("type") == "text" for item in content))
        images = [item["image_url"]["url"] for item in content if item.get("type") == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/"))
        header, payload = images[0].split(",", 1)
        self.assertIn(";base64", header)
        self.assertTrue(base64.b64decode(payload))
        self.assertNotIn(str(self.image), json.dumps(body, ensure_ascii=False))

    def test_complete_https_endpoint_is_not_appended_twice(self):
        request, _ = self.captured(
            base_url="https://caption.example.invalid/v1/chat/completions"
        )
        self.assertEqual(request.full_url, "https://caption.example.invalid/v1/chat/completions")

    def test_six_images_are_supported_and_seven_rejected_before_network(self):
        images = []
        for index in range(7):
            path = self.root / f"{index}.png"
            path.write_bytes(PNG)
            images.append(path)
        _, body = self.captured(paths=images[:6])
        content = body["messages"][0]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in content), 6)
        opener = FakeOpener()
        with patch.object(caption_cloud, "build_opener", return_value=opener):
            with self.assertRaises(Exception):
                self.call(paths=images)
        self.assertFalse(opener.requests)

    def test_http_and_invalid_role_are_rejected_before_network(self):
        opener = FakeOpener()
        with patch.object(caption_cloud, "build_opener", return_value=opener):
            with self.assertRaises(Exception):
                self.call(base_url="http://caption.example.invalid/v1")
            with self.assertRaises(Exception):
                self.call(role="reference")
        self.assertFalse(opener.requests)

    def test_redirect_handler_refuses_cross_host_redirect(self):
        opener = FakeOpener()
        with patch.object(caption_cloud, "build_opener", return_value=opener) as factory:
            self.call()
        handlers = [value() if isinstance(value, type) else value for value in factory.call_args.args]
        redirects = [value for value in handlers if isinstance(value, HTTPRedirectHandler)]
        self.assertTrue(redirects, "HTTPS requests must install an explicit no-redirect handler")
        request = opener.requests[0]
        for handler in redirects:
            try:
                redirected = handler.redirect_request(
                    request, None, 302, "Moved", {"Location": "https://other.invalid/"},
                    "https://other.invalid/"
                )
            except HTTPError:
                continue
            self.assertIsNone(redirected, "a redirect could leak the provider key or image")

    def test_http_error_does_not_echo_key_or_provider_body(self):
        provider_body = f"provider failure {SECRET} private-provider-detail".encode()
        error = HTTPError(
            "https://caption.example.invalid/v1/chat/completions", 403,
            "Forbidden", {}, io.BytesIO(provider_body)
        )
        class ErrorOpener(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                raise error
        opener = ErrorOpener()
        with patch.object(caption_cloud, "build_opener", return_value=opener):
            with self.assertRaises(Exception) as caught:
                self.call()
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("private-provider-detail", message)
        self.assertNotIn(provider_body.decode(), message)

    def test_success_response_echoing_api_key_is_rejected_without_echo(self):
        for key in (SECRET, "a7C"):
            with self.subTest(length=len(key)):
                response = FakeResponse({"choices": [{"message": {"content": f"识别结果 {key}"}}]})
                opener = FakeOpener(response)
                with patch.object(caption_cloud, "build_opener", return_value=opener):
                    with self.assertRaises(caption_cloud.CaptionRuntimeError) as caught:
                        self.call(api_key=key)
                self.assertEqual(len(opener.requests), 1)
                self.assertNotIn(key, str(caught.exception))

    def test_response_read_is_bounded(self):
        oversized = FakeResponse(b'{"choices":' + b" " * (6 * 1024 * 1024))
        opener = FakeOpener(oversized)
        with patch.object(caption_cloud, "build_opener", return_value=opener):
            with self.assertRaises(Exception):
                self.call()
        self.assertTrue(oversized.read_sizes)
        self.assertTrue(all(isinstance(size, int) and 0 < size <= 4 * 1024 * 1024
                            for size in oversized.read_sizes))
        self.assertLessEqual(oversized.bytes_returned, 4 * 1024 * 1024 + 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_video_uploads_only_bounded_png_frames(self):
        video = self.root / "fixture.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
             "-i", "color=c=red:s=48x48:r=6:d=1", "-c:v", "mpeg4", "-q:v", "3", str(video)],
            check=True, timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertTrue(video.is_file())
        _, body = self.captured(paths=[video], kind="video", role="reference")
        content = body["messages"][0]["content"]
        frames = [item["image_url"]["url"] for item in content if item.get("type") == "image_url"]
        self.assertGreaterEqual(len(frames), 1)
        self.assertLessEqual(len(frames), 6)
        self.assertTrue(all(item.startswith("data:image/png;base64,") for item in frames))
        self.assertTrue(all(base64.b64decode(item.split(",", 1)[1]).startswith(b"\x89PNG")
                            for item in frames))
        self.assertNotIn(str(video), json.dumps(body, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
