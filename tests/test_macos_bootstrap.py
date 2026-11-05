"""First-run checks use temporary paths and mocks; no installers or services run."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import macos_bootstrap as bootstrap
from scripts import app_control


class MacBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mac-bootstrap-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        (self.root / 'config').mkdir()
        (self.root / 'config/config.example.yaml').write_text('accounts: {doubao: []}\n', encoding='utf-8')
        self.log = self.root / 'startup.log'

    def test_default_uses_application_support_and_explicit_overrides_win(self):
        home = Path(self.temp.name) / 'home'
        env = {}
        data, config = bootstrap.configure_data_environment(self.root, env, home)
        self.assertEqual(data, home / 'Library/Application Support/DoubaoAutomation')
        self.assertEqual(config, data / 'config/config.yaml')
        (self.root / 'config/config.yaml').write_text('existing', encoding='utf-8')
        env = {'DOUBAO_DATA_DIR': str(home / 'custom'), 'DOUBAO_CONFIG_PATH': str(home / 'private.yaml')}
        self.assertEqual(bootstrap.configure_data_environment(self.root, env, home),
                         (home / 'custom', home / 'private.yaml'))

    def test_existing_project_config_selects_project_only_without_overrides(self):
        (self.root / 'config/config.yaml').write_text('existing', encoding='utf-8')
        self.assertEqual(bootstrap.configure_data_environment(self.root, {}, self.root / 'home'),
                         (self.root, self.root / 'config/config.yaml'))
        config = self.root / 'separate.yaml'
        data, selected = bootstrap.configure_data_environment(self.root, {'DOUBAO_CONFIG_PATH': str(config)}, self.root / 'home')
        self.assertNotEqual(data, self.root)
        self.assertEqual(selected, config)

    def test_relative_overrides_are_rejected_before_environment_mutation(self):
        for key in ('DOUBAO_DATA_DIR', 'DOUBAO_CONFIG_PATH'):
            env = {key: 'relative/path'}
            with self.assertRaisesRegex(ValueError, '绝对路径'):
                bootstrap.configure_data_environment(self.root, env, self.root / 'home')
            self.assertEqual(env, {key: 'relative/path'})

    def test_private_template_is_600_and_existing_content_is_never_overwritten(self):
        config = self.root / 'private/config.yaml'
        self.assertTrue(bootstrap.ensure_private_config(self.root, config))
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        config.write_text('private fixture setting', encoding='utf-8')
        self.assertFalse(bootstrap.ensure_private_config(self.root, config))
        self.assertEqual(config.read_text(), 'private fixture setting')

    def test_missing_template_cleans_only_its_new_empty_file(self):
        config = self.root / 'private/config.yaml'
        with self.assertRaises(FileNotFoundError):
            bootstrap.ensure_private_config(self.root / 'missing', config)
        self.assertFalse(config.exists())

    def test_existing_virtualenv_reused_and_broken_environment_preserved(self):
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('fixture')
        python.chmod(0o755)
        with patch.object(bootstrap, 'run_logged') as run:
            self.assertEqual(bootstrap.ensure_virtualenv(self.root, self.log), python)
            run.assert_not_called()
            python.unlink()
            with self.assertRaisesRegex(RuntimeError, '保留原目录'):
                bootstrap.ensure_virtualenv(self.root, self.log)
            self.assertTrue(python.parent.exists())

    def test_fresh_virtualenv_creation_is_bounded(self):
        python = self.root / '.venv/bin/python'
        def create(command, log, timeout):
            self.assertEqual(command[-2:], ['venv', str(self.root / '.venv')])
            self.assertEqual(timeout, 120)
            python.parent.mkdir(parents=True)
            python.write_text('fixture')
        with patch.object(bootstrap, 'run_logged', side_effect=create) as run:
            self.assertEqual(bootstrap.ensure_virtualenv(self.root, self.log), python)
            run.assert_called_once()

    def test_requirements_fingerprint_avoids_repeated_installs(self):
        (self.root / '.venv').mkdir()
        req = self.root / 'requirements.txt'
        req.write_text('Flask>=3.0\n')
        python = self.root / '.venv/bin/python'
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(bootstrap, 'run_logged') as run:
            bootstrap.ensure_dependencies(self.root, python, self.log)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[2], 300)
            bootstrap.ensure_dependencies(self.root, python, self.log)
            self.assertEqual(run.call_count, 2)
            req.write_text('Flask>=3.1\n')
            bootstrap.ensure_dependencies(self.root, python, self.log)
            self.assertEqual(run.call_count, 4)

    def test_failed_install_does_not_mark_environment_ready(self):
        (self.root / '.venv').mkdir()
        (self.root / 'requirements.txt').write_text('Flask>=3.0\n')
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(bootstrap, 'run_logged', side_effect=subprocess.TimeoutExpired('pip', 300)):
            with self.assertRaises(subprocess.TimeoutExpired):
                bootstrap.ensure_dependencies(self.root, self.root / '.venv/bin/python', self.log)
        self.assertFalse((self.root / '.venv/.doubao-requirements.sha256').exists())

    def test_missing_ffmpeg_reports_system_dependency_without_installing(self):
        with patch.object(bootstrap.shutil, 'which', return_value=None), \
             patch.object(bootstrap, 'run_logged') as run:
            with self.assertRaisesRegex(RuntimeError, 'brew install ffmpeg'):
                bootstrap.check_ffmpeg()
            run.assert_not_called()

    def test_browser_missing_installs_only_managed_and_rechecks(self):
        unavailable = dict(available=False, install_managed=True, error='missing')
        with patch('doubao_automation.browser_runtime.inspect_browser_runtime',
                   side_effect=[unavailable, dict(available=True)]) as inspect, \
             patch.object(bootstrap, 'run_logged') as run:
            bootstrap.ensure_browser({}, self.log)
            self.assertEqual(inspect.call_count, 2)
            self.assertEqual(run.call_args.args, ([bootstrap.sys.executable, '-m', 'playwright', 'install', 'chromium'], self.log, 300))
        with patch('doubao_automation.browser_runtime.inspect_browser_runtime',
                   return_value=dict(available=True)), patch.object(bootstrap, 'run_logged') as run:
            bootstrap.ensure_browser({}, self.log)
            run.assert_not_called()

    def test_browser_failure_prevents_service_start_and_open(self):
        config = self.root / 'config/config.yaml'
        with patch.object(bootstrap, 'configure_data_environment', return_value=(self.root, config)), \
             patch.object(bootstrap, 'startup_log', return_value=self.log), \
             patch.object(bootstrap, 'ensure_private_config'), \
             patch('doubao_automation.config.load_config', return_value={}), \
             patch.object(bootstrap, 'ensure_browser', side_effect=RuntimeError('browser fixture failure')), \
             patch('launch_dashboard.main') as launch, patch.object(bootstrap, 'run_logged') as run:
            with self.assertRaisesRegex(RuntimeError, 'browser fixture failure'):
                bootstrap.start_dashboard(Mock())
            launch.assert_not_called()
            run.assert_not_called()
            self.assertIn('browser fixture failure', self.log.read_text())

    def test_open_follows_service_identity_and_real_page_checks(self):
        calls = Mock()
        check, browser, launch, verify, run = [Mock() for _ in range(5)]
        for name, call in [('check', check), ('browser', browser), ('launch', launch), ('verify', verify), ('open', run)]:
            calls.attach_mock(call, name)
        with patch.object(bootstrap, 'configure_data_environment', return_value=(self.root, self.root / 'config/config.yaml')), \
             patch.object(bootstrap, 'startup_log', return_value=self.log), \
             patch.object(bootstrap, 'ensure_private_config'), \
             patch('doubao_automation.config.load_config', return_value={}), \
             patch.object(bootstrap, 'ensure_browser', browser), \
             patch('launch_dashboard.main', launch), \
             patch.object(bootstrap, 'verify_dashboard', verify), \
             patch.object(bootstrap, 'run_logged', run):
            bootstrap.start_dashboard(check)
        self.assertEqual([call[0] for call in calls.mock_calls], ['check', 'browser', 'launch', 'verify', 'open'])
        launch.assert_called_once_with(open_page=False)
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/open')

    def test_wrong_service_or_page_content_is_rejected(self):
        launcher = SimpleNamespace(URL='http://127.0.0.1:8782/', checked_state=Mock(return_value=None))
        with self.assertRaisesRegex(RuntimeError, '归属'):
            bootstrap.verify_dashboard(launcher)
        launcher.checked_state.return_value = {'server_pid': 10}
        response = Mock(status=200)
        response.read.return_value = b'not the dashboard'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(bootstrap.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(RuntimeError, '页面检查失败'):
                bootstrap.verify_dashboard(launcher)

    @unittest.skipIf(os.name == 'nt', 'fcntl startup lock is macOS/POSIX only')
    def test_setup_lock_bounds_duplicate_first_run(self):
        with bootstrap.setup_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, '等待超时'):
                with bootstrap.setup_lock(self.root, timeout=0):
                    self.fail('second setup acquired the first setup lock')
        with bootstrap.setup_lock(self.root, timeout=0):
            pass

    def test_macos_control_routes_to_native_bootstrap_and_windows_is_unchanged(self):
        with patch.object(app_control.sys, 'platform', 'darwin'), \
             patch.object(bootstrap, 'configure_data_environment'), \
             patch.object(bootstrap, 'start_dashboard') as start:
            self.assertEqual(app_control.main(['start']), 0)
            start.assert_called_once_with(app_control.check_start_requirements)
        with patch.object(app_control.sys, 'platform', 'win32'), \
             patch.object(app_control, 'check_start_requirements') as check, \
             patch('launch_dashboard.main') as launch:
            self.assertEqual(app_control.main(['start']), 0)
            check.assert_called_once()
            launch.assert_called_once_with()

    def test_status_selects_same_data_root_without_creating_config(self):
        with patch.object(app_control.sys, 'platform', 'darwin'), \
             patch.object(bootstrap, 'configure_data_environment') as configure, \
             patch.object(bootstrap, 'ensure_private_config') as create, \
             patch.object(app_control, 'query_state', return_value=None), \
             patch.object(app_control, 'report_state'):
            self.assertEqual(app_control.main(['status']), 2)
        configure.assert_called_once_with()
        create.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX shell test')
    def test_clean_path_and_space_directory_use_same_entry_without_docker(self):
        source = Path(__file__).resolve().parents[1]
        for name in ('start_mac.command', 'scripts/start_macos.command', 'scripts/start_macos.sh'):
            dest = self.root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, dest)
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "%s\\n" "$@" > "$BOOTSTRAP_ARGS"\n')
        python.chmod(0o755)
        commands = self.root / 'commands'
        commands.mkdir()
        uname = commands / 'uname'
        uname.write_text('#!/bin/sh\nprintf "Darwin\\n"\n')
        uname.chmod(0o755)
        args = self.root / 'arguments.txt'
        env = os.environ.copy()
        env.update(PATH=str(commands) + ':/usr/bin:/bin', BOOTSTRAP_ARGS=str(args))
        result = subprocess.run(['/bin/sh', str(self.root / 'start_mac.command')], cwd=self.temp.name,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args.read_text().splitlines(), ['-u', str(self.root / 'scripts/macos_bootstrap.py')])
        self.assertNotIn('docker', result.stdout.lower() + result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
