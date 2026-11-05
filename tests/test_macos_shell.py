"""Platform checks use mocks and never launch Finder or modify user files."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

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
        from doubao_automation import config
        settings = {'accounts': {}}
        with tempfile.TemporaryDirectory(prefix='configured-data-') as directory:
            root = Path(directory)
            source = root / 'checkout'
            source.mkdir()
            destination = root / 'private data/config/config.yaml'
            with patch.object(dashboard, 'DEFAULT_CONFIG_PATH', destination), \
                 patch.object(dashboard, 'ROOT', source), \
                 patch.object(config, 'ROOT', source):
                dashboard._savecfg(settings)
            self.assertEqual(yaml.safe_load(destination.read_text()), settings)
            self.assertFalse((source / 'config/config.yaml').exists())
            self.assertEqual(list(destination.parent.glob('.config-*.yaml.tmp')), [])
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

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

    def test_builtin_login_probe_rejects_missing_session_with_empty_rules(self):
        import threading
        import dashboard
        stop = threading.Event()
        job = {'stop': stop, 'ready': threading.Event(), 'finished': threading.Event()}
        page = Mock()
        page.url = 'https://www.doubao.com/chat/'
        page.is_closed.return_value = False
        empty = Mock()
        empty.count.return_value = 0
        page.get_by_role.return_value = empty
        page.locator.return_value = empty
        page.wait_for_timeout.side_effect = lambda _: stop.set()
        session = SimpleNamespace(context=SimpleNamespace(pages=[page]), page=page,
                                  cookies=Mock(return_value=[]), stop=Mock())
        with patch.object(dashboard, 'load_config', return_value={'login': {}}), \
             patch.object(dashboard, 'use_workspace_temp'), \
             patch.object(dashboard, 'account_status') as account_status, \
             patch.object(dashboard, '_savecfg') as save_config, \
             patch.object(dashboard, 'probe_doubao_login',
                          wraps=dashboard.probe_doubao_login) as login_probe, \
             patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.return_value = session
            dashboard.login_worker('account', 'profiles/account', job)
        login_probe.assert_called_once()
        self.assertIs(login_probe.call_args.args[0], session)
        self.assertEqual(login_probe.call_args.args[1]['login'], {})
        self.assertFalse(job.get('verified', False))
        self.assertEqual(job['status'], 'needs_login')
        save_config.assert_not_called()
        self.assertTrue(all(call.args[1] != 'available' for call in account_status.call_args_list))
        session.cookies.assert_called_once_with(page.url)
        session.stop.assert_called_once()

if __name__ == '__main__':
    unittest.main()
