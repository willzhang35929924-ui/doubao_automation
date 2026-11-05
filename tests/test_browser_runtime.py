"""Browser version drift and launch failure tests; no real browser/profile."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from doubao_automation import browser_runtime
from doubao_automation.browser_pool import BrowserSession


class RuntimeSelectionTests(unittest.TestCase):
    managed = Path('/fixture/current-chromium/browser')

    def select(self, cfg, present, platform='darwin'):
        with patch.object(Path, 'is_file', autospec=True,
                          side_effect=lambda path: path in present):
            return browser_runtime.resolve_browser_runtime(cfg, self.managed, platform=platform)

    def test_matching_managed_version_remains_the_macos_default(self):
        result = self.select({}, {self.managed, browser_runtime.MAC_CHROME})
        self.assertTrue(result['available'])
        self.assertIsNone(result['channel'])
        self.assertEqual(result['executable'], str(self.managed))

    def test_old_cached_version_is_not_used_and_system_chrome_can_open_separate_profile(self):
        result = self.select({}, {Path('/fixture/old-chromium/browser'), browser_runtime.MAC_CHROME})
        self.assertTrue(result['available'])
        self.assertEqual(result['channel'], 'chrome')
        self.assertEqual(result['source'], 'system-chrome')

    def test_explicit_chromium_never_silently_switches_to_system_chrome(self):
        result = self.select({'browser': {'channel': 'chromium'}}, {browser_runtime.MAC_CHROME})
        self.assertFalse(result['available'])
        self.assertTrue(result['install_managed'])
        self.assertIsNone(result['channel'])
        self.assertIn('-m playwright install chromium', result['error'])

    def test_explicit_chrome_never_silently_switches_to_managed_chromium(self):
        result = self.select({'browser': {'channel': 'chrome'}}, {self.managed})
        self.assertFalse(result['available'])
        self.assertFalse(result['install_managed'])
        self.assertEqual(result['channel'], 'chrome')

    def test_missing_browser_reports_how_the_matching_version_can_be_installed(self):
        result = self.select({}, set())
        self.assertFalse(result['available'])
        self.assertTrue(result['install_managed'])
        self.assertIn('一键启动入口', result['error'])
        self.assertIn('-m playwright install chromium', result['error'])

    def test_windows_default_still_uses_chrome_channel(self):
        result = self.select({}, set(), platform='win32')
        self.assertTrue(result['available'])
        self.assertEqual(result['channel'], 'chrome')
        self.assertFalse(result['explicit'])


class BrowserLaunchTests(unittest.TestCase):
    def setup_session(self, cfg=None):
        session = BrowserSession.__new__(BrowserSession)
        session.name = 'fixture'
        session.cfg = cfg or {}
        session.profile_dir = ROOT / 'unused-profile-fixture'
        session.start_url = ''
        session.context = session.page = session._pw = None
        session._lock = threading.RLock()
        driver = MagicMock()
        driver.chromium.executable_path = '/fixture/current-chromium/browser'
        context = MagicMock()
        context.pages = [MagicMock()]
        driver.chromium.launch_persistent_context.return_value = context
        return session, driver, context

    def start(self, session, driver, present):
        from playwright import sync_api
        controller = MagicMock()
        controller.start.return_value = driver
        with patch.object(sync_api, 'sync_playwright', return_value=controller), \
             patch.object(sys, 'platform', 'darwin'), \
             patch.object(Path, 'is_file', autospec=True,
                          side_effect=lambda path: path in present):
            return session.start()

    def test_fallback_chrome_uses_project_account_profile_and_brings_window_forward(self):
        session, driver, context = self.setup_session()
        self.start(session, driver, {browser_runtime.MAC_CHROME})
        launch = driver.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(launch['channel'], 'chrome')
        self.assertEqual(launch['user_data_dir'], str(session.profile_dir))
        self.assertFalse(launch['headless'])
        context.pages[0].bring_to_front.assert_called_once()

    def test_missing_browser_stops_driver_without_attempting_launch(self):
        session, driver, _ = self.setup_session()
        with self.assertRaisesRegex(RuntimeError, 'Chromium'):
            self.start(session, driver, set())
        driver.chromium.launch_persistent_context.assert_not_called()
        driver.stop.assert_called_once()
        self.assertIsNone(session._pw)

    def test_profile_lock_error_is_actionable_and_does_not_delete_profile(self):
        session, driver, _ = self.setup_session()
        driver.chromium.launch_persistent_context.side_effect = RuntimeError('ProcessSingleton: already in use')
        with patch.object(Path, 'unlink') as delete, self.assertRaisesRegex(RuntimeError, '正在使用'):
            self.start(session, driver, {Path(driver.chromium.executable_path)})
        delete.assert_not_called()
        driver.stop.assert_called_once()

    def test_launch_failure_stops_driver_and_preserves_original_error(self):
        session, driver, _ = self.setup_session()
        driver.chromium.launch_persistent_context.side_effect = RuntimeError('fixture OS launch error')
        with self.assertRaisesRegex(RuntimeError, 'fixture OS launch error'):
            self.start(session, driver, {Path(driver.chromium.executable_path)})
        driver.stop.assert_called_once()
        self.assertIsNone(session.context)

    def test_initialization_error_closes_created_context_and_driver(self):
        session, driver, context = self.setup_session()
        context.set_default_timeout.side_effect = RuntimeError('fixture initialization error')
        with self.assertRaisesRegex(RuntimeError, 'fixture initialization error'):
            self.start(session, driver, {Path(driver.chromium.executable_path)})
        context.close.assert_called_once()
        driver.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
