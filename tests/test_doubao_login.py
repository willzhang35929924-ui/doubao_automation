"""Login probes use synthetic cookie values and disposable account state."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dashboard
import run_login
from doubao_automation.browser_pool import BrowserSession
from doubao_automation.doubao_login import probe_doubao_login
from doubao_automation.state import JsonStore


class Elements:
    def __init__(self, visible=()):
        self.visible = visible

    def count(self):
        return len(self.visible)

    def nth(self, index):
        return SimpleNamespace(is_visible=lambda: self.visible[index])


def page_fixture(url='https://www.doubao.com/chat/', editor=(True,), login=False, form=False):
    page = Mock()
    page.url = url
    page.is_closed.return_value = False
    page.get_by_role.return_value = Elements((True,) if login else ())
    page.locator.side_effect = lambda selector: Elements((True,) if form else ()) if selector.startswith('input[type="tel"]') else Elements(editor)
    return page


def session_fixture(cookies=None, pages=None):
    pages = pages or [page_fixture()]
    return SimpleNamespace(context=SimpleNamespace(pages=pages), page=pages[0],
                           cookies=Mock(return_value=cookies if cookies is not None else [
                               dict(name='sessionid', value='fixture-only', expires=-1)]),
                           stop=Mock())


class DoubaoLoginProbeTests(unittest.TestCase):
    def test_empty_missing_or_legacy_rules_use_builtin_session_names(self):
        for cfg in ({}, {'login': {}}, {'login': {'doubao_logged_in_cookies': []}},
                    {'login': {'doubao_logged_in_cookies': ['uid_tt']}}):
            with self.subTest(cfg=cfg):
                self.assertTrue(probe_doubao_login(session_fixture(), cfg)['logged_in'])

    def test_known_session_cookie_variants_are_supported(self):
        for name in ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'sid_ucp_v1', 'ssid_ucp_v1'):
            with self.subTest(name=name):
                session = session_fixture([dict(name=name, value='fixture-only')])
                self.assertTrue(probe_doubao_login(session, {})['logged_in'])

    def test_visitor_cookies_and_guest_editor_do_not_prove_login(self):
        for name in ('uid_tt', 'uid_tt_ss', 'ttwid', 'odin_tt', 'passport_csrf_token'):
            with self.subTest(name=name):
                session = session_fixture([dict(name=name, value='fixture-only')])
                self.assertFalse(probe_doubao_login(session, {'login': {'doubao_logged_in_cookies': [name]}})['logged_in'])
        self.assertFalse(probe_doubao_login(session_fixture([]), {})['logged_in'])

    def test_empty_or_expired_session_cookie_is_rejected(self):
        for cookie in (dict(name='sessionid', value=''),
                       dict(name='sessionid', value='fixture-only', expires=time.time()-10)):
            with self.subTest(cookie=cookie):
                self.assertFalse(probe_doubao_login(session_fixture([cookie]), {})['logged_in'])

    def test_hidden_editor_login_controls_and_untrusted_pages_are_rejected(self):
        pages = [page_fixture(editor=(False,)), page_fixture(editor=()),
                 page_fixture(login=True), page_fixture(form=True),
                 page_fixture(url='https://www.doubao.com/login'),
                 page_fixture(url='https://accounts.doubao.com/passport'),
                 page_fixture(url='https://www.doubao.com.attacker.invalid/chat/')]
        for page in pages:
            with self.subTest(url=page.url):
                self.assertFalse(probe_doubao_login(session_fixture(pages=[page]), {})['logged_in'])

    def test_hidden_first_match_does_not_hide_visible_editor(self):
        page = page_fixture(editor=(False, True))
        self.assertTrue(probe_doubao_login(session_fixture(pages=[page]), {})['logged_in'])

    def test_login_in_another_tab_uses_the_authenticated_chat_page(self):
        login = page_fixture(url='https://accounts.doubao.com/passport')
        chat = page_fixture()
        session = session_fixture(pages=[login, chat])
        self.assertTrue(probe_doubao_login(session, {})['logged_in'])
        self.assertIs(session.page, chat)
        session.cookies.assert_called_once_with(chat.url)

    def test_navigation_failure_is_pending_and_does_not_expose_cookie_values(self):
        page = page_fixture()
        page.locator.side_effect = RuntimeError('navigation in progress')
        result = probe_doubao_login(session_fixture(pages=[page]), {})
        self.assertFalse(result['logged_in'])
        self.assertNotIn('fixture-only', repr(result))

    def test_custom_selector_and_string_cookie_configuration(self):
        session = session_fixture([dict(name='custom_session', value='fixture-only')])
        cfg = {'login': {'doubao_logged_in_cookies': 'custom_session'},
               'doubao': {'selectors': {'input_box': '#custom-editor'}}}
        self.assertTrue(probe_doubao_login(session, cfg)['logged_in'])
        self.assertIn('#custom-editor', session.page.locator.call_args.args[0])

    def test_authoritative_probe_cannot_be_short_circuited_by_visitor_cookie(self):
        session = BrowserSession.__new__(BrowserSession)
        session.page = page_fixture()
        session.has_any_cookie = Mock(return_value=True)
        with patch('doubao_automation.browser_pool.time.time', side_effect=[0, 0, 2]):
            self.assertFalse(session.wait_logged_in('https://www.doubao.com', ['uid_tt'],
                                                   timeout_s=1, login_probe=lambda _: False))
        session.has_any_cookie.assert_not_called()
        session.page.wait_for_timeout.assert_called_once()


class LoginPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doubao-login-state-')
        self.directory = Path(self.tmp.name)
        self.acct = dict(name='fixture', profile='profiles/fixture', enabled=False)
        self.cfg = {'accounts': {'doubao': [copy.deepcopy(self.acct)]}, 'login': {}}
        self.patches = [patch.object(dashboard, 'STATE_DIR', self.directory),
                        patch.object(dashboard, 'load_config', side_effect=lambda: self.cfg),
                        patch.object(dashboard, '_savecfg'),
                        patch.object(dashboard, 'use_workspace_temp')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_dashboard_verifies_and_persists_login_with_empty_rules(self):
        session = session_fixture()
        job = {name: threading.Event() for name in ('stop', 'ready', 'finished')}
        with patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.return_value = session
            dashboard.login_worker('fixture', 'profiles/fixture', job)
        self.assertTrue(job['verified'])
        self.assertTrue(job['finished'].is_set())
        self.assertTrue(self.cfg['accounts']['doubao'][0]['enabled'])
        self.assertEqual(JsonStore(self.directory/'accounts.json').snapshot()['accounts']['fixture']['status'], 'available')
        session.stop.assert_called_once()

    def test_dashboard_retries_loading_page_then_verifies_without_manual_config(self):
        session = session_fixture()
        session.cookies.side_effect = [[], [dict(name='sessionid', value='fixture-only')]]
        job = {name: threading.Event() for name in ('stop', 'ready', 'finished')}
        with patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.return_value = session
            dashboard.login_worker('fixture', 'profiles/fixture', job)
        self.assertTrue(job['verified'])
        session.page.wait_for_timeout.assert_called_once_with(1500)

    def test_cli_login_updates_dashboard_state_and_preserves_other_settings(self):
        self.cfg['unrelated'] = {'keep': 'fixture'}
        state = JsonStore(self.directory/'accounts.json')
        ledger = {'fixture-day': {'entries': {'request': {'state': 'charged'}}}}
        state.reset({'accounts': {'fixture': {'status': 'needs_login', 'quota_days': ledger}}})
        with patch.object(run_login, 'STATE_DIR', self.directory), \
             patch.object(run_login, 'load_config', return_value=self.cfg), \
             patch.object(run_login, 'save_config') as save:
            run_login._save_doubao_login(self.acct)
        self.assertTrue(self.cfg['accounts']['doubao'][0]['enabled'])
        self.assertEqual(self.cfg['unrelated'], {'keep': 'fixture'})
        self.assertEqual(state.snapshot()['accounts']['fixture']['status'], 'available')
        self.assertEqual(state.snapshot()['accounts']['fixture']['quota_days']['fixture-day'], ledger['fixture-day'])
        save.assert_called_once_with(self.cfg)

    def test_cli_rejects_removed_or_replaced_profile(self):
        self.cfg['accounts']['doubao'][0]['profile'] = 'profiles/replaced'
        with patch.object(run_login, 'load_config', return_value=self.cfg), \
             patch.object(run_login, 'save_config') as save:
            with self.assertRaises(ValueError):
                run_login._save_doubao_login(self.acct)
        save.assert_not_called()

    def test_cli_worker_uses_shared_probe_and_persists_before_reporting_success(self):
        session = session_fixture()
        stop = threading.Event()
        stop.set()
        result = {}
        with patch.object(run_login, 'BrowserSession', return_value=session), \
             patch.object(run_login, '_save_doubao_login') as save:
            session.start = Mock()
            run_login._worker(self.acct, 'doubao', self.cfg, result, stop)
        self.assertTrue(result['fixture'])
        save.assert_called_once_with(self.acct)
        session.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
