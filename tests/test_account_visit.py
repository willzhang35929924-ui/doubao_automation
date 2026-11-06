"""Account visit windows use saved profiles without changing login or quota state."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dashboard


class FakePage:
    def __init__(self):
        self.closed = threading.Event()
        self.focused = threading.Event()
        self.focus_count = 0
        self.navigations = []

    def is_closed(self):
        return self.closed.is_set()

    def bring_to_front(self):
        self.focus_count += 1
        self.focused.set()

    def wait_for_timeout(self, milliseconds):
        self.closed.wait(min(milliseconds / 1000, .02))

    def goto(self, url, **_kwargs):
        self.navigations.append(url)
        return None


class FakeSession:
    def __init__(self, name, profile, cfg, start_url=''):
        self.name, self.profile, self.cfg, self.start_url = name, profile, cfg, start_url
        self.owner = None
        self.stopped_by = None
        self.page = FakePage()
        self.context = Mock(pages=[self.page])

    def start(self):
        self.owner = threading.get_ident()
        return self

    def stop(self):
        self.stopped_by = threading.get_ident()
        self.page.closed.set()


class AccountVisitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='account-visit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / 'profiles'
        self.profile = self.profiles / 'saved'
        self.profile.mkdir(parents=True)
        (self.profile / 'keep.txt').write_text('saved profile', encoding='utf-8')
        self.cfg = {'accounts': {'doubao': [dict(name='saved', profile=str(self.profile), enabled=False)]},
                    'browser': {'headless': True}}
        self.jobs = {}
        self.sessions = []
        self.saved_config = copy.deepcopy(self.cfg)
        self.save = Mock()
        self.account_status = Mock()
        self.factory = Mock(side_effect=self.make_session)
        self.patches = [
            patch.object(dashboard, 'load_config', return_value=self.cfg),
            patch.object(dashboard, '_savecfg', self.save),
            patch.object(dashboard, 'account_status', self.account_status),
            patch.object(dashboard, 'PROFILES', self.profiles),
            patch.object(dashboard, 'STATE_DIR', self.root / 'state'),
            patch.object(dashboard, 'LOGS_DIR', self.root / 'logs'),
            patch.object(dashboard, 'use_workspace_temp'),
            patch.object(dashboard, 'running', return_value=False),
            patch.object(dashboard, 'pending_accounts', self.jobs),
            patch('doubao_automation.browser_pool.BrowserSession', self.factory),
        ]
        for item in self.patches:
            item.start()
        self.client = dashboard.app.test_client()
        self.client.environ_base.update(HTTP_ORIGIN='http://localhost',
                                        HTTP_X_CSRF_TOKEN=dashboard.CSRF_TOKEN)

    def tearDown(self):
        for job in self.jobs.values():
            if isinstance(job.get('stop'), threading.Event):
                job['stop'].set()
            thread = job.get('thread')
            if isinstance(thread, threading.Thread):
                thread.join(timeout=3)
        for item in reversed(self.patches):
            item.stop()

    def make_session(self, name, profile, cfg, start_url=''):
        session = FakeSession(name, profile, cfg, start_url)
        self.sessions.append(session)
        return session

    def visit(self, name='saved'):
        return self.client.post('/api/accounts/visit', json={'name': name})

    def test_visit_opens_saved_visible_profile_and_does_not_change_account(self):
        response = self.visit()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json['status'], 'open')
        self.assertEqual(len(self.sessions), 1)
        session = self.sessions[0]
        self.assertEqual(session.name, 'saved')
        self.assertEqual(Path(session.profile).resolve(), self.profile.resolve())
        self.assertFalse(session.cfg['browser']['headless'])
        self.assertEqual(session.page.navigations, ['https://www.doubao.com/chat/'])
        self.assertNotEqual(session.owner, threading.get_ident())
        self.assertTrue(self.jobs['saved']['thread'].is_alive())
        self.assertEqual(self.jobs['saved']['mode'], 'visit')
        self.assertFalse(self.jobs['saved']['finished'].is_set())
        self.assertEqual(self.cfg, self.saved_config)
        self.save.assert_not_called()
        self.account_status.assert_not_called()
        self.assertEqual((self.profile / 'keep.txt').read_text(), 'saved profile')

        listed = self.client.get('/api/accounts')
        self.assertEqual(listed.status_code, 200)
        account = listed.json['accounts'][0]
        self.assertTrue(account['account_window_open'])
        self.assertFalse(account['login_window_open'])
        self.assertFalse(account['enabled'])

        closed = self.client.post('/api/accounts/close', json={'name': 'saved'})
        self.assertEqual(closed.status_code, 200, closed.get_json())
        self.assertTrue(self.jobs['saved']['finished'].is_set())
        self.assertEqual(session.stopped_by, session.owner)
        self.assertEqual(self.cfg, self.saved_config)
        self.account_status.assert_not_called()

    def test_repeat_visit_focuses_existing_window_without_second_profile_owner(self):
        first = self.visit()
        self.assertEqual(first.status_code, 200, first.get_json())
        session = self.sessions[0]
        second = self.visit()
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(second.json['status'], 'open')
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(session.page.focused.wait(2), 'Existing visit window was not focused')
        self.assertGreaterEqual(session.page.focus_count, 1)
        self.assertTrue(self.jobs['saved']['thread'].is_alive())

    def test_visit_window_blocks_pipeline_start_and_profile_is_not_deleted(self):
        self.assertEqual(self.visit().status_code, 200)
        with patch.object(dashboard.subprocess, 'Popen') as spawn:
            run = self.client.post('/api/run', json={})
        self.assertGreaterEqual(run.status_code, 400)
        spawn.assert_not_called()
        self.assertTrue((self.profile / 'keep.txt').is_file())

    def test_running_login_window_and_missing_profile_reject_visit(self):
        with patch.object(dashboard, 'running', return_value=True):
            response = self.visit()
        self.assertGreaterEqual(response.status_code, 400)
        self.factory.assert_not_called()

        self.jobs['saved'] = {'mode': 'login', 'profile': str(self.profile),
                              'thread': Mock(is_alive=Mock(return_value=True))}
        conflict = self.visit()
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.factory.assert_not_called()
        self.jobs.clear()

        missing = self.profiles / 'missing'
        self.cfg['accounts']['doubao'][0]['profile'] = str(missing)
        response = self.visit()
        self.assertEqual(response.status_code, 409, response.get_json())
        self.factory.assert_not_called()

    def test_other_account_alias_of_active_profile_cannot_open_or_delete_it(self):
        self.assertEqual(self.visit().status_code, 200)
        self.cfg['accounts']['doubao'].append(dict(name='alias', profile=str(self.profile), enabled=True))
        conflict = self.visit('alias')
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(len(self.sessions), 1)
        deleted = self.client.post('/api/accounts/delete', json={'name': 'alias'})
        self.assertGreaterEqual(deleted.status_code, 400)
        self.assertTrue((self.profile / 'keep.txt').is_file())

    def test_visit_reports_slow_start_and_browser_failure_without_claiming_success(self):
        slow = {'ready': Mock(wait=Mock(return_value=False)), 'finished': threading.Event(),
                'status': 'opening'}
        with patch.object(dashboard, 'start_account_window', return_value=slow):
            response = self.visit()
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.json['status'], 'opening')
        self.assertEqual(self.sessions, [])

        failed = {'ready': threading.Event(), 'finished': threading.Event(),
                  'status': 'error', 'error': 'fixture browser unavailable'}
        failed['ready'].set()
        with patch.object(dashboard, 'start_account_window', return_value=failed):
            response = self.visit()
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(response.json['code'], 'browser_start_failed')
        self.assertEqual(self.cfg, self.saved_config)
        self.save.assert_not_called()
        self.account_status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
