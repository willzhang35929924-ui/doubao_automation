"""Raw recovery must prove request ownership before local completion. No browser/network."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from doubao_automation import orchestrator
from doubao_automation.doubao_batch import BatchGenerationMixin


class RawRecoveryBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='raw-recovery-binding-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('STATE_DIR', self.root), ('VIDEOS_DIR', self.root),
                            ('FRAMES_DIR', self.root), ('ROOT', self.root)]:
            self.stack.enter_context(patch.object(orchestrator, name, value))
        self.stack.enter_context(patch.object(orchestrator, 'ensure_dirs'))
        self.pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='account')]}})
        self.raw = self.root / 'script_seg0.raw.mp4'
        self.raw.write_bytes(b'synthetic local raw data' * 100)
        self.ref = dict(key='card:current', ownership_verified=True, kind='card',
                        card_message_id='assistant-current', card_ordinal=0,
                        owner_user_message_id='user-current')
        self.task = dict(id='script#seg0', script_id='script', seg_index=0, seg_total=1,
                         target_seconds=15, est_seconds=15, status='awaiting_result',
                         request_id='original-request', account='account',
                         raw_video=str(self.raw), remote_ref=self.ref)
        self.pipeline.accounts.reserve('account', [self.task['id']], 'original-request')
        self.pipeline.accounts.mark_submitted('account', 'original-request',
                                              chat_url='https://example.invalid/original-chat')
        self.pipeline.accounts.record_generated('account', self.task['id'],
                                                 request_id='original-request', remote_ref=self.ref)
        self.pipeline.store.reset(dict(tasks=[self.task], scripts=[], requests={
            'original-request': dict(request_id='original-request', account='account',
                                     task_ids=[self.task['id']], script_id='script',
                                     phase='submitted', submitted=True,
                                     chat_url='https://example.invalid/original-chat')}))

    def bind(self, ref=None):
        BatchGenerationMixin()._bind_download(self.raw, ref or self.ref)

    def test_verified_raw_can_retry_local_finalize_without_new_quota(self):
        self.bind()
        before = self.pipeline.accounts.store.snapshot()
        with patch.object(self.pipeline, '_finalize_local') as finalize:
            self.pipeline._recover_state()
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.args[1], self.raw)
        self.assertEqual(self.pipeline.accounts.store.snapshot(), before)

    def test_verified_local_failure_is_not_sent_back_to_remote_generation(self):
        self.bind()
        with patch.object(self.pipeline, '_finalize_local', side_effect=RuntimeError('local trim failed')):
            self.pipeline._recover_state()
        task = self.pipeline.store.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'local_failed')
        self.assertIsNone(self.pipeline._claim_batch('account'))
        self.assertEqual(task['request_id'], 'original-request')
        self.assertTrue(self.raw.is_file())

    def assert_original_request_remains_resumable(self):
        raw_before = self.raw.read_bytes()
        ledger_before = copy.deepcopy(self.pipeline.accounts.store.snapshot())
        with patch.object(self.pipeline, '_finalize_local') as finalize:
            self.pipeline._recover_state()
        finalize.assert_not_called()
        batch = self.pipeline._claim_batch('account')
        self.assertEqual([task['id'] for task in batch], [self.task['id']])
        self.assertEqual(batch[0]['request_id'], 'original-request')
        snapshot = self.pipeline.store.snapshot()
        self.assertEqual(list(snapshot['requests']), ['original-request'])
        self.assertTrue(snapshot['requests']['original-request']['submitted'])
        self.assertEqual(snapshot['requests']['original-request']['chat_url'],
                         'https://example.invalid/original-chat')
        self.assertEqual(self.pipeline.accounts.store.snapshot(), ledger_before)
        self.assertEqual(self.raw.read_bytes(), raw_before)

    def test_legacy_raw_without_sidecar_is_preserved_and_original_request_can_resume(self):
        self.assert_original_request_remains_resumable()

    def test_raw_bound_to_other_message_is_not_completed_or_skipped(self):
        wrong = dict(self.ref, card_message_id='assistant-old', key='card:old')
        self.bind(wrong)
        self.assert_original_request_remains_resumable()

    def test_tampered_raw_is_not_completed_or_skipped(self):
        self.bind()
        self.raw.write_bytes(b'changed local raw content' * 100)
        self.assert_original_request_remains_resumable()


if __name__ == '__main__':
    unittest.main()
