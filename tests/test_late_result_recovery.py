"""A timed-out generation can finish after another account's normal pass."""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from doubao_automation import orchestrator as module, video_utils
from doubao_automation.doubao_batch import BatchGenerationMixin
from tests.test_batch_pipeline import FakeAgent, Harness


CHAT_URL = 'https://www.doubao.com/chat/1234567890'
CHAT_PATH = '/chat/1234567890'


def accepted_baseline(rid, *, acknowledged=True, valid_proof=True):
    ownership = dict(prompt='原始剧本', marker=f'【自动化任务标记：{rid}】',
                     chat_path=CHAT_PATH, user_message_id='original-turn')
    if valid_proof:
        ownership['original_anchor_proof'] = BatchGenerationMixin._original_anchor_proof(ownership, CHAT_PATH)
    return dict(scope_version=1, ownership_acknowledged=acknowledged, ownership=ownership)


class LateCardAgent(FakeAgent):
    def run_batch(self, tasks, images, dests, context, on_submitted, on_generated):
        harness = self.harness
        if self.name == 'a' and not context['resume_only']:
            rid = context['request_id']
            harness.mode = 'unknown'
            try:
                result = super().run_batch(
                    tasks, images, dests, context,
                    lambda *_: on_submitted(CHAT_URL, accepted_baseline(rid)), on_generated,
                )
                result['chat_url'] = CHAT_URL
                return result
            finally:
                harness.mode = 'success'
        if context['resume_only'] and not harness.case.late_card_ready:
            harness.mode = 'unknown'
        return super().run_batch(tasks, images, dests, context, on_submitted, on_generated)


class LateResultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='late-result-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.h = Harness(self, self.path)
        self.late_card_ready = True
        self.output = io.StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(self.output))
        for name, value in (('STATE_DIR', self.h.state), ('VIDEOS_DIR', self.h.videos),
                            ('FRAMES_DIR', self.h.frames), ('ROOT', self.path)):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(module, 'ensure_dirs'))
        self.sessions = stack.enter_context(patch.object(module, 'BrowserSession', side_effect=self.h.session))
        stack.enter_context(patch.object(module, 'DoubaoAgent', side_effect=lambda _s, _cfg, name: LateCardAgent(self.h, name)))
        stack.enter_context(patch.object(module, 'find_ffmpeg', return_value='fake-ffmpeg'))
        stack.enter_context(patch.object(module, 'extract_last_frame', side_effect=self.h.extract))
        stack.enter_context(patch.object(video_utils, 'trim_video', side_effect=self.h.trim))
        self.cfg = {'accounts': {'doubao': [dict(name=name, profile='fixture-' + name, enabled=True)
                                             for name in ('a', 'b')]},
                    'video': {'auto_concat': False}}
        self.pipeline = module.Pipeline(self.cfg)
        self.h.pipeline = self.pipeline

    def plan(self, *script_ids):
        image = self.path / 'product.png'
        image.write_bytes(b'fixture image')
        scripts, tasks = [], []
        for script_id in script_ids:
            text = f'Complete original script {script_id}.'
            self.h.script_texts[script_id] = text
            self.h.script_seconds[script_id] = 15
            scripts.append(dict(id=script_id, text=text))
            tasks.append(dict(id=script_id+'#seg0', script_id=script_id, seg_index=0, seg_total=1,
                              prompt='Fixture scene', narration='Fixture voice', product_images=[str(image)],
                              est_seconds=15, target_seconds=15, start_seconds=0, end_seconds=15,
                              status='pending', account='', attempts=0, video='', raw_video='',
                              error='', request_id='', remote_ref={}))
        self.pipeline.store.reset(dict(scripts=scripts, tasks=tasks, requests={}, assemblies={}))

    def test_late_card_is_downloaded_after_other_account_finishes_with_same_request(self):
        self.plan('late', 'other')
        self.pipeline._stage_run_locked()
        normal, other, recovery = self.h.calls
        self.assertEqual([c['account'] for c in (normal, other, recovery)], ['a', 'b', 'a'])
        self.assertEqual([c['resume_only'] for c in (normal, other, recovery)], [False, False, True])
        rid = normal['context']['request_id']
        self.assertEqual(recovery['context']['request_id'], rid)
        self.assertEqual(recovery['context']['chat_url'], CHAT_URL)
        self.assertEqual(recovery['context']['recovery_timeout_seconds'], 45)
        self.assertEqual(self.sessions.call_args.args[:2], ('a', 'fixture-a'))
        self.assertEqual(self.h.sent, 2)
        state = self.pipeline.store.snapshot()
        self.assertEqual(set(state['requests']), self.pipeline._attempted_requests)
        self.assertEqual(len(state['requests']), 2)
        self.assertTrue(all(task['status'] == 'done' and Path(task['video']).is_file() for task in state['tasks']))
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 1)
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'], 0)

    def test_missing_late_card_stays_unresolved_after_one_short_lookup(self):
        self.late_card_ready = False
        self.plan('late', 'other')
        with self.assertRaisesRegex(RuntimeError, '完成1/2'):
            self.pipeline._stage_run_locked()
        self.assertEqual(len(self.h.calls), 3)
        self.assertEqual(self.h.sent, 2)
        state = self.pipeline.store.snapshot()
        late = state['tasks'][0]
        self.assertEqual(late['status'], 'awaiting_result')
        self.assertEqual(state['requests'][late['request_id']]['phase'], 'unknown')
        self.assertEqual(len(state['requests']), 2)
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'], 1)
        self.assertIn('从原会话恢复检查', self.output.getvalue())
        self.assertNotIn('次日', self.output.getvalue())

    def seed_waiting(self, *, chat_url=CHAT_URL, ledger_url=None, submitted=True,
                     attempted=True, error_kind='timeout', acknowledged=True, valid_proof=True):
        self.pipeline.accounts.store.reset({})
        self.plan('late')
        batch = self.pipeline._claim_batch('a')
        rid = batch[0]['request_id']
        baseline = accepted_baseline(rid, acknowledged=acknowledged, valid_proof=valid_proof)
        if submitted:
            self.pipeline.accounts.mark_submitted('a', rid, chat_url=ledger_url or chat_url, baseline=baseline)
            self.pipeline.accounts.mark_unknown('a', rid)
        with self.pipeline.store.txn() as data:
            data['requests'][rid].update(phase='unknown', error_kind=error_kind, submitted=True,
                                         chat_url=chat_url, baseline=baseline)
            data['tasks'][0]['status'] = 'awaiting_result'
        self.pipeline._attempted_requests = {rid} if attempted else set()
        return self.pipeline.store.snapshot()['tasks']

    def test_ineligible_requests_do_not_open_a_browser(self):
        cases = [dict(attempted=False), dict(submitted=False), dict(error_kind='needs_user'),
                 dict(chat_url=CHAT_URL.replace('https:', 'http:')),
                 dict(chat_url='https://www.doubao.com/chat/local_not_formal'),
                 dict(ledger_url='https://www.doubao.com/chat/999'),
                 dict(acknowledged=False), dict(valid_proof=False)]
        for options in cases:
            with self.subTest(options=options):
                self.seed_waiting(**options)
                self.pipeline._recover_late_results()
                self.sessions.assert_not_called()
        self.seed_waiting()
        self.pipeline.accounts.mark_needs_login('a', 'Fixture login expired')
        self.pipeline._recover_late_results()
        self.sessions.assert_not_called()
        self.seed_waiting()
        self.cfg['accounts']['doubao'][0]['enabled'] = False
        self.pipeline._recover_late_results()
        self.sessions.assert_not_called()

    def test_short_execution_cannot_fall_through_to_sending_when_binding_changes(self):
        tasks = self.seed_waiting(submitted=False)
        agent = Mock()
        with patch.object(self.pipeline, '_task_images') as images:
            self.assertFalse(self.pipeline._execute_batch(agent, 'a', tasks, recovery_timeout_seconds=45))
        agent.run_batch.assert_not_called()
        images.assert_not_called()
        self.assertEqual(self.pipeline.accounts.quota('a')['reserved'], 1)
        self.assertEqual(len(self.pipeline.store.snapshot()['requests']), 1)

    def test_short_lookup_cannot_release_or_exhaust_from_dialog_or_quota_text(self):
        for kind in ('quota', 'partial_failed_refunded', 'user_approval'):
            with self.subTest(kind=kind):
                tasks = self.seed_waiting(error_kind='download')
                task = tasks[0]
                before = self.pipeline.accounts.quota('a')
                agent = Mock()
                agent.run_batch.return_value = dict(
                    videos={}, remote_refs={}, chat_url=CHAT_URL, submitted=True,
                    error_kind=kind, error='Fixture ambiguous reply text',
                    quota_remaining=0, continuation_safe=True, rejected_task_ids=[task['id']],
                    rejection_evidence=dict(kind='generation_failed_refunded', definitive=True,
                        active=False, source='latest_assistant', quota_not_charged=True,
                        failed_task_ids=[task['id']]),
                )
                with patch.object(self.pipeline.accounts, 'mark_awaiting_approval') as approval:
                    self.assertFalse(self.pipeline._execute_batch(agent, 'a', tasks, recovery_timeout_seconds=45))
                approval.assert_not_called()
                self.assertNotIn('on_quota_remaining', agent.run_batch.call_args.args[3])
                self.assertEqual(self.pipeline.accounts.quota('a'), before)
                self.assertEqual(self.pipeline.accounts.status('a')['status'], 'available')
                saved = self.pipeline.store.snapshot()
                self.assertEqual(saved['tasks'][0]['request_id'], task['request_id'])
                self.assertEqual(saved['requests'][task['request_id']]['phase'], 'unknown')


if __name__ == '__main__':
    unittest.main()
