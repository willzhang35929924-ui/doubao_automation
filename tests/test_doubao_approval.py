"""Local DOM fixtures for human-only approvals; no accounts or remote requests."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

from doubao_automation.doubao_agent import DoubaoAgent, NeedsUserAction
from doubao_automation.paths import use_workspace_temp


NOTICE = ('<span>安全确认</span><p>你在本功能中上传、使用的素材，均已获充分授权，'
          '无侵权违法风险。违反《豆包使用规范》，相关责任需由你自行承担。</p>'
          '<button onclick="window.cancelled=true">取消</button>'
          '<button onclick="window.approved=true">确认</button>')


class ApprovalTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory(prefix='doubao-approval-')
        self.addCleanup(self.temp.cleanup)
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body></body>')
                        if route.request.is_navigation_request() else route.abort())
        self.page.goto('https://www.doubao.com/chat/')
        self.addCleanup(self.page.close)
        self.session = SimpleNamespace(page=self.page, context=SimpleNamespace(request=Mock()))
        self.agent = DoubaoAgent(self.session, {'doubao': {
            'selectors': {'input_box': '#editor', 'send_button': ['#send']},
            'user_approval_timeout_seconds': 0,
            'generation_timeout_minutes': .01,
            'poll_interval_seconds': .01,
        }}, 'approval-fixture')

    def fixture(self, modal='', script=''):
        self.page.set_content('<textarea id="editor"></textarea>' + modal +
                              '<script>document.addEventListener("keydown",e=>{'
                              'window.keys=(window.keys||[]).concat(e.key)});' + script + '</script>')

    def assert_no_approval_actions(self):
        self.assertFalse(self.page.evaluate('Boolean(window.approved || window.cancelled)'))
        self.assertFalse(self.page.evaluate('Boolean((window.keys||[]).length)'))

    def test_visible_material_dialog_without_data_state_is_never_clicked_or_escaped(self):
        self.fixture('<div role="dialog">' + NOTICE + '</div>')
        with self.assertRaises(NeedsUserAction) as caught:
            self.agent._handle_dialogs()
        self.assertEqual(caught.exception.approval_kind, 'material_authorization')
        self.assertTrue(self.page.get_by_role('dialog').is_visible())
        self.assert_no_approval_actions()

    def test_material_popup_without_role_uses_exact_heading_and_small_container(self):
        self.fixture('<section id="popup">' + NOTICE + '</section>')
        with self.assertRaises(NeedsUserAction):
            self.agent._handle_dialogs()
        self.assert_no_approval_actions()

    def test_hidden_old_dialog_and_quoted_chat_notice_do_not_block(self):
        self.fixture('<div role="dialog" hidden>' + NOTICE + '</div>'
                     '<div data-role="user">' + NOTICE + '</div>')
        self.agent._handle_dialogs()
        self.assert_no_approval_actions()

    def test_platform_terms_alertdialog_requires_user_action(self):
        self.fixture('<div role="alertdialog"><p>请阅读并同意新的服务条款</p>'
                     '<button onclick="window.approved=true">同意</button></div>')
        with self.assertRaises(NeedsUserAction) as caught:
            self.agent.send()
        self.assertEqual(caught.exception.approval_kind, 'platform_terms')
        self.assert_no_approval_actions()

    def test_invalid_timeout_does_not_let_send_fall_through_approval(self):
        self.agent.dcfg['user_approval_timeout_seconds'] = 'invalid'
        self.fixture('<div role="dialog">' + NOTICE + '</div>')
        with self.assertRaises(NeedsUserAction):
            self.agent.send()
        self.assert_no_approval_actions()

    def test_initial_dialog_read_failure_stops_before_any_send_action(self):
        self.fixture('<div role="dialog">' + NOTICE + '</div>')
        self.agent._approval_dialog_kind = Mock(side_effect=RuntimeError('fixture detached DOM'))
        with self.assertRaises(NeedsUserAction) as caught:
            self.agent.send()
        self.assertEqual(caught.exception.approval_kind, 'unresolved_dialog')
        self.assertEqual(self.agent._approval_dialog_kind.call_count, 3)
        self.assert_no_approval_actions()

    def test_dialog_hydrating_between_observations_is_not_dismissed(self):
        self.fixture('<div role="dialog">' + NOTICE + '</div>')
        self.agent._approval_dialog_kind = Mock(side_effect=[None, 'material_authorization'])
        with self.assertRaises(NeedsUserAction):
            self.agent.send()
        self.assert_no_approval_actions()

    def test_human_dismissal_can_finish_bounded_wait_without_program_clicking(self):
        self.agent.dcfg['user_approval_timeout_seconds'] = 1
        self.fixture('<div role="dialog" id="approval">' + NOTICE + '</div>',
                     'setTimeout(()=>document.querySelector("#approval").hidden=true,100)')
        self.agent._handle_dialogs()
        self.assert_no_approval_actions()

    def test_send_does_not_press_enter_through_existing_authorization_dialog(self):
        self.fixture('<div aria-modal="true">' + NOTICE + '</div>')
        with self.assertRaises(NeedsUserAction):
            self.agent.send()
        self.assert_no_approval_actions()

    def test_send_trigger_is_not_logged_as_platform_acceptance(self):
        self.fixture('<button id="send">Send</button>')
        self.agent.type_text = lambda text: self.page.locator('#editor').fill(text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.agent.type_and_send('local fixture prompt')
        self.assertIn('已触发发送', output.getvalue())
        self.assertNotIn('提示词已发送', output.getvalue())
        self.assertNotIn('平台已接受', output.getvalue())

    def test_batch_approval_after_send_returns_strict_unsubmitted_evidence(self):
        self.fixture('<button id="send">Send</button>',
            'document.querySelector("#send").onclick=()=>{'
            'window.sendClicks=(window.sendClicks||0)+1;const popup=document.createElement("div");'
            'popup.setAttribute("role","dialog");popup.innerHTML=' + repr(NOTICE) + ';document.body.append(popup)}')
        self.agent.open_chat = self.agent.new_chat = self.agent.configure_video_mode = lambda: None
        self.agent.upload_images = lambda _: None
        self.agent._assert_logged_in = lambda: None
        self.agent._save_batch_debug = lambda *args: None
        self.agent.type_text = lambda text: self.page.locator('#editor').fill(text)
        task = dict(id='approval#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='LOCAL SCRIPT')
        submitted = []
        result = self.agent.run_batch([task], [], {'approval#0': Path(self.temp.name) / 'unused.mp4'},
            {'request_id': 'local-approval-request', 'total_seconds': 15},
            lambda url, baseline: submitted.append(url), lambda *args: self.fail('No generated asset exists'))
        self.assertEqual(result['error_kind'], 'user_approval')
        self.assertFalse(result['submitted'])
        self.assertEqual(result['failure_phase'], 'before_accept')
        evidence = result['approval_block']
        self.assertTrue(evidence['before_accept_proven'])
        self.assertEqual(evidence['request_id'], 'local-approval-request')
        self.assertEqual(evidence['kind'], 'material_authorization')
        for key in ('dialog_visible', 'draft_matches', 'marker_matches', 'no_user_messages', 'no_cards', 'fresh_route'):
            self.assertTrue(evidence[key], key)
        self.assertEqual(evidence['prompt_sha256'], self.agent._original_anchor_proof(
            self.agent._batch_ownership, '/chat/123456789')['prompt_sha256'])
        self.assertEqual(submitted, [''])
        self.assertEqual(self.page.evaluate('window.sendClicks'), 1)
        self.assert_no_approval_actions()
        self.session.context.request.get.assert_not_called()

    def test_existing_user_message_prevents_before_accept_proof(self):
        self.fixture('<div role="dialog">' + NOTICE + '</div><div data-role="user">accepted turn</div>')
        self.agent._batch_ownership = {'prompt': 'LOCAL SCRIPT', 'marker': 'unique-marker'}
        self.page.locator('#editor').fill('LOCAL SCRIPT unique-marker')
        self.agent._batch_send_started_fresh = self.agent._batch_initial_send_attempted = True
        evidence = self.agent._approval_block_evidence({'request_id': 'local'}, {}, {})
        self.assertFalse(evidence['no_user_messages'])
        self.assertFalse(evidence['before_accept_proven'])


if __name__ == '__main__':
    unittest.main()
