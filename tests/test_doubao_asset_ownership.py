"""Local DOM fixtures: no accounts, external uploads, or generation requests."""
import html
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

from doubao_automation.doubao_batch import BatchGenerationMixin, verified_download_matches
from doubao_automation.paths import use_workspace_temp


def user(identity, text):
    return (f'<section data-message-id="{identity}"><div data-testid="send_message" '
            f'data-message-role="user">{html.escape(text)}</div></section>')


def assistant(identity, card=True, text='', video=''):
    contents = (f'<div class="block-video-fixture"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
                + (f'<video src="{video}"></video>' if video else '') + '</div>') if card else ''
    return (f'<section data-message-id="{identity}"><div data-testid="receive_message" '
            f'data-message-role="assistant">{html.escape(text)}{contents}</div></section>')


class FixtureAgent(BatchGenerationMixin):
    def __init__(self, page):
        self.page = page
        self.sel = {}
        self.name = 'local-ownership-fixture'
        self.dcfg = {'generation_timeout_minutes': .05, 'poll_interval_seconds': .02}
        self.quota_patterns = []
        self.failure_patterns = []
        self.sent = []
        self.session = SimpleNamespace(context=SimpleNamespace(request=Mock()))
        self.session.context.request.get.return_value = SimpleNamespace(ok=True, body=lambda: b'x' * 2048)

    def open_chat(self):
        pass

    def new_chat(self):
        pass

    def configure_video_mode(self):
        pass

    def upload_images(self, _):
        pass

    def _assert_logged_in(self):
        pass

    def _handle_dialogs(self):
        pass

    def _save_batch_debug(self, *args):
        pass

    def type_and_send(self, prompt):
        self.sent.append(prompt)
        # The former chat is still visible when send returns. Its video must
        # not be claimed before the actual submitted turn and route mount.
        self.page.evaluate('''markup => setTimeout(() => {
            document.body.insertAdjacentHTML('beforeend', markup);
            history.pushState({}, '', '/chat/12340000000002');
        }, 150)''', user('new-user', prompt) +
            assistant('new-answer', video='https://fixture.invalid/new.mp4'))


class AssetOwnershipTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory(prefix='doubao-ownership-')
        self.addCleanup(self.temp.cleanup)
        self.page = self.browser.new_page()
        # Every URL is fulfilled locally or aborted. No outgoing request can
        # reach an account, CDN, service, or model.
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body></body>')
                        if route.request.is_navigation_request() else route.abort())
        self.page.goto('http://127.0.0.1/chat/12340000000001')
        self.addCleanup(self.page.close)
        self.agent = FixtureAgent(self.page)
        self.agent._batch_ownership = {'marker': '【任务 marker-unique】', 'prompt': 'OFFICE TASK', 'control_texts': []}

    def own_user(self):
        return user('owner', 'OFFICE TASK\n【任务 marker-unique】')

    def test_delayed_old_card_is_not_claimed_before_submitted_user_exists(self):
        self.page.set_content(user('old-user', 'OLD GYM TASK') + assistant('old-answer'))
        self.assertEqual(self.agent._batch_assets(), [])
        self.assertFalse(self.agent._batch_scope_state['owner_found'])
        self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)', self.own_user())
        self.assertEqual(self.agent._batch_assets(), [])
        self.assertEqual(self.agent._assistant_text(), '')

    def test_only_cards_after_exact_owner_are_exposed(self):
        self.page.set_content(user('old-user', 'OLD GYM TASK') + assistant('old-answer') +
                              self.own_user() + assistant('office-answer', text='office ready'))
        assets = self.agent._batch_assets()
        self.assertEqual([a['card_message_id'] for a in assets], ['office-answer'])
        self.assertEqual(assets[0]['card_position'], 0)
        self.assertEqual(assets[0]['owner_user_message_id'], 'owner')
        self.assertTrue(assets[0]['ownership_verified'])
        self.assertEqual(self.agent._assistant_text(), 'office ready')

    def test_role_only_owner_is_found_alongside_older_canonical_messages(self):
        own = self.own_user().replace('data-testid="send_message" ', '')
        answer = assistant('role-answer').replace('data-testid="receive_message" ', '')
        self.page.set_content(user('old-user', 'OLD TASK') + assistant('old-answer') + own + answer)
        self.assertEqual([asset['card_message_id'] for asset in self.agent._batch_assets()], ['role-answer'])

    def test_nested_role_and_canonical_message_are_one_owner(self):
        nested = '<section data-message-id="owner" data-role="user"><div data-testid="send_message">OFFICE TASK 【任务 marker-unique】</div></section>'
        self.page.set_content(nested + assistant('one-answer'))
        self.assertEqual([asset['card_message_id'] for asset in self.agent._batch_assets()], ['one-answer'])

    def test_role_layout_ancestor_does_not_hide_distinct_role_messages(self):
        own = self.own_user().replace('data-testid="send_message" ', '')
        answer = assistant('role-answer').replace('data-testid="receive_message" ', '')
        self.page.set_content('<main data-role="assistant">' + own + answer + '</main>')
        self.assertEqual([asset['card_message_id'] for asset in self.agent._batch_assets()], ['role-answer'])

    def test_next_unrelated_user_turn_closes_ownership_scope(self):
        self.page.set_content(self.own_user() + assistant('office-answer') +
                              user('other-user', 'HIKING TASK') + assistant('hiking-answer', text='wrong reply'))
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-answer'])
        self.assertNotIn('wrong reply', self.agent._assistant_text())

    def control_recovery_spec(self, proof=True, task_count=1):
        spec = {'prompt': 'OFFICE TASK', 'marker': '【任务 marker-unique】',
                'user_message_id': 'original-user-not-mounted', 'chat_path': '/chat/12340000000001',
                'control_texts': ['确认仅生成本次办公室视频']}
        if proof:
            spec['original_anchor_proof'] = self.agent._original_anchor_proof(spec, spec['chat_path'])
        tasks = [{'id': f'office#{index}'} for index in range(task_count)]
        self.agent._batch_ownership = self.agent._ownership_spec(tasks, {},
                                      {'ownership': spec, 'ownership_acknowledged': True}, True)
        return self.agent._batch_ownership

    def virtualized_messages(self, control='确认仅生成本次办公室视频'):
        return assistant('unowned-before-control') + user('registered-control', control) + assistant('office-result')

    def test_verified_latest_control_can_anchor_virtualized_original_turn(self):
        self.control_recovery_spec()
        self.page.set_content(self.virtualized_messages())
        assets = self.agent._batch_assets()
        self.assertEqual([a['card_message_id'] for a in assets], ['office-result'])
        self.assertEqual(self.agent._batch_scope_state['anchor_kind'], 'verified_control')
        self.assertEqual(assets[0]['owner_user_message_id'], 'original-user-not-mounted')

    def test_control_fallback_needs_exact_original_proof_and_single_task(self):
        self.page.set_content(self.virtualized_messages())
        self.control_recovery_spec(proof=False)
        self.assertEqual(self.agent._batch_assets(), [], 'old acknowledgement alone is not proof')
        spec = self.control_recovery_spec()
        spec['marker'] = 'changed marker'
        self.agent._batch_ownership = self.agent._ownership_spec([{'id':'office#0'}], {}, {'ownership':spec}, True)
        self.assertEqual(self.agent._batch_assets(), [], 'proof is bound to the exact original marker')
        self.control_recovery_spec(task_count=2)
        self.assertEqual(self.agent._batch_assets(), [], 'multi-output slot ordering is ambiguous')
        self.control_recovery_spec()
        self.page.evaluate("history.pushState({},'', '/chat/12340000000005')")
        self.assertEqual(self.agent._batch_assets(), [], 'proof is bound to the exact formal chat')

    def test_duplicate_control_turns_are_ambiguous(self):
        self.control_recovery_spec()
        self.page.set_content(self.virtualized_messages() +
                              user('duplicate', '确认仅生成本次办公室视频') + assistant('other-result'))
        self.assertEqual(self.agent._batch_assets(), [])

    def test_only_latest_registered_control_can_replace_original(self):
        self.control_recovery_spec()
        self.agent._batch_ownership['control_texts'].append('最新明确确认，仅生成本条视频')
        self.page.set_content(self.virtualized_messages())
        self.assertEqual(self.agent._batch_assets(), [])

    def test_ambiguous_original_turns_cannot_be_bypassed_with_control(self):
        spec = self.control_recovery_spec()
        spec.pop('user_message_id')
        self.page.set_content(self.own_user() + user('duplicate-original', 'OFFICE TASK 【任务 marker-unique】') +
                              self.virtualized_messages())
        self.assertEqual(self.agent._batch_assets(), [])

    def test_control_anchor_still_stops_at_next_unrelated_user(self):
        self.control_recovery_spec()
        self.page.set_content(self.virtualized_messages() + user('another-task', '拍摄另一产品') + assistant('wrong-result'))
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-result'])
        self.page.set_content(self.virtualized_messages('确认仅生成本次办公室视频，然后拍另一个产品'))
        self.assertEqual(self.agent._batch_assets(), [], 'a control substring is not a registered control')

    def test_canonical_messages_are_not_hidden_by_broad_role_ancestors(self):
        self.page.set_content('<div data-role="assistant">' + self.own_user() + assistant('office-answer') + '</div>')
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-answer'])

    def test_same_request_confirmation_does_not_create_a_second_anchor(self):
        self.agent._batch_ownership['control_texts'] = ['确认\n【任务 marker-unique】']
        self.page.set_content(self.own_user() + assistant('ask-confirm', card=False, text='confirm?') +
                              user('confirmation', '确认\n【任务 marker-unique】') + assistant('office-answer'))
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-answer'])

    def test_mounted_confirmation_user_does_not_replay_previous_assistant_request(self):
        self.agent._batch_ownership['control_texts'] = ['确认\n【任务 marker-unique】']
        self.page.set_content(self.own_user() + assistant('ask-confirm', card=False, text='确认后我再开始生成视频') +
                              user('confirmation', '确认\n【任务 marker-unique】'))
        self.assertEqual(self.agent._batch_assets(), [])
        self.assertEqual(self.agent._assistant_text(), '')

    def test_copied_marker_or_control_substring_does_not_authorize_another_task(self):
        self.agent._batch_ownership['control_texts'] = ['确认\n【任务 marker-unique】']
        self.page.set_content(self.own_user() + assistant('ask-confirm', card=False) +
                              user('unrelated', '确认\n【任务 marker-unique】\n现在生成另一段滑雪广告') +
                              assistant('unrelated-answer'))
        self.assertEqual(self.agent._batch_assets(), [])

    def test_reloaded_dom_identity_can_fall_back_to_unique_full_prompt_in_locked_chat(self):
        self.agent._batch_ownership.update(user_message_id='old-dom-id', chat_path='/chat/12340000000001')
        self.page.set_content(self.own_user() + assistant('office-answer'))
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-answer'])
        self.agent._batch_ownership['chat_path'] = '/chat/different'
        self.assertEqual(self.agent._batch_assets(), [])

    def test_legacy_full_prompt_can_recover_without_sending(self):
        self.agent._batch_ownership = {'prompt': 'EXACT OFFICE PROMPT', 'control_texts': ['确认仅生成办公室']}
        self.page.set_content(user('old-user', 'GYM PROMPT') + assistant('old-answer') +
                              user('office-user', '生成视频： EXACT\n OFFICE PROMPT，15s') +
                              assistant('confirm', card=False) + user('control', '确认仅生成办公室') +
                              assistant('office-answer'))
        self.assertEqual([a['card_message_id'] for a in self.agent._batch_assets()], ['office-answer'])
        self.assertFalse(self.agent.sent)

    def test_duplicate_legacy_prompts_are_ambiguous_and_fail_closed(self):
        self.agent._batch_ownership = {'prompt': 'OFFICE TASK'}
        self.page.set_content(user('one', 'OFFICE TASK') + assistant('one-answer') +
                              user('two', 'OFFICE TASK') + assistant('two-answer'))
        self.assertEqual(self.agent._batch_assets(), [])

    def test_root_route_cannot_acknowledge_a_new_chat(self):
        self.page.evaluate("history.pushState({},'', '/chat/')")
        self.page.set_content(self.own_user() + assistant('office-answer'))
        self.assertEqual(self.agent._batch_assets(), [])
        self.assertTrue(self.agent._batch_scope_state['owner_found'])
        self.assertFalse(self.agent._batch_scope_state['route_ready'])

    def route_run(self, mode):
        self.agent.dcfg['ownership_ack_timeout_seconds'] = .5 if mode == 'promote' else .06
        task = dict(id='route#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='ROUTE TASK')
        destination = Path(self.temp.name) / 'route.mp4'
        submitted = []

        def send(prompt):
            self.agent.sent.append(prompt)
            if mode == 'no-owner':
                return
            self.page.evaluate("history.pushState({},'', '/chat/local_123456')")
            self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)',
                                               user('route-user', prompt) +
                                               assistant('route-answer', video='https://fixture.invalid/route.mp4'))
            if mode == 'promote':
                self.page.evaluate("setTimeout(()=>history.pushState({},'', '/chat/12340000000005'),150)")

        self.agent.type_and_send = send
        result = self.agent.run_batch([task], [], {'route#0': destination},
                                      {'request_id': 'route-request', 'total_seconds': 15},
                                      lambda url, baseline: submitted.append(url), lambda *args: None)
        return result, submitted, destination

    def test_local_route_promotes_to_numeric_before_any_url_ack_or_lock(self):
        result, submitted, destination = self.route_run('promote')
        self.assertFalse(result['unresolved'])
        self.assertTrue(destination.exists())
        self.assertEqual(submitted[0], '')
        self.assertTrue(all(not url or url.endswith('/chat/12340000000005') for url in submitted))
        self.assertEqual(self.agent._batch_ownership['chat_path'], '/chat/12340000000005')
        self.assertNotIn('local_', result['chat_url'])

    def test_local_route_ack_timeout_preserves_submitted_request_without_claiming(self):
        result, submitted, destination = self.route_run('local-only')
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertIn('正式数字ID对话路由确认', result['error'])
        self.assertTrue(result['submitted'])
        self.assertEqual(submitted, [''])
        self.assertNotIn('chat_path', self.agent._batch_ownership)
        self.assertFalse(destination.exists())
        self.agent.session.context.request.get.assert_not_called()

    def test_missing_owner_has_independent_ack_timeout(self):
        result, submitted, destination = self.route_run('no-owner')
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertIn('本次用户消息定位', result['error'])
        self.assertTrue(result['submitted'])
        self.assertEqual(submitted, [''])
        self.assertFalse(destination.exists())

    def test_new_numeric_route_is_saved_before_owner_hydration_without_claiming_cards(self):
        self.page.goto('https://www.doubao.com/chat/')
        self.page.set_content('')
        self.agent.dcfg['ownership_ack_timeout_seconds'] = .05
        def send(prompt):
            self.agent.sent.append(prompt)
            self.page.evaluate("history.pushState({},'', '/chat/12340000000006')")
            self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)',
                                               user('partial-new-user', self.agent._batch_ownership['marker']) +
                                               assistant('not-yet-owned-answer'))
        self.agent.type_and_send = send
        task = dict(id='route#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='ROUTE TASK')
        submitted = []
        result = self.agent.run_batch([task], [], {'route#0': Path(self.temp.name) / 'unclaimed.mp4'},
            {'request_id': 'route-request', 'total_seconds': 15},
            lambda url, baseline: submitted.append((url, bool(baseline.get('ownership_acknowledged')))),
            lambda *args: self.fail('No owned result exists'))
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertEqual(result['chat_url'], 'https://www.doubao.com/chat/12340000000006')
        self.assertEqual(submitted, [('', False), (result['chat_url'], False)])
        self.assertFalse(result['remote_refs'])
        self.agent.session.context.request.get.assert_not_called()

    def test_numeric_navigation_without_this_request_marker_is_not_saved_as_recovery(self):
        self.page.goto('https://www.doubao.com/chat/')
        self.page.set_content('')
        self.agent.dcfg['ownership_ack_timeout_seconds'] = .05
        def send(prompt):
            self.agent.sent.append(prompt)
            self.page.evaluate("history.pushState({},'', '/chat/12340000000007')")
            self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)',
                                               user('old-user', 'OLD TASK') + assistant('old-answer'))
        self.agent.type_and_send = send
        task = dict(id='route#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='ROUTE TASK')
        submitted = []
        result = self.agent.run_batch([task], [], {'route#0': Path(self.temp.name) / 'old-route.mp4'},
            {'request_id': 'route-request', 'total_seconds': 15},
            lambda url, baseline: submitted.append(url), lambda *args: self.fail('No owned result exists'))
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertEqual(result['chat_url'], '')
        self.assertEqual(submitted, [''])
        self.assertFalse(result['remote_refs'])
        self.agent.session.context.request.get.assert_not_called()

    def test_resume_rejects_local_route_before_navigation_and_never_sends(self):
        before = self.page.url
        task = dict(id='route#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='ROUTE TASK')
        result = self.agent.run_batch([task], [], {'route#0': Path(self.temp.name) / 'resume-local.mp4'},
                                      {'resume_only': True, 'chat_url': 'https://www.doubao.com/chat/local_123456',
                                       'total_seconds': 15}, lambda *args: None, lambda *args: None)
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertIn('临时 local_', result['error'])
        self.assertTrue(result['submitted'])
        self.assertEqual(before, self.page.url)
        self.assertFalse(self.agent.sent)

    def test_unverified_saved_url_cannot_download(self):
        destination = Path(self.temp.name) / 'unsafe.mp4'
        self.assertFalse(self.agent._download_batch_asset({'url': 'https://fixture.invalid/old.mp4'}, destination))
        self.agent.session.context.request.get.assert_not_called()
        self.assertFalse(destination.exists())

    def test_card_click_does_not_fall_back_to_an_existing_old_video(self):
        self.page.set_content(self.own_user() + assistant('office-answer') +
                              '<video src="https://fixture.invalid/old.mp4"></video>')
        ref = self.agent._batch_assets()[0]
        destination = Path(self.temp.name) / 'wrong.mp4'
        self.assertFalse(self.agent._download_batch_asset(ref, destination))
        self.agent.session.context.request.get.assert_not_called()
        self.assertFalse(destination.exists())

    def test_stable_message_identity_replaces_stale_receive_index(self):
        self.page.set_content(user('old-user', 'OLD GYM TASK') + assistant('old-answer') +
                              self.own_user() + assistant('office-answer'))
        self.page.locator('[data-message-id="office-answer"] .block-video-fixture').evaluate('''node => {
            node.onclick = () => document.body.insertAdjacentHTML('beforeend',
                '<video src="https://fixture.invalid/office.mp4"></video>');
        }''')
        ref = dict(self.agent._batch_assets()[0], receive_index=0)
        destination = Path(self.temp.name) / 'office.mp4'
        self.assertTrue(self.agent._download_batch_asset(ref, destination))
        self.assertEqual(self.agent.session.context.request.get.call_args.args[0], 'https://fixture.invalid/office.mp4')
        self.assertTrue(destination.exists())

    def test_run_waits_for_owned_turn_and_new_route_before_claim_or_url_ack(self):
        self.page.set_content(user('old-user', 'OLD GYM TASK') +
                              assistant('old-answer', video='https://fixture.invalid/old.mp4'))
        task = dict(id='office#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='OFFICE TASK')
        generated, submitted = [], []
        result = self.agent.run_batch([task], [], {'office#0': Path(self.temp.name) / 'office.mp4'},
                                      {'request_id': 'unique-request', 'total_seconds': 15},
                                      lambda url, baseline: submitted.append(url),
                                      lambda task_id, ref: generated.append(ref['card_message_id']))
        self.assertFalse(result['unresolved'])
        self.assertEqual(generated, ['new-answer'])
        self.assertEqual(submitted[0], '')
        self.assertTrue(all(not url or url.endswith('/chat/12340000000002') for url in submitted))
        self.assertEqual(len(self.agent.sent), 1)

    def test_old_raw_without_binding_is_preserved_and_redownloaded(self):
        self.page.set_content(user('old-user', 'OLD GYM TASK') + assistant('old-answer'))
        destination = Path(self.temp.name) / 'office.mp4'
        old_bytes = b'WRONG OLD GYM VIDEO' * 200
        destination.write_bytes(old_bytes)
        task = dict(id='office#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='OFFICE TASK')
        result = self.agent.run_batch([task], [], {'office#0': destination},
                                      {'request_id': 'unique-request', 'total_seconds': 15},
                                      lambda *args: None, lambda *args: None)
        self.assertFalse(result['unresolved'])
        self.assertEqual(destination.read_bytes(), b'x' * 2048)
        self.assertTrue(any(p.read_bytes() == old_bytes for p in (destination.parent / '.download-backups').iterdir()))
        self.agent.session.context.request.get.assert_called_once()
        binding = json.loads(destination.with_suffix('.mp4.asset.json').read_text())
        self.assertEqual(binding['identity']['message_id'], 'new-answer')

    def test_bound_raw_requires_original_ref_and_matching_actual_file_hash(self):
        self.page.set_content(self.own_user() + assistant('office-answer'))
        ref = self.agent._batch_assets()[0]
        destination = Path(self.temp.name) / 'bound.mp4'
        destination.write_bytes(b'x' * 2048)
        self.agent._bind_download(destination, ref)
        self.assertTrue(self.agent._bound_download_matches(destination, ref, dict(ref)))
        self.assertTrue(verified_download_matches(destination, ref))
        self.assertFalse(self.agent._bound_download_matches(destination, ref, {}))
        other = dict(ref, card_message_id='different-answer')
        self.assertFalse(self.agent._bound_download_matches(destination, ref, other))
        destination.write_bytes(b'y' * 2048)
        self.assertFalse(self.agent._bound_download_matches(destination, ref, dict(ref)))
        self.assertFalse(verified_download_matches(destination, ref))

    def test_runtime_persists_original_proof_then_recovers_virtualized_control(self):
        task = dict(id='office#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='OFFICE TASK')
        control = '确认仅生成本次办公室视频'
        snapshots, replaced = [], []

        def send(prompt):
            self.agent.sent.append(prompt)
            self.agent._batch_ownership['control_texts'].append(control)
            self.page.set_content(user('main-original', prompt) + assistant('working', card=False, text='正在生成'))

        def submitted(url, baseline):
            snapshots.append(json.loads(json.dumps(baseline)))
            if url and baseline['ownership'].get('original_anchor_proof') and not replaced:
                replaced.append(True)
                # The real service virtualizes the long original prompt while
                # keeping its registered confirmation and newest output.
                self.page.set_content(self.virtualized_messages(control).replace(
                    '<div class="block-video-fixture"><img',
                    '<div class="block-video-fixture"><video src="https://fixture.invalid/office.mp4"></video><img'))

        self.agent.type_and_send = send
        result = self.agent.run_batch([task], [], {'office#0': Path(self.temp.name) / 'virtualized.mp4'},
                                      {'request_id': 'virtualized-request', 'total_seconds': 15}, submitted,
                                      lambda *args: None)
        self.assertFalse(result['unresolved'])
        self.assertEqual(len(self.agent.sent), 1)
        self.assertTrue(any(s['ownership'].get('original_anchor_proof') for s in snapshots))
        self.assertEqual(self.agent._batch_ownership['last_anchor_kind'], 'verified_control')
        self.assertEqual(self.agent._batch_ownership['user_message_id'], 'main-original')

    def test_current_run_verified_download_is_reused_while_other_slot_waits(self):
        self.agent.dcfg['generation_timeout_minutes'] = .008
        tasks = [dict(id=f'office#{i}', seg_index=i, seg_total=2, target_seconds=15,
                      est_seconds=15, start_seconds=i*15, end_seconds=(i+1)*15, prompt=f'OFFICE TASK {i}')
                 for i in range(2)]
        result = self.agent.run_batch(tasks, [], {t['id']: Path(self.temp.name) / f'{i}.mp4' for i,t in enumerate(tasks)},
                                      {'request_id': 'partial-request', 'total_seconds': 30},
                                      lambda *args: None, lambda *args: None)
        self.assertEqual(set(result['videos']), {'office#0'})
        self.assertEqual(result['unresolved'], ['office#1'])
        self.agent.session.context.request.get.assert_called_once()

    def confirmation_run(self, mode):
        task = dict(id='office#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='OFFICE TASK')
        self.page.set_content('')

        def send(prompt):
            self.agent.sent.append(prompt)
            if len(self.agent.sent) == 1:
                markup = user('main-user', prompt) + assistant('confirm-one', card=False, text='确认后我再开始生成视频。')
                self.page.evaluate("history.pushState({},'', '/chat/12340000000003')")
                self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)', markup)
            else:
                self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)', user('confirmation-user', prompt))
                if mode == 'repeat':
                    self.page.locator('body').evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)',
                                                       assistant('confirm-two', card=False, text='确认后我再开始生成视频。'))
                else:
                    self.page.evaluate('''markup => setTimeout(() => document.body.insertAdjacentHTML('beforeend',markup),150)''',
                                       assistant('generated-answer', video='https://fixture.invalid/office.mp4'))

        self.agent.type_and_send = send
        return self.agent.run_batch([task], [], {'office#0': Path(self.temp.name) / 'confirmed.mp4'},
                                    {'request_id': 'confirmation-request', 'total_seconds': 15}, lambda *args: None, lambda *args: None)

    def test_second_confirmation_same_text_new_reply_stops_instead_of_waiting(self):
        result = self.confirmation_run('repeat')
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertIn('仍要求确认', result['error'])
        self.assertEqual(len(self.agent.sent), 2)
        self.assertTrue(self.agent.sent[1].startswith('确认，立即开始生成上述'))
        self.assertFalse(result['videos'])
        self.assertFalse(result['continuation_safe'])

    def test_one_clear_confirmation_can_wait_for_actual_generated_answer(self):
        result = self.confirmation_run('generate')
        self.assertFalse(result['unresolved'])
        self.assertEqual(len(self.agent.sent), 2)
        self.assertTrue(self.agent.sent[1].startswith('确认，立即开始生成上述'))

    def test_resume_confirmation_is_reported_without_sending_or_waiting_for_generation(self):
        target = 'https://www.doubao.com/chat/12340000000004'
        markup = user('owner', 'OFFICE TASK') + assistant('confirm-one', card=False, text='确认后我再开始生成视频。')
        self.page.route(target, lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=markup))
        task = dict(id='office#0', seg_index=0, seg_total=1, target_seconds=15,
                    est_seconds=15, start_seconds=0, end_seconds=15, prompt='OFFICE TASK')
        result = self.agent.run_batch([task], [], {'office#0': Path(self.temp.name) / 'resume.mp4'},
                                      {'resume_only': True, 'chat_url': target, 'total_seconds': 15,
                                       'baseline': {'ownership': {'user_message_id': 'owner', 'prompt': 'OFFICE TASK'}}},
                                      lambda *args: None, lambda *args: None)
        self.assertEqual(result['error_kind'], 'needs_user')
        self.assertIn('恢复模式不会发送消息', result['error'])
        self.assertFalse(self.agent.sent)
        self.assertFalse(result['videos'])


if __name__ == '__main__':
    unittest.main()
