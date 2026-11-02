"""Batch pipeline tests with temporary state and fake remote/media operations."""
import io
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doubao_automation import orchestrator as module  # noqa: E402
from doubao_automation import video_utils  # noqa: E402
from doubao_automation.orchestrator import Pipeline  # noqa: E402


class FakeSession:
    def __init__(self, harness, name):
        self.harness = harness
        self.name = name
        self.started = False

    def start(self):
        self.started = True
        self.harness.opened.append(self.name)
        self.harness.active.add(self.name)
        self.harness.max_active = max(self.harness.max_active, len(self.harness.active))

    def stop(self):
        if self.started:
            self.harness.active.discard(self.name)
            self.started = False


class FakeAgent:
    def __init__(self, harness, name):
        self.harness = harness
        self.name = name

    def open_chat(self):
        if self.name in self.harness.login_fail_accounts:
            raise module.NeedsLogin('Injected login requirement')

    def run_task(self, *args, **kwargs):
        raise AssertionError('The batch pipeline must use run_batch, not run_task')

    def run_batch(self, tasks, image_paths, dests, context, on_submitted, on_generated):
        h = self.harness
        resume_only = bool(context.get('resume_only'))
        ids = [task['id'] for task in tasks]
        indices = [task['seg_index'] for task in tasks]
        h.case.assertLessEqual(len(tasks), 2)
        h.case.assertGreaterEqual(len(tasks), 1)
        h.case.assertEqual(len({task['script_id'] for task in tasks}), 1)
        h.case.assertEqual(indices, list(range(indices[0], indices[0] + len(indices))))
        h.case.assertTrue(all(0 < task['target_seconds'] <= 15 for task in tasks))
        h.case.assertEqual(context['script_text'], h.script_texts[tasks[0]['script_id']])
        h.case.assertEqual(context['total_seconds'], h.script_seconds[tasks[0]['script_id']])

        snapshot = h.pipeline.store.snapshot()
        predecessors = [task for task in snapshot['tasks']
                        if task['script_id'] == tasks[0]['script_id']
                        and task['seg_index'] < indices[0]]
        if not resume_only:h.case.assertTrue(all(task['status'] == 'done' for task in predecessors))
        if resume_only:h.case.assertEqual(image_paths, [], 'Download-only recovery must not require or upload tail images')
        if indices[0] > 0 and not resume_only:
            required = h.frames / f"{tasks[0]['script_id']}_seg{indices[0] - 1}_end.png"
            h.case.assertTrue(required.is_file(), 'The preceding tail must exist before the next batch')
            h.case.assertEqual(Path(image_paths[0]), required)

        h.calls.append(dict(account=self.name, task_ids=ids, indices=indices,
                            script_id=tasks[0]['script_id'], context=dict(context),
                            resume_only=resume_only))
        h.events.append(('batch', tuple(ids), resume_only))
        chat_url = context.get('chat_url') or 'https://example.invalid/chat/' + context['request_id']
        if h.mode in ('unsubmitted_once','preflight_failure'):
            persistent=h.mode=='preflight_failure'
            if not persistent:h.mode = 'success'
            return dict(videos={}, remote_refs={}, unresolved=ids, submitted=False,
                        chat_url=chat_url, error_kind='failed',
                        error='找不到视频时长控件，发送前已停止，避免默认10秒浪费次数' if persistent else 'Upload failed before send',
                        continuation_safe=False)
        if not resume_only:
            h.sent += 1
            on_submitted(chat_url, {'video_count': 0, 'known_ids': []})

        quota_mode=h.mode if self.name=='a' else ''
        if quota_mode in ('quota_reject_a','quota_unknown_a'):
            return dict(videos={},remote_refs={},unresolved=ids,submitted=True,chat_url=chat_url,
                        error_kind='quota',error='Latest assistant quota response',
                        continuation_safe=quota_mode=='quota_reject_a')

        if h.mode in ('unknown','empty_refs'):
            return dict(videos={}, remote_refs={tid:{} for tid in ids} if h.mode=='empty_refs' else {}, unresolved=ids, submitted=True,
                        chat_url=chat_url, error_kind='timeout', error='Result not yet reconciled',
                        continuation_safe=False)

        partial_quota=quota_mode in ('partial_quota_reject_a','partial_quota_download_a')
        refund_mode=h.mode if h.mode in ('partial_refunded','partial_refunded_download','partial_refunded_unproven','refund_every_attempt') else ''
        failed_tasks=tasks if refund_mode=='refund_every_attempt' else tasks[:1] if refund_mode else []
        generated = ([] if refund_mode=='refund_every_attempt' else tasks[1:] if refund_mode else
                     tasks[:1] if h.mode == 'partial' or partial_quota else tasks)
        refs = {}
        videos = {}
        for task in generated:
            ref = {'id': 'remote-' + task['id'], 'key': 'remote-' + task['id']}
            on_generated(task['id'], ref)
            refs[task['id']] = ref
            h.generated_balances.append(h.pipeline.accounts.quota(self.name)['used'])
            if h.mode != 'download_failure' and quota_mode!='partial_quota_download_a' and refund_mode!='partial_refunded_download':
                dest = Path(dests[task['id']])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b'fake generated mp4')
                h.durations[str(dest)] = 15.0
                videos[task['id']] = str(dest)
        unresolved = [task_id for task_id in ids if task_id not in videos]
        if refund_mode:
            failed_ids=[task['id'] for task in failed_tasks]
            evidence=dict(kind='generation_failed_refunded',definitive=True,active=False,
                          source='latest_assistant',quota_not_charged=True,failed_task_ids=failed_ids)
            if refund_mode=='partial_refunded_unproven':evidence.pop('failed_task_ids')
            if refund_mode!='refund_every_attempt':h.mode='success'
            return dict(videos=videos,remote_refs=refs,unresolved=unresolved,submitted=True,chat_url=chat_url,
                        error_kind='partial_failed_refunded',rejected_task_ids=failed_ids,rejection_evidence=evidence,
                        error='Latest assistant confirms failed-card videos did not consume quota',continuation_safe=True)
        return dict(videos=videos, remote_refs=refs, unresolved=unresolved,
                    submitted=True, chat_url=chat_url,
                    error_kind='quota' if partial_quota else 'download' if unresolved else '',
                    error='Download unavailable' if unresolved else '',
                    continuation_safe=partial_quota or not unresolved)


class Harness:
    def __init__(self, case, path):
        self.case = case
        self.path = path
        self.state = path / 'state'
        self.videos = path / 'videos'
        self.frames = path / 'frames'
        for directory in (self.state, self.videos, self.frames):
            directory.mkdir(parents=True, exist_ok=True)
        self.mode = 'success'
        self.login_fail_accounts = set()
        self.calls = []
        self.events = []
        self.sent = 0
        self.opened = []
        self.active = set()
        self.max_active = 0
        self.trims = []
        self.concat_calls = []
        self.fail_concat = False
        self.fail_extract = False
        self.generated_balances = []
        self.durations = {}
        self.script_texts = {}
        self.script_seconds = {}
        self.pipeline = None

    def session(self, name, profile, cfg, **kwargs):
        return FakeSession(self, name)

    def agent(self, session, cfg, name):
        return FakeAgent(self, name)

    def trim(self, video, out_mp4, target_seconds, ffmpeg=None, **kwargs):
        out = Path(out_mp4)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b'fake trimmed mp4')
        self.trims.append((Path(video), out, float(target_seconds)))
        self.durations[str(out)] = float(target_seconds)
        return out

    def extract(self, video, out_png, ffmpeg=None, offset_seconds=0.3):
        # Publication must follow extraction, not precede it.
        current = next(task for task in self.pipeline.store.snapshot()['tasks']
                       if Path(video).name == f"{task['script_id']}_seg{task['seg_index']}.mp4")
        self.case.assertNotEqual(current['status'], 'done')
        if self.fail_extract:
            raise RuntimeError('Injected local tail extraction failure')
        out = Path(out_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b'fake tail png')
        self.events.append(('tail', current['id']))
        return out

    def concat(self, videos, out_mp4, ffmpeg=None, target_seconds=None):
        paths = [Path(video) for video in videos]
        out = Path(out_mp4)
        self.concat_calls.append((paths, out))
        if self.fail_concat:
            raise RuntimeError('Injected local concat failure')
        self.case.assertTrue(all(path.is_file() for path in paths))
        out.write_bytes(b'fake concatenated mp4')
        self.durations[str(out)] = sum(self.durations[str(path)] for path in paths)
        if target_seconds is not None:
            self.case.assertAlmostEqual(self.durations[str(out)], float(target_seconds))
        return out


class BatchPipelineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='batch_tests_', dir=ROOT / '.tmp')
        self.path = Path(self.tmp.name)
        self.h = Harness(self, self.path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.tmp.cleanup)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        for name, value in (('STATE_DIR', self.h.state), ('VIDEOS_DIR', self.h.videos),
                            ('FRAMES_DIR', self.h.frames), ('ROOT', self.path)):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(module, 'ensure_dirs'))
        self.stack.enter_context(patch.object(module, 'BrowserSession', side_effect=self.h.session))
        self.stack.enter_context(patch.object(module, 'DoubaoAgent', side_effect=self.h.agent))
        self.stack.enter_context(patch.object(module, 'find_ffmpeg', return_value='fake-ffmpeg'))
        self.stack.enter_context(patch.object(module, 'extract_last_frame', side_effect=self.h.extract))
        self.stack.enter_context(patch.object(module, 'concat_videos', side_effect=self.h.concat))
        # _finalize_local imports this lazily; patch the provider, not just an alias.
        self.stack.enter_context(patch.object(video_utils, 'trim_video', side_effect=self.h.trim, create=True))
        self.cfg = {
            'accounts': {'doubao': [dict(name=name, enabled=True, profile='unused-' + name)
                                    for name in ('a', 'b', 'c')]},
            'pipeline': {'parallel_accounts': True},
            'video': {'auto_concat': True, 'later_segment_images': 'prev_frame'},
            'doubao': {'max_attempts_per_task': 3},
        }
        self.pipeline = self.make_pipeline()

    def make_pipeline(self):
        pipeline = Pipeline(self.cfg)
        pipeline.ffmpeg = 'fake-ffmpeg'
        pipeline._attempted_requests = set()
        self.h.pipeline = pipeline
        return pipeline

    def plan(self, specifications):
        scripts, tasks = [], []
        image = self.path / 'product.png'
        image.write_bytes(b'fake product image')
        for script_id, durations in specifications:
            text = f'Complete original script {script_id}: all scenes and narration are preserved.'
            total = sum(durations)
            self.h.script_texts[script_id] = text
            self.h.script_seconds[script_id] = total
            scripts.append(dict(id=script_id, text=text, total_seconds=total))
            start = 0
            for index, seconds in enumerate(durations):
                tasks.append(dict(
                    id=f'{script_id}#seg{index}', script_id=script_id,
                    seg_index=index, seg_total=len(durations), prompt=f'Original scene {index}',
                    narration=f'Complete narration for scene {index}', est_seconds=seconds,
                    target_seconds=seconds, start_seconds=start, end_seconds=start + seconds,
                    product_images=[str(image)], status='pending', account='', attempts=0,
                    video='', raw_video='', error='', request_id='', remote_ref={},
                ))
                start += seconds
        with self.pipeline.store.txn() as data:
            data.update(scripts=scripts, tasks=tasks, requests={}, assemblies={})
        return tasks

    def test_fifty_and_seventy_seconds_use_real_batches_and_accounts_four_four_one(self):
        self.plan([('script50', [15, 15, 15, 5]), ('script70', [15, 15, 15, 15, 10])])
        self.pipeline.stage_run()
        calls = self.h.calls
        self.assertEqual([len(call['task_ids']) for call in calls], [2, 2, 2, 2, 1])
        allocations = Counter()
        for call in calls:
            allocations[call['account']] += len(call['task_ids'])
        self.assertEqual(allocations, {'a': 4, 'b': 4, 'c': 1})
        self.assertEqual(self.h.opened, ['a', 'b', 'c'])
        self.assertEqual(self.h.max_active, 1)
        self.assertEqual(self.h.sent, 5)
        self.assertTrue(all(task['status'] == 'done' for task in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual([self.pipeline.accounts.quota(name)['used'] for name in ('a', 'b', 'c')], [4, 4, 1])
        self.assertEqual(self.h.durations[str(self.h.videos / 'script50_final.mp4')], 50)
        self.assertEqual(self.h.durations[str(self.h.videos / 'script70_final.mp4')], 70)
        self.assertEqual([seconds for _, _, seconds in self.h.trims],
                         [15, 15, 15, 5, 15, 15, 15, 15, 10])

    def test_one_remaining_unit_claims_a_single_video(self):
        self.plan([('script50', [15, 15, 15, 5])])
        self.pipeline.accounts.observe_remaining('a', 1)
        batch = self.pipeline._claim_batch('a', limit=2)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]['seg_index'], 0)
        self.assertEqual(self.pipeline.accounts.quota('a')['remaining'], 0)

    def test_second_runner_cannot_recover_or_submit_same_request_concurrently(self):
        self.plan([('script30',[15,15])])
        other=self.make_pipeline()
        with self.pipeline._run_lock():
            with self.assertRaisesRegex(RuntimeError,'已有流水线'):
                other.stage_run()
        self.assertEqual(self.h.sent,0)

    def test_first_account_login_failure_releases_unsent_batch_for_next_account(self):
        self.plan([('script30', [15, 15])])
        self.h.login_fail_accounts.add('a')
        self.pipeline.stage_run()
        self.assertEqual(self.h.opened, ['a', 'b'])
        self.assertEqual(self.h.max_active, 1)
        self.assertEqual(self.h.sent, 1)
        self.assertEqual([call['account'] for call in self.h.calls], ['b'])
        first = self.pipeline.accounts.quota('a')
        self.assertEqual((first['used'], first['reserved'], first['unknown']), (0, 0, 0))
        self.assertEqual(self.pipeline.accounts.status('a')['status'], 'needs_login')
        self.assertEqual(self.pipeline.accounts.quota('b')['used'], 2)
        requests = list(self.pipeline.store.snapshot()['requests'].values())
        self.assertEqual([(request['account'], request['phase']) for request in requests],
                         [('a', 'cancelled'), ('b', 'complete')])

    def test_submitted_definitive_quota_refusal_continues_on_next_account(self):
        self.plan([('script30',[15,15])])
        self.h.mode='quota_reject_a'
        self.pipeline.stage_run()
        self.assertEqual([call['account'] for call in self.h.calls],['a','b'])
        self.assertEqual([t['account'] for t in self.pipeline.store.snapshot()['tasks']],['b','b'])
        first=self.pipeline.accounts.request('a',self.h.calls[0]['context']['request_id'])
        self.assertTrue(all(e['state']=='rejected' for e in first['entries'].values()))
        self.assertEqual((self.pipeline.accounts.quota('a')['reserved'],self.pipeline.accounts.quota('a')['unknown']),(0,0))
        self.assertEqual(self.pipeline.accounts.quota('b')['used'],2)

    def test_partial_generation_then_definitive_quota_refusal_moves_only_missing_slot(self):
        self.plan([('script30',[15,15])])
        self.h.mode='partial_quota_reject_a'
        self.pipeline.stage_run()
        self.assertEqual([call['account'] for call in self.h.calls],['a','b'])
        self.assertEqual([len(call['task_ids']) for call in self.h.calls],[2,1])
        self.assertEqual([t['account'] for t in self.pipeline.store.snapshot()['tasks']],['a','b'])
        first=self.pipeline.accounts.request('a',self.h.calls[0]['context']['request_id'])
        self.assertEqual([e['state'] for e in first['entries'].values()],['charged','rejected'])
        self.assertEqual(self.pipeline.accounts.quota('b')['used'],1)

    def test_ambiguous_quota_result_keeps_slots_bound_to_original_request(self):
        self.plan([('script30',[15,15])])
        self.h.mode='quota_unknown_a'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual([call['account'] for call in self.h.calls],['a'])
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'],2)
        self.assertTrue(all(t['account']=='a' for t in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual(self.pipeline.accounts.quota('b')['used'],0)

    def test_charged_download_failure_is_not_released_with_other_refused_slot(self):
        self.plan([('script30',[15,15])])
        self.h.mode='partial_quota_download_a'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        original=self.h.calls[0]['context']['request_id']
        states=self.pipeline.accounts.request('a',original)['entries']
        self.assertEqual([entry['state'] for entry in states.values()],['charged','rejected'])
        self.assertEqual(self.pipeline.store.snapshot()['requests'][original]['task_ids'],['script30#seg0'])
        self.h.mode='success'
        self.pipeline=self.make_pipeline()
        self.pipeline.stage_run()
        self.assertEqual([(call['account'],call['resume_only']) for call in self.h.calls],
                         [('a',False),('a',True),('b',False)])
        self.assertEqual(self.h.calls[1]['task_ids'],['script30#seg0'])
        self.assertEqual(self.h.sent,2)

    def test_rejection_ledger_journal_gap_is_repaired_without_resubmitting_old_account(self):
        self.plan([('script30',[15,15])])
        batch=self.pipeline._claim_batch('a')
        rid=batch[0]['request_id']
        self.pipeline.accounts.mark_submitted('a',rid)
        proof=dict(kind='quota',definitive=True,active=False,source='latest_assistant')
        self.pipeline.accounts.release_rejected('a',rid,[t['id'] for t in batch],proof)
        self.pipeline=self.make_pipeline()
        self.pipeline.stage_run()
        self.assertEqual([call['account'] for call in self.h.calls],['b'])
        self.assertEqual(self.h.sent,1)

    def test_failed_first_slot_refund_retries_only_failed_slot_on_same_account(self):
        self.plan([('script30',[15,15])]);self.h.mode='partial_refunded'
        batch=self.pipeline._claim_batch('a');rid=batch[0]['request_id']
        self.assertTrue(self.pipeline._execute_batch(FakeAgent(self.h,'a'),'a',batch))
        first,second=self.pipeline.store.snapshot()['tasks']
        self.assertEqual((first['status'],first['account'],first['request_id']),('pending','',''))
        self.assertEqual((second['status'],second['account'],second['remote_ref']['key']),
                         ('done','a','remote-script30#seg1'))
        self.assertEqual([e['state'] for e in self.pipeline.accounts.request('a',rid)['entries'].values()],
                         ['rejected','charged'])
        self.assertEqual(self.pipeline.accounts.quota('a')['remaining'],3)
        second_video=Path(second['video']);second_bytes=second_video.read_bytes()
        self.pipeline.stage_run()
        self.assertEqual([call['task_ids'] for call in self.h.calls],
                         [['script30#seg0','script30#seg1'],['script30#seg0']])
        self.assertEqual([call['account'] for call in self.h.calls],['a','a'])
        finished=self.pipeline.store.snapshot()['tasks']
        self.assertEqual([t['remote_ref']['key'] for t in finished],
                         ['remote-script30#seg0','remote-script30#seg1'])
        self.assertEqual(Path(finished[1]['video']),second_video)
        self.assertEqual(second_video.read_bytes(),second_bytes)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'],2)

    def test_partial_refund_still_uses_accounts_four_four_one_and_final_video_order(self):
        self.plan([('script50',[15,15,15,5]),('script70',[15,15,15,15,10])])
        self.h.mode='partial_refunded';self.pipeline.stage_run()
        self.assertEqual([self.pipeline.accounts.quota(name)['used'] for name in ('a','b','c')],[4,4,1])
        self.assertEqual([call['account'] for call in self.h.calls],['a','a','a','b','b','c'])
        self.assertEqual([len(call['task_ids']) for call in self.h.calls],[2,1,2,2,2,1])
        self.assertEqual(self.h.calls[1]['task_ids'],['script50#seg0'])
        self.assertEqual(sum('script50#seg1' in call['task_ids'] and not call['resume_only'] for call in self.h.calls),1)
        self.assertEqual(self.h.opened,['a','b','c'])
        self.assertTrue(all(t['status']=='done' for t in self.pipeline.store.snapshot()['tasks']))
        ordered=next(paths for paths,out in self.h.concat_calls if out.name=='script50_final.mp4')
        self.assertEqual([path.name for path in ordered],[f'script50_seg{i}.mp4' for i in range(4)])
        self.assertEqual(self.h.durations[str(self.h.videos/'script50_final.mp4')],50)
        self.assertEqual(self.h.durations[str(self.h.videos/'script70_final.mp4')],70)

    def test_refund_without_failed_card_proof_keeps_missing_slot_unknown(self):
        self.plan([('script30',[15,15])]);self.h.mode='partial_refunded_unproven'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual([call['account'] for call in self.h.calls],['a'])
        first,second=self.pipeline.store.snapshot()['tasks']
        self.assertEqual((first['status'],first['account']),('awaiting_result','a'))
        self.assertEqual(second['status'],'done')
        quota=self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'],quota['unknown']),(1,1))
        self.assertIn('rejection_error',self.pipeline.store.snapshot()['requests'][first['request_id']])

    def test_refund_with_unsafe_continuation_does_not_release_unknown_slot(self):
        self.plan([('script30',[15,15])]);self.h.mode='partial_refunded'
        class UnsafeAgent(FakeAgent):
            def run_batch(self,*args,**kwargs):
                result=super().run_batch(*args,**kwargs);result['continuation_safe']=False;return result
        batch=self.pipeline._claim_batch('a')
        self.assertFalse(self.pipeline._execute_batch(UnsafeAgent(self.h,'a'),'a',batch))
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'],1)

    def test_refunded_first_slot_and_charged_download_failure_keep_original_success(self):
        self.plan([('script30',[15,15])]);self.h.mode='partial_refunded_download'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual([call['task_ids'] for call in self.h.calls],
                         [['script30#seg0','script30#seg1'],['script30#seg0']])
        original=self.h.calls[0]['context']['request_id']
        self.assertEqual(self.pipeline.store.snapshot()['requests'][original]['task_ids'],['script30#seg1'])
        self.assertEqual(self.pipeline.accounts.quota('a')['used'],2)
        sends=self.h.sent;self.pipeline=self.make_pipeline();self.pipeline.stage_run()
        recovered=self.h.calls[-1]
        self.assertTrue(recovered['resume_only'])
        self.assertEqual(recovered['task_ids'],['script30#seg1'])
        self.assertEqual(recovered['context']['original_task_ids'],['script30#seg0','script30#seg1'])
        self.assertEqual(recovered['context']['request_id'],original)
        self.assertEqual(self.h.sent,sends)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'],2)

    def test_refund_ledger_journal_gap_recovers_slot_one_before_slot_zero_exists(self):
        self.plan([('script30',[15,15])]);batch=self.pipeline._claim_batch('a');rid=batch[0]['request_id']
        self.pipeline.accounts.mark_submitted('a',rid,chat_url='https://example.invalid/original',
                                              baseline={'video_count':0,'known_ids':[]})
        self.pipeline.accounts.record_generated('a','script30#seg1',request_id=rid,
                                               remote_ref={'key':'remote-script30#seg1'})
        proof=dict(kind='generation_failed_refunded',definitive=True,active=False,
                   source='latest_assistant',quota_not_charged=True,failed_task_ids=['script30#seg0'])
        self.pipeline.accounts.release_failed_refunded('a',rid,['script30#seg0'],proof)
        self.assertFalse((self.h.frames/'script30_seg0_end.png').is_file())
        self.pipeline=self.make_pipeline();self.pipeline.stage_run()
        self.assertEqual([(call['account'],call['resume_only'],call['task_ids']) for call in self.h.calls],
                         [('a',True,['script30#seg1']),('a',False,['script30#seg0'])])
        self.assertEqual(self.h.calls[0]['context']['original_task_ids'],['script30#seg0','script30#seg1'])
        self.assertEqual(self.h.sent,1)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'],2)
        self.assertEqual(self.pipeline.accounts.status('a')['status'],'available')
        self.assertEqual(self.pipeline.store.snapshot()['tasks'][0]['attempts'],1)

    def test_repeated_refunded_failures_stop_at_task_retry_limit_without_charging(self):
        self.plan([('script30',[15,15])]);self.h.mode='refund_every_attempt'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual(self.h.sent,3)
        self.assertEqual([call['account'] for call in self.h.calls],['a','a','a'])
        self.assertTrue(all(t['status']=='failed' and t['attempts']==3 for t in self.pipeline.store.snapshot()['tasks']))
        quota=self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'],quota['reserved'],quota['unknown']),(0,0,0))
        sends,calls,opened=self.h.sent,len(self.h.calls),len(self.h.opened)
        self.pipeline=self.make_pipeline()
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual((self.h.sent,len(self.h.calls),len(self.h.opened)),(sends,calls,opened))
        self.assertTrue(all(t['status']=='failed' and t['attempts']==3 for t in self.pipeline.store.snapshot()['tasks']))

    def test_refund_journal_gap_counts_last_attempt_once_and_keeps_failed_across_restarts(self):
        self.plan([('script30',[15,15])]);batch=self.pipeline._claim_batch('a');rid=batch[0]['request_id']
        with self.pipeline.store.txn() as data:
            for task in data['tasks']:task['attempts']=2
        self.pipeline.accounts.mark_submitted('a',rid)
        proof=dict(kind='generation_failed_refunded',definitive=True,active=False,source='latest_assistant',
                   quota_not_charged=True,failed_task_ids=[t['id'] for t in batch])
        self.pipeline.accounts.release_failed_refunded('a',rid,[t['id'] for t in batch],proof)
        self.pipeline=self.make_pipeline();self.pipeline._recover_state();self.pipeline._recover_state()
        self.assertTrue(all(t['status']=='failed' and t['attempts']==3 for t in self.pipeline.store.snapshot()['tasks']))
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual(self.h.sent,0)

    def test_forced_replan_uses_new_task_paths_without_washing_old_charges(self):
        original = self.plan([('script30', [15, 15])])
        batch = self.pipeline._claim_batch('a')
        rid = batch[0]['request_id']
        self.pipeline.accounts.mark_submitted('a', rid, chat_url='https://example.invalid/original')
        self.pipeline.accounts.record_generated('a', original[0]['id'], request_id=rid,
                                                remote_ref={'id': 'old-generated-output'})
        old_raw = self.h.videos / 'script30_seg0.raw.mp4'
        old_raw.write_bytes(b'old generated artifact must not be adopted by a new plan')
        self.pipeline._finish(original[0]['id'], status='downloaded', raw_video=str(old_raw))
        with self.pipeline.store.txn() as data:
            data['scripts'][0].update(keyword='product', text='总时长：30秒\n口播文案：完整台词保留，不复用旧任务。')
            data['products'] = [dict(keyword='product', name='Test product',
                                     images=[str(self.path / 'product.png')])]
        before = self.pipeline.accounts.quota('a')
        self.pipeline.stage_plan(force=True)
        planned = self.pipeline.store.snapshot()
        self.assertNotEqual(planned['scripts'][0]['id'], 'script30')
        self.assertTrue({task['id'] for task in original}.isdisjoint(task['id'] for task in planned['tasks']))
        self.assertTrue(all(task['raw_video'] == '' and task['video'] == '' for task in planned['tasks']))
        self.assertEqual(planned['requests'], {})
        self.pipeline._recover_state()
        self.assertTrue(all(task['status'] == 'pending' for task in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual(old_raw.read_bytes(), b'old generated artifact must not be adopted by a new plan')
        after = self.pipeline.accounts.quota('a')
        self.assertEqual((after['used'], after['reserved'], after['unknown'], after['remaining']),
                         (before['used'], before['reserved'], before['unknown'], before['remaining']))
        self.assertEqual(after['used'], 1)

    def test_legacy_dashboard_count_does_not_truncate_new_plan(self):
        with self.pipeline.store.txn() as data:
            data['requested_script_count']=1
            data['scripts']=[dict(id=f'script{i}',keyword='product',
                text=f'总时长：15秒\n口播文案：这是第{i}个完整脚本。') for i in range(3)]
            data['products']=[dict(keyword='product',name='Test product',images=[])]
            data['tasks']=[]
        self.pipeline.stage_plan()
        snapshot=self.pipeline.store.snapshot()
        self.assertEqual({t['script_id'] for t in snapshot['tasks']},{'script0','script1','script2'})

    def test_generated_callbacks_charge_even_when_no_download_is_available(self):
        self.plan([('script30', [15, 15])])
        self.h.mode = 'download_failure'
        batch = self.pipeline._claim_batch('a')
        self.pipeline._execute_batch(FakeAgent(self.h, 'a'), 'a', batch)
        quota = self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'], quota['reserved'], quota['remaining']), (2, 0, 2))
        self.assertEqual(self.h.generated_balances, [1, 2])
        self.assertEqual(list(self.h.videos.glob('*.mp4')), [])
        self.assertFalse(any(task['status'] == 'done' for task in self.pipeline.store.snapshot()['tasks']))

    def test_partial_batch_only_charges_generated_output_and_preserves_unknown(self):
        self.plan([('script30', [15, 15])])
        self.h.mode = 'partial'
        batch = self.pipeline._claim_batch('a')
        self.pipeline._execute_batch(FakeAgent(self.h, 'a'), 'a', batch)
        quota = self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'], quota['unknown'], quota['remaining']), (1, 1, 2))
        tasks = self.pipeline.store.snapshot()['tasks']
        self.assertEqual(tasks[0]['status'], 'done')
        self.assertNotEqual(tasks[1]['status'], 'done')

    def test_partial_batch_recovery_reuses_request_and_only_charges_missing_output(self):
        self.plan([('script30', [15, 15])])
        self.h.mode = 'partial'
        batch = self.pipeline._claim_batch('a')
        request_id = batch[0]['request_id']
        self.pipeline._execute_batch(FakeAgent(self.h, 'a'), 'a', batch)
        self.assertEqual(self.h.sent, 1)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 1)
        self.h.mode = 'success'
        self.pipeline = self.make_pipeline()
        self.pipeline.stage_run()
        resumed = self.h.calls[-1]
        self.assertTrue(resumed['resume_only'])
        self.assertEqual(resumed['context']['request_id'], request_id)
        self.assertEqual(resumed['context']['remote_refs']['script30#seg0'],
                         {'id': 'remote-script30#seg0', 'key': 'remote-script30#seg0'})
        self.assertEqual(self.h.sent, 1)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 2)
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'], 0)
        first = self.h.videos / 'script30_seg0.mp4'
        self.assertEqual(sum(out == first for _, out, _ in self.h.trims), 1)
        self.assertEqual(self.pipeline.store.snapshot()['assemblies']['script30']['status'], 'done')

    def test_known_unsubmitted_failure_retries_with_new_request_and_no_extra_charge(self):
        self.plan([('script30', [15, 15])])
        self.h.mode = 'unsubmitted_once'
        self.pipeline.stage_run()
        self.assertEqual(len(self.h.calls), 2)
        self.assertNotEqual(self.h.calls[0]['context']['request_id'],
                            self.h.calls[1]['context']['request_id'])
        self.assertEqual(self.h.sent, 1)
        quota = self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'], quota['reserved'], quota['unknown']), (2, 0, 0))
        phases = [request['phase'] for request in self.pipeline.store.snapshot()['requests'].values()]
        self.assertEqual(phases, ['cancelled', 'complete'])

    def test_pre_submit_setup_errors_are_bounded_per_run_and_do_not_consume_cloud_retries(self):
        self.plan([('script30',[15,15])]);self.h.mode='preflight_failure'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        tasks=self.pipeline.store.snapshot()['tasks']
        self.assertTrue(all(t['status']=='pending' and t['attempts']==0 and t['cloud_attempts']==0
                            and t['local_attempts']==3 and t['local_blocked'] for t in tasks))
        self.assertEqual(self.h.sent,0);self.assertEqual(len(self.h.calls),3)
        quota=self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'],quota['reserved'],quota['unknown']),(0,0,0))
        self.h.mode='success';self.pipeline=self.make_pipeline();self.pipeline.stage_run()
        self.assertEqual(self.h.sent,1)
        self.assertTrue(all(t['status']=='done' and t['attempts']==0 for t in self.pipeline.store.snapshot()['tasks']))

    def test_legacy_four_done_five_failed_resume_only_remaining_five_without_force(self):
        self.plan([('script50',[15,15,15,5]),('script70',[15,15,15,15,10])])
        batch=self.pipeline._claim_batch('a');self.pipeline._execute_batch(FakeAgent(self.h,'a'),'a',batch)
        self.pipeline._local_blocked_tasks.update({'script50#seg2','script50#seg3'})
        batch=self.pipeline._claim_batch('b');self.assertEqual(batch[0]['script_id'],'script70')
        self.pipeline._execute_batch(FakeAgent(self.h,'b'),'b',batch);self.pipeline._local_blocked_tasks.clear()
        done={t['id']:dict(t) for t in self.pipeline.store.snapshot()['tasks'] if t['status']=='done'}
        self.assertEqual(len(done),4)
        self.h.mode='preflight_failure'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        with self.pipeline.store.txn() as data:
            for task in data['tasks']:
                if task['id'] in done:continue
                task.update(status='failed',attempts=0 if task['seg_index']==4 else 3,
                            error='前置分段永久失败' if task['seg_index']==4 else '找不到视频时长控件，发送前已停止，避免默认10秒浪费次数')
                for key in ('cloud_attempts','local_attempts','local_blocked','failure_phase'):task.pop(key,None)
        calls=len(self.h.calls);self.h.mode='success';self.pipeline=self.make_pipeline()
        self.pipeline._recover_state()
        recovered=self.pipeline.store.snapshot()['tasks']
        self.assertEqual({t['id']:t for t in recovered if t['id'] in done},done)
        self.assertTrue(all(t['status']=='pending' and t['attempts']==0 for t in recovered if t['id'] not in done))
        self.assertEqual([t['local_attempts'] for t in recovered if t['id'] not in done and t['seg_index']!=4],[3,3,3,3])
        self.pipeline.stage_run()
        sent_ids=[tid for call in self.h.calls[calls:] if not call['resume_only'] for tid in call['task_ids']]
        self.assertEqual(len(sent_ids),5);self.assertTrue(set(sent_ids).isdisjoint(done))
        self.assertTrue(all(t['status']=='done' for t in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual([self.pipeline.accounts.quota(name)['used'] for name in ('a','b','c')],[4,4,1])

    def test_local_retry_migration_preserves_real_refunded_cloud_attempt_limit(self):
        self.plan([('script30',[15,15])]);self.h.mode='refund_every_attempt'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        ids=[t['id'] for t in self.pipeline.store.snapshot()['tasks']];rid='legacy-local-cancelled'
        self.pipeline.accounts.reserve('a',ids,rid);self.pipeline.accounts.release_unsubmitted('a',rid)
        with self.pipeline.store.txn() as data:
            data['requests'][rid]=dict(request_id=rid,account='a',task_ids=ids,phase='cancelled',submitted=False)
            for task in data['tasks']:
                task.update(attempts=4,error='Setup failed before send');task.pop('cloud_attempts',None)
        self.pipeline=self.make_pipeline();self.pipeline._recover_state()
        self.assertTrue(all(t['status']=='failed' and t['attempts']==3 and t['cloud_attempts']==3
                            and t['local_attempts']==1 for t in self.pipeline.store.snapshot()['tasks']))
        sends=self.h.sent
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        self.assertEqual(self.h.sent,sends)

    def test_local_error_text_cannot_reset_an_actual_submitted_unknown_request(self):
        self.plan([('script30',[15,15])]);self.h.mode='unknown'
        with self.assertRaises(RuntimeError):self.pipeline.stage_run()
        rid=self.h.calls[0]['context']['request_id']
        with self.pipeline.store.txn() as data:
            for task in data['tasks']:
                task.update(status='failed',attempts=3,error='找不到视频时长控件，发送前已停止')
        self.pipeline=self.make_pipeline();self.pipeline._recover_state()
        self.assertTrue(all(t['status']=='awaiting_result' and t['request_id']==rid for t in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'],2)
        self.assertEqual(self.h.sent,1)

    def test_legacy_failed_without_unsent_evidence_is_not_automatically_requeued(self):
        self.plan([('script30',[15,15])])
        with self.pipeline.store.txn() as data:
            data['tasks'][0].update(status='failed',attempts=3,error='找不到视频时长控件，发送前已停止')
            data['tasks'][1].update(status='failed',error='前置分段永久失败')
        self.pipeline._recover_state()
        self.assertTrue(all(t['status']=='failed' for t in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual(self.h.sent,0)

    def test_unknown_request_restarts_in_resume_only_mode_without_sending_again(self):
        self.plan([('script30', [15, 15])])
        self.h.mode = 'unknown'
        with self.assertRaises(RuntimeError):
            self.pipeline.stage_run()
        first = self.h.calls[0]
        self.assertEqual(self.h.sent, 1)
        self.assertEqual(self.pipeline.accounts.quota('a')['unknown'], 2)
        self.h.mode = 'success'
        self.pipeline = self.make_pipeline()
        self.pipeline._recover_state()
        self.pipeline.stage_run()
        resumed = self.h.calls[-1]
        self.assertTrue(resumed['resume_only'])
        self.assertEqual(resumed['context']['request_id'], first['context']['request_id'])
        self.assertTrue(resumed['context']['chat_url'])
        self.assertEqual(resumed['context']['baseline'], {'video_count': 0, 'known_ids': []})
        self.assertEqual(self.h.sent, 1)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 2)

    def test_empty_remote_placeholders_are_not_generated_or_charged(self):
        self.plan([('script30',[15,15])])
        self.h.mode='empty_refs'
        batch=self.pipeline._claim_batch('a')
        self.assertFalse(self.pipeline._execute_batch(FakeAgent(self.h,'a'),'a',batch))
        quota=self.pipeline.accounts.quota('a')
        self.assertEqual((quota['used'],quota['unknown']),(0,2))
        self.assertFalse(any(t['status']=='done' for t in self.pipeline.store.snapshot()['tasks']))

    def test_ledger_submission_without_task_journal_recovers_without_new_send(self):
        tasks = self.plan([('script30', [15, 15])])
        ids = [task['id'] for task in tasks]
        self.pipeline.accounts.reserve('a', ids, 'request-before-journal')
        self.pipeline.accounts.mark_submitted('a', 'request-before-journal',
                                              chat_url='https://example.invalid/original',
                                              baseline={'video_count': 0, 'known_ids': []})
        self.pipeline = self.make_pipeline()
        self.pipeline.stage_run()
        self.assertEqual(len(self.h.calls), 1)
        self.assertTrue(self.h.calls[0]['resume_only'])
        self.assertEqual(self.h.calls[0]['context']['request_id'], 'request-before-journal')
        self.assertEqual(self.h.sent, 0)
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 2)

    def test_next_batch_starts_only_after_previous_tail_is_published(self):
        self.plan([('script50', [15, 15, 15, 5])])
        self.pipeline.stage_run()
        batches = [index for index, event in enumerate(self.h.events) if event[0] == 'batch']
        boundary = self.h.events.index(('tail', 'script50#seg1'))
        self.assertLess(batches[0], boundary)
        self.assertLess(boundary, batches[1])
        self.assertTrue((self.h.frames / 'script50_seg1_end.png').is_file())

    def test_tail_failure_recovery_only_retries_local_media_work(self):
        self.plan([('script30', [15, 15])])
        self.h.fail_extract = True
        with self.assertRaises(RuntimeError):
            self.pipeline.stage_run()
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 2)
        current = self.pipeline.store.snapshot()['tasks']
        self.assertEqual(current[0]['status'], 'local_failed')
        self.assertTrue(Path(current[0]['raw_video']).is_file())
        sent, calls, opened = self.h.sent, len(self.h.calls), len(self.h.opened)
        self.h.fail_extract = False
        self.pipeline = self.make_pipeline()
        self.pipeline.stage_run()
        self.assertEqual((self.h.sent, len(self.h.calls), len(self.h.opened)), (sent, calls, opened))
        self.assertEqual(self.pipeline.accounts.quota('a')['used'], 2)
        self.assertTrue(all(task['status'] == 'done' for task in self.pipeline.store.snapshot()['tasks']))

    def test_concat_failure_retries_locally_without_more_generation_or_browser(self):
        self.plan([('script30', [15, 15])])
        self.h.fail_concat = True
        with self.assertRaises(RuntimeError):
            self.pipeline.stage_run()
        self.assertTrue(all(task['status'] == 'done' for task in self.pipeline.store.snapshot()['tasks']))
        self.assertEqual(self.pipeline.store.snapshot()['assemblies']['script30']['status'], 'failed')
        generated_calls, sends, opened, trims = len(self.h.calls), self.h.sent, len(self.h.opened), len(self.h.trims)
        self.h.fail_concat = False
        self.pipeline = self.make_pipeline()
        self.pipeline.stage_run()
        self.assertEqual((len(self.h.calls), self.h.sent, len(self.h.opened), len(self.h.trims)),
                         (generated_calls, sends, opened, trims))
        self.assertEqual(self.pipeline.store.snapshot()['assemblies']['script30']['status'], 'done')
        self.assertEqual(self.h.durations[str(self.h.videos / 'script30_final.mp4')], 30)


if __name__ == '__main__':
    unittest.main(verbosity=2)
