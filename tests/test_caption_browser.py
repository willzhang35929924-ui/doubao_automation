"""The product reader uses isolated browser state and guarded public transport."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from doubao_automation import caption_browser as reader
from doubao_automation.caption_page import CaptionPageError


URL = "https://shop.tiktok.com/us/pdp/1732813990412915186"
PROXY = {"server": "http://127.0.0.1:31001", "username": "fixture-user", "password": "fixture-secret"}


def snapshot(**changes):
    return dict(url=URL, title="Plush Dumplings - TikTok Shop", heading="Plush Dumplings",
                text="Material: PP cotton. Packaging: a box with dumpling plush toys and a steamer-style basket.",
                product_section=True, verification=False) | changes


class CaptionBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = Mock()
        self.browser = self.playwright.chromium.launch.return_value
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.evaluate.return_value = snapshot()
        self.runtime = patch.object(reader, "resolve_browser_runtime", return_value={"available": True, "channel": None})
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def read(self):
        return reader._read_with_browser(self.playwright, URL, PROXY, time.monotonic() + 20)

    def test_rendered_product_uses_fresh_sandboxed_browser_and_closes_it(self):
        product = self.read()
        self.assertEqual(product.url, URL)
        self.assertEqual(product.title, "Plush Dumplings")
        self.assertIn("PP cotton", product.text)
        options = self.playwright.chromium.launch.call_args.kwargs
        self.assertFalse(options["headless"])
        self.assertTrue(options["chromium_sandbox"])
        self.assertEqual(options["proxy"], PROXY)
        self.assertIn("--proxy-bypass-list=<-loopback>", options["args"])
        self.assertIn("--disable-quic", options["args"])
        self.assertNotIn("ignore_https_errors", self.browser.new_context.call_args.kwargs)
        self.browser.new_context.assert_called_once_with(
            accept_downloads=False, service_workers="block", locale="en-US")
        self.context.route_web_socket.assert_called_once()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.playwright.chromium.launch_persistent_context.assert_not_called()

    def test_navigation_subresources_and_websockets_obey_guard(self):
        self.read()
        handle = self.context.route.call_args.args[1]

        def route(url, resource="script", navigation=False, frame=None):
            value = Mock()
            value.request.url = url
            value.request.resource_type = resource
            value.request.is_navigation_request.return_value = navigation
            value.request.frame = self.page.main_frame if frame is None else frame
            handle(value)
            return value

        allowed = route("https://cdn.example.com/product.js")
        allowed.continue_.assert_called_once()
        route(URL, "document", True).continue_.assert_called_once()
        for url in ("http://cdn.example.com/product.js", "file:///etc/passwd", "https://127.0.0.1/",
                    "https://192.168.1.1/", "https://localhost/", "https://user:secret@example.com/"):
            with self.subTest(url=url):
                route(url).abort.assert_called_once()
        route("https://cdn.example.com/hero.jpg", "image").abort.assert_called_once()
        route(URL + "0", "document", True).abort.assert_called_once()
        route(URL, "document", True, frame=Mock()).abort.assert_called_once()
        websocket = Mock()
        self.context.route_web_socket.call_args.args[1](websocket)
        websocket.close.assert_called_once()

    def test_verification_and_redirect_errors_close_browser_without_returning_data(self):
        for data in (snapshot(title="Security Check"), snapshot(verification=True),
                     snapshot(url="https://shop.tiktok.com/us/pdp/1732813990412915187")):
            with self.subTest(data=data), self.assertRaises(CaptionPageError):
                self.page.evaluate.return_value = data
                self.read()
        self.assertEqual(self.context.close.call_count, 3)
        self.assertEqual(self.browser.close.call_count, 3)

    def test_waits_for_real_product_heading_and_bounds_output(self):
        self.page.evaluate.side_effect = [snapshot(product_section=False), snapshot(text="商品资料" * 3000)]
        product = self.read()
        self.page.wait_for_timeout.assert_called_once()
        self.assertLessEqual(len(product.text), reader._MAX_TEXT)

    def test_no_browser_or_network_starts_for_invalid_entry_link(self):
        fake_proxy = Mock()
        module = SimpleNamespace(public_browser_proxy=fake_proxy)
        with patch.dict(sys.modules, {"doubao_automation.caption_browser_proxy": module}):
            for url in ("https://shop.example.com/item", "http://shop.tiktok.com/us/pdp/1732813990412915186",
                        "https://shop.tiktok.com/us/store/1732813990412915186", "file:///etc/passwd"):
                with self.subTest(url=url), self.assertRaises(CaptionPageError):
                    reader.read_browser_product_page(url)
        fake_proxy.assert_not_called()

    def test_driver_errors_are_safe_and_proxy_lifetime_covers_browser_cleanup(self):
        events = []

        @contextlib.contextmanager
        def proxy(**kwargs):
            events.append("proxy start")
            try:
                yield PROXY
            finally:
                events.append("proxy closed")

        self.page.evaluate.side_effect = PlaywrightError("fixture-secret full original url")
        self.browser.close.side_effect = lambda: events.append("browser closed")
        module = SimpleNamespace(public_browser_proxy=proxy)
        driver = Mock()
        driver.__enter__ = Mock(return_value=self.playwright)
        driver.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {"doubao_automation.caption_browser_proxy": module}), \
             patch.object(reader, "sync_playwright", return_value=driver):
            with self.assertRaisesRegex(CaptionPageError, "浏览器无法读取") as failure:
                reader._read_browser_product_page(URL, None)
        self.assertNotIn("fixture-secret", str(failure.exception))
        self.assertEqual(events, ["proxy start", "browser closed", "proxy closed"])

    def test_subframe_navigation_is_aborted_without_rejecting_real_product(self):
        subframe = Mock()
        subframe.request.url = "https://www.tiktok.com/embedded-widget"
        subframe.request.resource_type = "document"
        subframe.request.frame = Mock()
        subframe.request.is_navigation_request.return_value = True

        def navigate(*args, **kwargs):
            self.context.route.call_args.args[1](subframe)

        self.page.goto.side_effect = navigate
        product = self.read()
        subframe.abort.assert_called_once()
        self.assertIn("PP cotton", product.text)

    @unittest.skipIf(os.name == "nt", "POSIX owned process group cleanup")
    def test_hung_worker_stops_only_owned_groups_and_keeps_url_off_command_line(self):
        process = Mock(pid=23456)
        process.wait.side_effect = [subprocess.TimeoutExpired("owned worker", reader._PROCESS_TIMEOUT), 0]
        process.poll.return_value = None
        original = URL + "?sku_id=private-fixture-value"

        def start(command, **kwargs):
            self.assertNotIn(original, command)
            request_path, _, manifest = map(Path, command[-3:])
            self.assertEqual(json.loads(request_path.read_text())["url"], original)
            self.assertEqual(request_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            manifest.write_text(json.dumps({"worker_pid": process.pid, "browser_pid": 34567}))
            return process

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(reader, "TEMP_DIR", Path(directory)), \
             patch.object(reader.subprocess, "Popen", side_effect=start), \
             patch.object(reader.os, "getpgid", return_value=34567), \
             patch.object(reader.os, "killpg") as kill, \
             patch.object(reader.time, "sleep"), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-do-not-forward"}):
            with self.assertRaisesRegex(CaptionPageError, "超过时限"):
                reader.read_browser_product_page(original)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(kill.call_args_list,
                         [unittest.mock.call(34567, signal.SIGKILL), unittest.mock.call(23456, signal.SIGKILL)])

    @unittest.skipIf(os.name == "nt", "POSIX owned process group cleanup")
    def test_successful_worker_cleans_only_a_lingering_owned_browser_group(self):
        process = Mock(pid=23456, returncode=0)
        process.wait.return_value = 0
        process.poll.return_value = 0

        def start(command, **kwargs):
            _, result, manifest = map(Path, command[-3:])
            result.write_text(json.dumps({"url": URL, "title": "Plush Dumplings", "text": "Actual product facts."}))
            manifest.write_text(json.dumps({"worker_pid": process.pid, "browser_pid": 34567}))
            return process

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(reader, "TEMP_DIR", Path(directory)), \
             patch.object(reader.subprocess, "Popen", side_effect=start), \
             patch.object(reader.os, "getpgid", return_value=34567), \
             patch.object(reader.os, "killpg") as kill:
            product = reader.read_browser_product_page(URL)
        self.assertEqual(product.text, "Actual product facts.")
        kill.assert_called_once_with(34567, signal.SIGKILL)

    @unittest.skipIf(os.name == "nt", "POSIX owned process group cleanup")
    def test_foreign_process_manifest_never_signals_reported_browser_group(self):
        process = Mock(pid=23456)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "browser.json"
            manifest.write_text(json.dumps({"worker_pid": 99999, "browser_pid": 34567}))
            with patch.object(reader.os, "killpg") as kill, patch.object(reader.time, "sleep"):
                reader._stop_worker(process, manifest)
        self.assertEqual([item.args[0] for item in kill.call_args_list], [process.pid, process.pid])


if __name__ == "__main__":
    unittest.main()
