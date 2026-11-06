"""Intercepted DOM fixtures for recovering stale Doubao queue pages."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from doubao_automation import doubao_batch
from doubao_automation.paths import use_workspace_temp
from tests import test_doubao_asset_ownership as fixtures


ROOT_URL = 'https://www.doubao.com/chat/'
CHAT_PATH = '/chat/12340000000021'
CHAT_URL = 'https://www.doubao.com' + CHAT_PATH
VIDEO_URL = 'https://fixture.invalid/first.mp4'
QUEUE_REPLY = '正在排队，预计等待10分钟。'
CONFIRM_REPLY = '确认后我再开始生成视频。'


class StaleQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_workspace_temp()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='doubao-stale-queue-')
        self.addCleanup(self.temp.cleanup)
        self.page = self.browser.new_page(service_workers='block')
        self.addCleanup(self.page.close)
        self.page.route('**/*', self._fulfill_fixture)
        self._reset_page()

    def _reset_page(self):
        self.page.goto(ROOT_URL)
        self.navigation_urls = []
        self.navigation_times = []
        self.elapsed = 0.0
        self.reopened_document = lambda: ''
        self.agent = fixtures.FixtureAgent(self.page)
        self.agent.dcfg.update(generation_timeout_minutes=.3,
                               ownership_ack_timeout_seconds=10, poll_interval_seconds=2)
        self.agent._approval_dialog_kind = lambda: None
        self.agent._visible_dialogs = lambda: []

    def _fulfill_fixture(self, route):
        if not route.request.is_navigation_request():
            route.abort()
            return
        url = route.request.url
        if url == ROOT_URL:
            markup = ''
        else:
            self.navigation_urls.append(url)
            self.navigation_times.append(self.elapsed)
            if url != CHAT_URL:
                route.abort()
                return
            markup = self.reopened_document()
        route.fulfill(content_type='text/html; charset=utf-8', body=f'<body>{markup}</body>')

    def _route_to(self, path):
        self.page.evaluate("path => history.pushState({}, '', path)", path)

    def _append(self, markup):
        self.page.locator('body').evaluate('(node, markup) => node.insertAdjacentHTML("beforeend", markup)', markup)

    def _accept_turn(self, prompt, identity, reply):
        if not self.agent.sent:
            self._route_to(CHAT_PATH)
        self.agent.sent.append(prompt)
        self._append(fixtures.user(identity, prompt) + reply)

    def _run(self, *, segments=1, **context):
        tasks = [dict(id=f'fixture#{i}', seg_index=i, seg_total=segments, target_seconds=15,
                      est_seconds=15, start_seconds=i * 15, end_seconds=(i + 1) * 15,
                      prompt=f'FIXTURE SCENE {i}') for i in range(segments)]
        self.dests = {task['id']: Path(self.temp.name) / f'{i}.mp4' for i, task in enumerate(tasks)}
        self.generated = []
        self.submitted = []
        def advance_time(milliseconds):
            self.elapsed += milliseconds / 1000
        def record_submission(url, baseline):
            self.submitted.append((url, baseline.get('ownership_acknowledged') is True))
        with (patch.object(doubao_batch, 'time', SimpleNamespace(monotonic=lambda: self.elapsed)),
              patch.object(doubao_batch, '_CHAT_RECHECK_IDLE_SECONDS', 4),
              patch.object(self.page, 'wait_for_timeout', advance_time)):
            return self.agent.run_batch(tasks, [], self.dests,
                {'request_id': 'stale-queue-fixture', 'total_seconds': segments * 15, **context},
                record_submission, lambda tid, ref: self.generated.append((tid, ref['card_message_id'])))

    def test_reopens_stale_queue_and_waits_for_confirmation_turn_to_reappear(self):
        def send(prompt):
            if not self.agent.sent:
                self._accept_turn(prompt, 'original-user',
                    fixtures.assistant('confirmation-request', card=False, text=CONFIRM_REPLY))
            elif len(self.agent.sent) == 1:
                self._accept_turn(prompt, 'accepted-confirmation',
                    fixtures.assistant('stale-queue', card=False, text=QUEUE_REPLY))
            else:
                self.fail('Refreshing the page must not resend a submitted turn')
        self.agent.type_and_send = send
        self.reopened_document = lambda: (
            fixtures.user('original-user', self.agent.sent[0]) +
            fixtures.assistant('old-confirmation-hydrating', card=False, text=CONFIRM_REPLY))
        inspect = self.agent._batch_assets
        observed_reopened_reply_ids = []
        def inspect_while_history_hydrates():
            assets = inspect()
            if self.navigation_urls and not observed_reopened_reply_ids:
                # The stale assistant turn appears before its already submitted
                # confirmation and the current card hydrate on the reopened page.
                observed_reopened_reply_ids.append(self.agent._batch_scope_state.get('latest_reply_message_id'))
                self._append(fixtures.user('accepted-confirmation', self.agent.sent[1]) +
                             fixtures.assistant('owned-video', video=VIDEO_URL))
            return assets
        self.agent._batch_assets = inspect_while_history_hydrates

        result = self._run()

        self.assertEqual(self.navigation_urls, [CHAT_URL])
        self.assertEqual(observed_reopened_reply_ids, ['old-confirmation-hydrating'])
        self.assertEqual(len(self.agent.sent), 2)
        self.assertTrue(self.agent.sent[1].startswith('确认，立即开始生成'))
        self.assertEqual(self.generated, [('fixture#0', 'owned-video')])
        self.assertFalse(result['unresolved'], result['error'])
        self.assertEqual(self.dests['fixture#0'].read_bytes(), b'x' * 2048)
        self.agent.session.context.request.get.assert_called_once_with(VIDEO_URL, timeout=90000)

    def test_refresh_keeps_legitimate_next_segment_control(self):
        def send(prompt):
            if not self.agent.sent:
                self._accept_turn(prompt, 'original-user',
                    fixtures.assistant('single-limit', card=False, text='一次只能生成一条视频。'))
            elif len(self.agent.sent) == 1:
                self._accept_turn(prompt, 'accepted-first',
                    fixtures.assistant('stale-queue', card=False, text=QUEUE_REPLY))
            elif len(self.agent.sent) == 2:
                self._accept_turn(prompt, 'accepted-second',
                    fixtures.assistant('second-card', video='https://fixture.invalid/second.mp4'))
            else:
                self.fail('A segment was submitted twice')
        self.agent.type_and_send = send
        self.reopened_document = lambda: (
            fixtures.user('original-user', self.agent.sent[0]) +
            fixtures.user('accepted-first', self.agent.sent[1]) +
            fixtures.assistant('first-card', video=VIDEO_URL))

        result = self._run(segments=2)

        self.assertEqual(self.navigation_urls, [CHAT_URL])
        self.assertEqual(self.generated, [('fixture#0', 'first-card'), ('fixture#1', 'second-card')])
        self.assertFalse(result['unresolved'], result['error'])
        self.assertEqual(len(self.agent.sent), 3)
        self.assertIn('现在仅生成下一条独立视频', self.agent.sent[-1])

    def test_does_not_reopen_unconfirmed_or_operator_changed_chat(self):
        for scenario in ('marker_only', 'different_route', 'typed_draft', 'later_user_turn', 'approval'):
            with self.subTest(scenario=scenario):
                self._reset_page()
                def send(prompt):
                    own_text = self.agent._batch_ownership['marker'] if scenario == 'marker_only' else prompt
                    self._route_to(CHAT_PATH)
                    self.agent.sent.append(prompt)
                    self._append(fixtures.user('original-user', own_text) +
                                 fixtures.assistant('stale-queue', card=False, text=QUEUE_REPLY))
                    if scenario == 'typed_draft':
                        self.agent.sel['input_box'] = '#draft'
                        self._append('<textarea id="draft">手工输入尚未发送</textarea>')
                    elif scenario == 'later_user_turn':
                        self._append(fixtures.user('another-user', 'Unrelated user message') +
                                     fixtures.assistant('unrelated-card', video=VIDEO_URL))
                    elif scenario == 'approval':
                        self.agent._approval_dialog_kind = lambda: 'material_authorization'
                self.agent.type_and_send = send
                if scenario == 'different_route':
                    read_reply = self.agent._assistant_text
                    def switch_route_at_recheck():
                        reply = read_reply()
                        if self.elapsed >= 4 and self.page.url == CHAT_URL:
                            self._route_to('/chat/12340000000022')
                        return reply
                    self.agent._assistant_text = switch_route_at_recheck

                result = self._run()

                self.assertEqual(self.navigation_urls, [])
                self.assertTrue(result['submitted'])
                self.assertEqual(len(self.agent.sent), 1)
                self.assertEqual(any(acknowledged for _, acknowledged in self.submitted),
                                 scenario != 'marker_only')
                self.assertEqual(result['unresolved'], ['fixture#0'])
                self.assertFalse(self.generated)
                self.agent.session.context.request.get.assert_not_called()
                if scenario == 'typed_draft':
                    self.assertEqual(self.page.locator('#draft').input_value(), '手工输入尚未发送')
                elif scenario == 'different_route':
                    self.assertTrue(self.page.url.endswith('/chat/12340000000022'))

    def test_reopen_attempts_back_off_and_keep_original_deadline(self):
        self.agent.dcfg['generation_timeout_minutes'] = 1.1
        queue = fixtures.assistant('stale-queue', card=False, text=QUEUE_REPLY)
        self.agent.type_and_send = lambda prompt: self._accept_turn(prompt, 'original-user', queue)
        self.reopened_document = lambda: fixtures.user('original-user', self.agent.sent[0]) + queue

        result = self._run()

        self.assertEqual(self.navigation_urls, [CHAT_URL] * 3)
        self.assertEqual(self.navigation_times, [4, 12, 28])
        self.assertEqual(self.elapsed, 66)
        self.assertEqual(result['error_kind'], 'timeout')
        self.assertEqual(len(self.agent.sent), 1)
        self.assertFalse(self.generated)
        self.agent.session.context.request.get.assert_not_called()

    def test_short_resume_uses_one_navigation_and_caps_polling_time(self):
        for requested_seconds, expected_seconds in ((8, 8), (30, 18)):
            with self.subTest(requested_seconds=requested_seconds):
                self._reset_page()
                self.reopened_document = lambda: (
                    fixtures.user('original-user', 'ORIGINAL SCENE') +
                    fixtures.assistant('stale-queue', card=False, text=QUEUE_REPLY))

                result = self._run(resume_only=True, chat_url=CHAT_URL,
                    recovery_timeout_seconds=requested_seconds,
                    baseline={'ownership': {'prompt': 'ORIGINAL SCENE', 'user_message_id': 'original-user'}})

                self.assertEqual(self.navigation_urls, [CHAT_URL])
                self.assertEqual(self.elapsed, expected_seconds)
                self.assertEqual(result['error_kind'], 'timeout')
                self.assertFalse(self.agent.sent)
                self.assertFalse(self.generated)
                self.agent.session.context.request.get.assert_not_called()

    def test_short_resume_pauses_immediately_for_an_open_dialog(self):
        for roleless in (False, True):
            with self.subTest(roleless=roleless):
                self._reset_page()
                role = '' if roleless else ' role="dialog"'
                self.reopened_document = lambda: (
                    fixtures.user('original-user', 'ORIGINAL SCENE') +
                    fixtures.assistant('owned-card', video=VIDEO_URL) +
                    f'<div id="pending-approval"{role}>安全确认</div>')
                if roleless:
                    self.agent._approval_dialog_kind = lambda: 'material_authorization'
                else:
                    self.agent._visible_dialogs = lambda: self.page.get_by_role('dialog').all()
                self.agent._handle_dialogs = Mock(side_effect=AssertionError('Short recovery attempted dialog handling'))

                result = self._run(resume_only=True, chat_url=CHAT_URL, recovery_timeout_seconds=8,
                    baseline={'ownership': {'prompt': 'ORIGINAL SCENE', 'user_message_id': 'original-user'}})

                self.assertEqual(result['error_kind'], 'needs_user')
                self.assertEqual(self.navigation_urls, [CHAT_URL])
                self.assertEqual(self.elapsed, 0)
                self.assertTrue(self.page.locator('#pending-approval').is_visible())
                self.assertFalse(self.agent.sent)
                self.assertFalse(self.generated)
                self.agent._handle_dialogs.assert_not_called()
                self.agent.session.context.request.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
