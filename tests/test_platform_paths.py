"""Platform path and browser defaults: simulated without creating profiles."""
import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from doubao_automation.browser_pool import BrowserSession
from doubao_automation import config, gemini_web


def load_paths(platform, environment):
    spec = importlib.util.spec_from_file_location("platform_paths_under_test", ROOT / "doubao_automation" / "paths.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "platform", platform), patch.dict(os.environ, environment, clear=True):
        spec.loader.exec_module(module)
    return module


class PlatformPathTests(unittest.TestCase):
    def test_windows_keeps_original_paths(self):
        paths = load_paths("win32", {})
        self.assertEqual(paths.DATA_ROOT, ROOT)
        self.assertEqual(paths.PROFILES, ROOT / "profiles")
        self.assertEqual(paths.WORKSPACE, ROOT / "workspace")

    def test_macos_uses_user_application_support_without_source_writes(self):
        with patch.object(Path, "home", return_value=ROOT / "simulated_home"):
            paths = load_paths("darwin", {})
        expected = ROOT / "simulated_home" / "Library" / "Application Support" / "DoubaoAutomation"
        self.assertEqual(paths.DATA_ROOT, expected)
        self.assertEqual(paths.CONFIG_DIR, expected / "config")
        self.assertEqual(paths.resolve("profiles/test"), (expected / "profiles/test").resolve())
        self.assertEqual(paths.resolve("scripts/example.sh"), ROOT / "scripts/example.sh")

    def test_override_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "DOUBAO_DATA_DIR"):
            load_paths("darwin", {"DOUBAO_DATA_DIR": "relative/path"})
        paths = load_paths("darwin", {"DOUBAO_DATA_DIR": str(ROOT / "isolated")})
        self.assertEqual(paths.DATA_ROOT, ROOT / "isolated")

    def test_data_paths_cannot_escape(self):
        paths = load_paths("darwin", {"DOUBAO_DATA_DIR": str(ROOT / "isolated")})
        with self.assertRaises(ValueError):
            paths.resolve("profiles/../../outside")

    def test_config_override_requires_absolute_path(self):
        with patch.dict(os.environ, {"DOUBAO_CONFIG_PATH": "relative.yaml"}):
            with self.assertRaisesRegex(ValueError, "DOUBAO_CONFIG_PATH"):
                config._default_config_path()


class AuditPathTests(unittest.TestCase):
    def test_macos_relative_diagnostics_stay_under_user_data(self):
        data = ROOT.parent / "simulated_user_data"
        with patch.object(sys, "platform", "darwin"), patch.object(gemini_web, "ROOT", ROOT), patch.object(gemini_web, "DATA_ROOT", data):
            self.assertEqual(gemini_web._resolve_audit_dir("diagnostics"), (data / "diagnostics").resolve())
            with self.assertRaisesRegex(ValueError, "逃逸"):
                gemini_web._resolve_audit_dir("../outside")

    def test_macos_diagnostics_cannot_write_to_source_tree(self):
        with patch.object(sys, "platform", "darwin"), patch.object(gemini_web, "ROOT", ROOT), patch.object(gemini_web, "DATA_ROOT", ROOT):
            with self.assertRaisesRegex(ValueError, "源码"):
                gemini_web._resolve_audit_dir("diagnostics")

    def test_windows_audit_configuration_keeps_legacy_resolution(self):
        desired = ROOT / "legacy_diagnostics"
        with patch.object(sys, "platform", "win32"), patch.object(gemini_web, "resolve", return_value=desired) as resolver:
            self.assertEqual(gemini_web._resolve_audit_dir("legacy_diagnostics"), desired)
            resolver.assert_called_once_with("legacy_diagnostics")

class BrowserDefaultsTests(unittest.TestCase):
    def _start(self, platform, launch_side_effect=None):
        from playwright import sync_api
        agent = BrowserSession.__new__(BrowserSession)
        agent.name = "test"
        agent.cfg = {}
        agent.profile_dir = ROOT / "unused_profile"
        agent.start_url = ""
        agent.context = None
        agent.page = None
        agent._pw = None
        agent._lock = threading.RLock()
        context = MagicMock()
        context.pages = [MagicMock()]
        driver = MagicMock()
        driver.chromium.launch_persistent_context.side_effect = launch_side_effect or [context]
        controller = MagicMock()
        controller.start.return_value = driver
        with patch.object(sys, "platform", platform), patch.object(sync_api, "sync_playwright", return_value=controller), patch.object(Path, "is_file", return_value=True):
            agent.start()
        return driver.chromium.launch_persistent_context.call_args_list

    def test_macos_profiles_stay_in_user_data(self):
        profiles = ROOT / "simulated_data" / "profiles"
        with patch.object(sys, "platform", "darwin"), patch("doubao_automation.browser_pool.PROFILES", profiles), patch("doubao_automation.browser_pool.resolve", side_effect=lambda p: p), patch.object(Path, "mkdir"):
            session = BrowserSession("test", profiles / "account", {})
            self.assertEqual(session.profile_dir, (profiles / "account").resolve())
            with self.assertRaisesRegex(ValueError, "profiles"):
                BrowserSession("test", ROOT / "outside_profile", {})
    def test_macos_defaults_to_playwright_chromium(self):
        calls = self._start("darwin")
        self.assertNotIn("channel", calls[0].kwargs)

    def test_windows_still_defaults_to_chrome(self):
        calls = self._start("win32")
        self.assertEqual(calls[0].kwargs["channel"], "chrome")

    def test_windows_missing_chrome_can_fallback_to_installed_chromium(self):
        context = MagicMock()
        context.pages = [MagicMock()]
        calls = self._start("win32", [RuntimeError("Executable does not exist"), context])
        self.assertEqual(calls[0].kwargs["channel"], "chrome")
        self.assertNotIn("channel", calls[1].kwargs)


if __name__ == "__main__":
    unittest.main()
