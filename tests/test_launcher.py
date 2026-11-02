"""启动器复用已确认的后台，不强制终止旧进程。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import launch_dashboard as launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.state={'server_pid':12345,'project_root':str(launcher.ROOT),'data_root':str(launcher.DATA_ROOT),'service_pid':None,
                    'running':False,'login_windows_open':False}

    def check_restart(self,state):
        with patch.object(launcher,'server_state',return_value=None), \
             patch.object(launcher,'record'), patch.object(launcher.subprocess,'run') as stop:
            launcher.migrate_legacy_server(state)
            return stop.call_args

    def test_legacy_idle_server_is_not_terminated(self):
        with patch.object(launcher, 'record'), \
             patch.object(launcher.subprocess, 'run') as stop, \
             patch.object(launcher.os, 'kill') as signal:
            launcher.migrate_legacy_server(self.state)
        stop.assert_not_called()
        signal.assert_not_called()

    def test_start_reuses_owned_legacy_server_without_stopping_it(self):
        with patch.object(launcher, 'LOGS_DIR'), \
             patch.object(launcher, 'checked_state', return_value=self.state), \
             patch.object(launcher, 'healthy', return_value=True), \
             patch.object(launcher, 'open_browser', return_value=True) as browser, \
             patch.object(launcher, 'record'), \
             patch.object(launcher.subprocess, 'run') as stop, \
             patch.object(launcher.subprocess, 'Popen') as start, \
             patch.object(launcher.os, 'kill') as signal:
            launcher.main()
        browser.assert_called_once()
        stop.assert_not_called()
        start.assert_not_called()
        signal.assert_not_called()

    def test_non_windows_legacy_server_is_not_terminated(self):
        with patch.object(launcher, 'belongs_to_project', return_value=True), \
             patch.object(launcher.os, 'name', 'posix'), \
             patch.object(launcher, 'record'), \
             patch.object(launcher.subprocess, 'run') as stop:
            launcher.migrate_legacy_server(self.state)
        stop.assert_not_called()

    def test_busy_or_login_server_is_preserved(self):
        for key in ('running','login_windows_open','postprocessing'):
            self.assertIsNone(self.check_restart({**self.state,key:True}))

    def test_other_project_or_missing_identity_is_preserved(self):
        for value in ('',str(launcher.ROOT.parent/'other-project')):
            self.assertIsNone(self.check_restart({**self.state,'project_root':value}))

    def test_normal_server_is_reused(self):
        self.assertIsNone(self.check_restart({**self.state,'service_pid':67890}))


class LauncherIdentityTests(unittest.TestCase):
    def test_rejects_other_project_without_stopping_any_process(self):
        state = {'server_pid': 12345, 'project_root': str(launcher.ROOT.parent / 'other-project'),
                 'data_root': str(launcher.DATA_ROOT), 'service_pid': None}
        with patch.object(launcher, 'server_state', return_value=state), \
             patch.object(launcher.subprocess, 'run') as stop:
            with self.assertRaisesRegex(RuntimeError, '其他项目'):
                launcher.checked_state()
        stop.assert_not_called()

    def test_rejects_other_data_root_even_for_same_project(self):
        state = {'server_pid': 12345, 'project_root': str(launcher.ROOT),
                 'data_root': str(launcher.DATA_ROOT.parent / 'other-data')}
        with patch.object(launcher, 'server_state', return_value=state):
            with self.assertRaisesRegex(RuntimeError, '数据目录'):
                launcher.healthy()

    def test_own_project_and_data_root_are_accepted(self):
        state = {'server_pid': 12345, 'project_root': str(launcher.ROOT),
                 'data_root': str(launcher.DATA_ROOT)}
        with patch.object(launcher, 'server_state', return_value=state):
            self.assertTrue(launcher.healthy())

    def test_missing_identity_never_qualifies_as_owned(self):
        self.assertFalse(launcher.belongs_to_project({'server_pid': 12345}))

if __name__=='__main__': unittest.main(verbosity=2)
