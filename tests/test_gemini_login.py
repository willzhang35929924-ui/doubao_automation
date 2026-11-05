"""Gemini startup must wait for login and readiness, without sending anything."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_login
from doubao_automation import gemini_web, orchestrator
from doubao_automation.gemini_web import GeminiWeb, GeminiNotLoggedIn


class GeminiLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gemini-login-')
        self.addCleanup(self.tmp.cleanup)
        self.now = 0
        self.visible = [True]
        self.page = Mock()
        self.page.url = 'https://gemini.google.com/app'
        self.page.is_closed.return_value = False
        self.page.wait_for_timeout.side_effect = self.advance
        self.boxes = Mock()
        self.boxes.count.side_effect = lambda: len(self.visible)
        self.boxes.nth.side_effect = lambda i: SimpleNamespace(is_visible=lambda: self.visible[i], is_enabled=lambda: True)
        self.page.locator.return_value = self.boxes
        self.session = SimpleNamespace(page=self.page, context=SimpleNamespace(pages=[self.page]),
                                       has_any_cookie=Mock(return_value=True))
        self.cfg = {'gemini': {'audit_dir': self.tmp.name}}
        self.agent = GeminiWeb(self.session, self.cfg)
        clock = patch.object(gemini_web.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def advance(self, milliseconds):
        self.now += milliseconds / 1000

    def test_authenticated_ready_page_does_not_wait(self):
        self.agent.wait_logged_in()
        self.page.wait_for_timeout.assert_not_called()

    def test_guest_editor_or_google_login_page_is_not_ready(self):
        self.session.has_any_cookie.return_value = False
        self.assertFalse(self.agent.login_ready())
        self.session.has_any_cookie.return_value = True
        self.page.url = 'https://accounts.google.com/v3/signin'
        self.assertFalse(self.agent.login_ready())
        self.page.url = 'https://gemini.google.com.attacker.invalid/app'
        self.assertFalse(self.agent.login_ready())

    def test_empty_cookie_rules_use_google_session_defaults(self):
        self.cfg['login'] = {'gemini_logged_in_cookies': []}
        self.assertTrue(self.agent.login_ready())
        self.assertIn('__Secure-1PSID', self.session.has_any_cookie.call_args.args[1])

    def test_wait_keeps_browser_open_until_user_login_completes(self):
        self.session.has_any_cookie.return_value = False
        def sign_in(milliseconds):
            self.advance(milliseconds)
            self.session.has_any_cookie.return_value = True
        self.page.wait_for_timeout.side_effect = sign_in
        with redirect_stdout(io.StringIO()) as output:
            self.agent.wait_logged_in()
        self.page.wait_for_timeout.assert_called_once_with(1000)
        self.assertIn('窗口会保留', output.getvalue())
        self.assertIn('继续处理素材', output.getvalue())
        self.page.close.assert_not_called()

    def test_cookie_before_editor_loading_does_not_cause_immediate_failure(self):
        self.visible = [False]
        def load(milliseconds):
            self.advance(milliseconds)
            self.visible = [True]
        self.page.wait_for_timeout.side_effect = load
        with redirect_stdout(io.StringIO()):
            self.agent.wait_logged_in()
        self.page.wait_for_timeout.assert_called_once()

    def test_hidden_first_editor_does_not_hide_visible_second_editor(self):
        self.visible = [False, True]
        self.assertTrue(self.agent.login_ready())

    def test_popup_login_switches_back_to_ready_gemini_page(self):
        other = Mock()
        other.url = 'https://accounts.google.com/signin'
        other.is_closed.return_value = False
        self.session.context.pages = [other, self.page]
        self.session.page = other
        self.assertTrue(self.agent.login_ready())
        self.assertIs(self.session.page, self.page)

    def test_headless_has_bounded_wait_and_actionable_message(self):
        self.cfg['browser'] = {'headless': True}
        self.cfg['gemini']['startup_timeout_seconds'] = 2
        self.session.has_any_cookie.return_value = False
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(GeminiNotLoggedIn, '有界面模式'):
                self.agent.wait_logged_in()
        self.assertEqual(self.now, 2)
        self.page.close.assert_not_called()

    def test_closing_login_window_ends_wait_without_submitting(self):
        self.page.is_closed.return_value = True
        with self.assertRaisesRegex(GeminiNotLoggedIn, '窗口已关闭'):
            self.agent.wait_logged_in()
        self.page.wait_for_timeout.assert_not_called()

    def test_pipeline_waits_before_generation_and_closes_only_after_wait_exits(self):
        with patch.object(orchestrator, 'STATE_DIR', Path(self.tmp.name)), \
             patch.object(orchestrator, 'ensure_dirs'), \
             patch.object(orchestrator, 'BrowserSession') as browser, \
             patch.object(orchestrator, 'GeminiWeb') as gemini:
            pipeline = orchestrator.Pipeline({})
            pipeline.store.reset({'sources': [dict(record_id='fixture')], 'fixed_prompt': 'fixture'})
            session = browser.return_value
            agent = gemini.return_value
            def stop_waiting():
                session.stop.assert_not_called()
                agent.generate.assert_not_called()
                raise GeminiNotLoggedIn('test user closed login window')
            agent.wait_logged_in.side_effect = stop_waiting
            with self.assertRaises(GeminiNotLoggedIn):
                pipeline.stage_scripts()
            agent.wait_logged_in.assert_called_once_with()
            agent.generate.assert_not_called()
            session.stop.assert_called_once_with()

    def test_pipeline_does_not_retry_an_uncertain_or_accepted_submission(self):
        with patch.object(orchestrator, 'STATE_DIR', Path(self.tmp.name)), \
             patch.object(orchestrator, 'ensure_dirs'), \
             patch.object(orchestrator, 'BrowserSession') as browser, \
             patch.object(orchestrator, 'GeminiWeb') as gemini, \
             patch.object(orchestrator.time, 'sleep') as sleep:
            pipeline = orchestrator.Pipeline({'gemini': {'retry_on_error': 2}})
            pipeline.store.reset({'sources': [dict(record_id='fixture')], 'fixed_prompt': 'fixture',
                                  'requests': {'existing': {'status': 'unknown'}}})
            before = pipeline.store.snapshot()
            gemini.return_value.generate.side_effect = orchestrator.GeminiSubmissionUncertain('fixture send attempted')
            with self.assertRaises(orchestrator.GeminiSubmissionUncertain):
                pipeline.stage_scripts()
            gemini.return_value.generate.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(pipeline.store.snapshot(), before)
            browser.return_value.stop.assert_called_once()

    def test_cli_uses_default_gemini_profile_when_account_config_is_empty(self):
        for configured in ({}, {'name': 'custom', 'profile': 'profiles/custom'}):
            with self.subTest(configured=configured):
                captured = []
                def make_thread(target, args, **kwargs):
                    captured.append(args)
                    return SimpleNamespace(start=lambda: args[3].update({args[0]['name']: True}), join=lambda **_: None)
                with patch.object(sys, 'argv', ['run_login.py', '--only', 'gemini']), \
                     patch.object(run_login, 'load_config', return_value={'accounts': {'gemini': configured}}), \
                     patch.object(run_login, 'ensure_dirs'), \
                     patch.object(run_login, 'use_workspace_temp'), \
                     patch.object(run_login.threading, 'Thread', side_effect=make_thread), \
                     patch.object(run_login.time, 'sleep'), redirect_stdout(io.StringIO()):
                    self.assertEqual(run_login.main(), 0)
                self.assertEqual(captured[0][0]['name'], configured.get('name', 'gemini'))
                self.assertEqual(captured[0][0]['profile'], configured.get('profile', 'profiles/gemini'))


if __name__ == '__main__':
    unittest.main()
