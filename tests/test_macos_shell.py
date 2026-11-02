"""Platform checks use mocks and never launch Finder or modify user files."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doubao_automation import windows_shell


class MacOSFolderTests(unittest.TestCase):
    def test_finder_receives_valid_video_without_a_shell(self):
        video = windows_shell.VIDEOS_DIR / 'video.mp4'
        with patch.object(Path, 'is_file', return_value=True), \
             patch.object(windows_shell.sys, 'platform', 'darwin'), \
             patch.object(windows_shell.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=0, stderr='')) as call:
            windows_shell.open_video_folder(video)
        self.assertEqual(call.call_args.args[0], ['/usr/bin/open', '-R', str(video.resolve())])
        self.assertNotIn('shell', call.call_args.kwargs)

    def test_failed_finder_request_is_not_reported_as_success(self):
        video = windows_shell.VIDEOS_DIR / 'video.mp4'
        with patch.object(Path, 'is_file', return_value=True), \
             patch.object(windows_shell.sys, 'platform', 'darwin'), \
             patch.object(windows_shell.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=1, stderr='denied')):
            with self.assertRaisesRegex(OSError, 'denied'):
                windows_shell.open_video_folder(video)

    def test_file_outside_video_directory_is_rejected(self):
        video = windows_shell.VIDEOS_DIR.parent / 'outside.mp4'
        with patch.object(Path, 'is_file', return_value=True), \
             patch.object(windows_shell.sys, 'platform', 'darwin'), \
             patch.object(windows_shell.subprocess, 'run') as call:
            with self.assertRaises(FileNotFoundError):
                windows_shell.open_video_folder(video)
        call.assert_not_called()


class DashboardPlatformTests(unittest.TestCase):
    def test_save_config_uses_configured_data_location(self):
        import dashboard
        destination = Path('Z:/example/config/config.yaml')
        with patch.object(dashboard, 'DEFAULT_CONFIG_PATH', destination), \
             patch.object(Path, 'mkdir') as mkdir, \
             patch.object(Path, 'write_text') as write, \
             patch.object(dashboard.os, 'replace') as replace:
            dashboard._savecfg({'accounts': {}})
        self.assertTrue(mkdir.called)
        self.assertTrue(write.called)
        self.assertEqual(replace.call_args.args[1], destination)

    def test_finder_ui_reports_request_not_window_confirmation(self):
        import dashboard
        with dashboard.app.test_client() as client, \
             patch.object(dashboard, 'video_path', return_value=Path('Z:/video.mp4')), \
             patch.object(dashboard, 'open_video_folder'), \
             patch.object(dashboard.sys, 'platform', 'darwin'):
            client.environ_base.update({'HTTP_ORIGIN':'http://localhost','HTTP_X_CSRF_TOKEN':dashboard.CSRF_TOKEN})
            result = client.post('/api/open-video', json={'path': 'ignored'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('请求', result.json['message'])
        self.assertIn('确认', result.json['message'])

    def test_missing_cookie_detection_rules_never_reports_verified(self):
        import threading
        import dashboard
        stop = threading.Event(); stop.set()
        job = {'stop': stop, 'ready': threading.Event(), 'finished': threading.Event()}
        with patch.object(dashboard, 'load_config', return_value={'login': {}}), \
             patch.object(dashboard, 'use_workspace_temp'), \
             patch.object(dashboard, 'account_status'), \
             patch('doubao_automation.browser_pool.BrowserSession') as browser:
            dashboard.login_worker('account', 'profiles/account', job)
        self.assertFalse(job.get('verified', False))
        self.assertIn('登录检测规则未配置', job['note'])
        browser.return_value.start.return_value.stop.assert_called_once()

if __name__ == '__main__':
    unittest.main()
