"""Quota ledger regression tests. All state is temporary; no browser or network."""
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doubao_automation.account_manager import AccountManager  # noqa: E402
from doubao_automation.state import JsonStore  # noqa: E402


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class QuotaTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='quota_tests_', dir=ROOT / '.tmp')
        self.path = Path(self.tmp.name) / 'accounts.json'
        self.clock = Clock()
        self.manager = self.new_manager()

    def tearDown(self):
        self.tmp.cleanup()

    def new_manager(self):
        return AccountManager(JsonStore(self.path), ['a', 'b'], daily_limit=4,
                              clock=self.clock)

    def charge(self, task_id, request_id, manager=None):
        manager = manager or self.manager
        manager.reserve('a', [task_id], request_id)
        manager.mark_submitted('a', request_id, chat_url='https://example.invalid/chat',
                               baseline={'video_count': 0})
        manager.record_generated('a', task_id, request_id=request_id,
                                 remote_ref={'id': 'remote-' + task_id})

    def test_public_fields_and_legacy_usage_are_explicitly_estimated(self):
        legacy = Path(self.tmp.name) / 'legacy.json'
        with JsonStore(legacy).txn() as data:
            data['accounts'] = {'legacy': {'status': 'available', 'note': '', 'date': ''}}
        manager = AccountManager(JsonStore(legacy), ['legacy'], clock=self.clock)
        quota = manager.quota('legacy')
        self.assertTrue(set(('date', 'limit', 'used', 'reserved', 'unknown',
                             'remaining', 'estimated', 'blocked_reason')).issubset(quota))
        self.assertEqual(quota['date'], '2026-10-30')
        self.assertEqual(quota['limit'], 4)
        self.assertIs(quota['estimated'], True)

    def test_two_videos_reserve_two_units_not_one_request_unit(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        quota = self.manager.quota('a')
        self.assertEqual((quota['used'], quota['reserved'], quota['unknown'],
                          quota['remaining']), (0, 2, 0, 2))

    def test_repeated_reservation_is_idempotent_across_manager_instances(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.new_manager().reserve('a', ['s#0', 's#1'], 'request-1')
        self.assertEqual(self.manager.quota('a')['reserved'], 2)
        self.assertEqual(self.manager.quota('a')['remaining'], 2)

    def test_partial_generation_charges_only_generated_video(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.mark_submitted('a', 'request-1', baseline={'video_count': 0})
        self.manager.record_generated('a', 's#0', request_id='request-1',
                                      remote_ref={'id': 'remote-0'})
        self.manager.mark_unknown('a', 'request-1', task_ids=['s#1'],
                                  note='The submitted result is not yet reconciled')
        quota = self.manager.quota('a')
        self.assertEqual((quota['used'], quota['reserved'], quota['unknown'],
                          quota['remaining']), (1, 0, 1, 2))

    def test_generation_is_charged_before_any_local_download(self):
        self.charge('s#0', 'request-1')
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 1)
        self.assertEqual(quota['remaining'], 3)
        self.assertEqual(list(Path(self.tmp.name).glob('*.mp4')), [])

    def test_duplicate_generated_callback_does_not_charge_twice(self):
        self.charge('s#0', 'request-1')
        self.new_manager().record_generated('a', 's#0', request_id='request-1',
                                            remote_ref={'id': 'remote-s#0'})
        self.assertEqual(self.manager.quota('a')['used'], 1)
        self.assertEqual(self.manager.quota('a')['remaining'], 3)

    def test_four_generated_videos_block_a_fifth(self):
        for i in range(4):
            self.charge('s#' + str(i), 'request-' + str(i))
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#4'], 'request-4')
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 4)
        self.assertEqual(quota['remaining'], 0)
        self.assertTrue(quota['blocked_reason'])

    def test_insufficient_batch_is_rejected_without_partial_reservation(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.reserve('a', ['s#2'], 'request-2')
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#3', 's#4'], 'request-3')
        self.assertEqual(self.manager.quota('a')['reserved'], 3)
        self.assertEqual(self.manager.quota('a')['remaining'], 1)

    def test_explicitly_unsubmitted_reservations_can_be_released(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.release_unsubmitted('a', 'request-1', task_ids=['s#1'])
        self.assertEqual(self.manager.quota('a')['reserved'], 1)
        self.assertEqual(self.manager.quota('a')['remaining'], 3)
        self.manager.release_unsubmitted('a', 'request-1')
        self.assertEqual(self.manager.quota('a')['remaining'], 4)

    def test_submitted_unknown_and_charged_units_cannot_be_released(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.mark_submitted('a', 'request-1', baseline={'video_count': 0})
        self.manager.release_unsubmitted('a', 'request-1')
        self.assertEqual(self.manager.quota('a')['remaining'], 2)
        self.manager.record_generated('a', 's#0', request_id='request-1')
        self.manager.mark_unknown('a', 'request-1', task_ids=['s#1'])
        self.manager.release_unsubmitted('a', 'request-1')
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 1)
        self.assertEqual(quota['unknown'], 1)
        self.assertEqual(quota['remaining'], 2)

    def test_platform_observation_marks_balance_verified(self):
        self.manager.observe_remaining('a', 1)
        quota = self.manager.quota('a')
        self.assertIs(quota['estimated'], False)
        self.assertEqual(quota['remaining'], 1)
        self.manager.reserve('a', ['s#0'], 'request-1')
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#1'], 'request-2')

    def test_unknown_result_persists_submission_even_if_callback_journal_was_lost(self):
        self.manager.reserve('a', ['s#0'], 'request-1')
        self.manager.mark_unknown('a', 'request-1')
        restarted = self.new_manager()
        self.assertTrue(restarted.request('a', 'request-1')['submitted'])
        self.assertTrue(restarted.find_task('s#0')['request']['submitted'])
        with self.assertRaises(ValueError):
            restarted.reserve('a', ['s#1'], 'request-2')

    def test_cancelled_request_requires_new_attempt_and_fresh_reservation(self):
        self.manager.reserve('a', ['s#0'], 'request-1')
        self.manager.release_unsubmitted('a', 'request-1')
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#0'], 'request-1')
        self.manager.reserve('a', ['s#0'], 'request-2')
        self.assertEqual(self.manager.quota('a')['reserved'], 1)

    def test_repeated_observation_does_not_double_count_known_generation(self):
        self.manager.observe_remaining('a', 4)
        self.charge('s#0', 'request-1')
        self.manager.observe_remaining('a', 3)
        self.manager.observe_remaining('a', 3)
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 1)
        self.assertEqual(quota['remaining'], 3)
        self.assertIs(quota['estimated'], False)

    def test_stale_observation_cannot_refill_external_usage_after_our_generation(self):
        self.manager.observe_remaining('a',2)
        self.manager.reserve('a',['s#0','s#1'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.record_generated('a','s#0',request_id='request-1')
        self.manager.record_generated('a','s#1',request_id='request-1')
        self.manager.observe_remaining('a',2)
        quota=self.manager.quota('a')
        self.assertEqual((quota['used'],quota['remaining']),(4,0))

    def test_stale_positive_balance_does_not_clear_definitive_same_day_exhaustion(self):
        self.manager.observe_remaining('a',0)
        self.manager.observe_remaining('a',4)
        self.manager.mark_available_after_login('a')
        self.assertEqual(self.manager.quota('a')['remaining'],0)

    def test_explicit_rejection_releases_only_uncharged_slots(self):
        self.manager.reserve('a',['s#0','s#1'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.record_generated('a','s#0',request_id='request-1')
        proof=dict(kind='quota',definitive=True,active=False,source='latest_assistant')
        released=self.manager.release_rejected('a','request-1',['s#0','s#1'],proof)
        self.assertEqual(released,['s#1'])
        self.assertEqual((self.manager.quota('a')['used'],self.manager.quota('a')['unknown']),(1,0))
        self.assertIsNone(self.manager.find_task('s#1'))
        self.manager.reserve('b',['s#1'],'request-2')
        self.assertEqual(self.manager.quota('b')['reserved'],1)

    def test_uncertain_or_active_rejection_cannot_release_submitted_slots(self):
        self.manager.reserve('a',['s#0'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.mark_unknown('a','request-1')
        for proof in ({},dict(kind='quota',definitive=False,active=False,source='latest_assistant'),
                      dict(kind='quota',definitive=True,active=True,source='latest_assistant')):
            with self.assertRaises(ValueError):
                self.manager.release_rejected('a','request-1',['s#0'],proof)
        self.assertEqual(self.manager.quota('a')['unknown'],1)

    @staticmethod
    def refunded_proof(*task_ids):
        return dict(kind='generation_failed_refunded',definitive=True,active=False,
                    source='latest_assistant',quota_not_charged=True,
                    failed_task_ids=list(task_ids))

    def test_failed_first_slot_refunds_without_reassigning_successful_second_slot(self):
        self.manager.reserve('a',['s#0','s#1'],'request-1')
        self.manager.mark_submitted('a','request-1')
        success_ref={'url':'https://example.invalid/successful-slot-1.mp4','key':'slot-1'}
        self.manager.record_generated('a','s#1',request_id='request-1',remote_ref=success_ref)
        self.manager.mark_unknown('a','request-1',task_ids=['s#0'])
        released=self.manager.release_failed_refunded('a','request-1',['s#0'],self.refunded_proof('s#0'))
        self.assertEqual(released,['s#0'])
        self.assertIsNone(self.manager.find_task('s#0'))
        successful=self.manager.find_task('s#1')
        self.assertEqual((successful['task_id'],successful['request_id'],successful['state']),
                         ('s#1','request-1','charged'))
        self.assertEqual(successful['remote_ref'],success_ref)
        quota=self.manager.quota('a')
        self.assertEqual((quota['used'],quota['unknown'],quota['remaining']),(1,0,3))
        self.assertEqual(list(Path(self.tmp.name).glob('*.mp4')),[])

    def test_refunded_task_can_only_start_a_new_request_and_old_charge_callback_is_rejected(self):
        self.manager.reserve('a',['s#0'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.release_failed_refunded('a','request-1',['s#0'],self.refunded_proof('s#0'))
        with self.assertRaises(ValueError):self.manager.reserve('a',['s#0'],'request-1')
        self.manager.reserve('a',['s#0'],'request-2')
        with self.assertRaises(ValueError):
            self.manager.record_generated('a','s#0',request_id='request-1',remote_ref={'key':'late-old-slot'})
        self.manager.mark_submitted('a','request-2')
        self.manager.record_generated('a','s#0',request_id='request-2',remote_ref={'key':'retry-success'})
        self.assertEqual((self.manager.quota('a')['used'],self.manager.quota('a')['remaining']),(1,3))
        self.assertEqual(self.manager.find_task('s#0')['request_id'],'request-2')

    def test_refund_release_is_idempotent_across_restart_and_never_releases_charged_success(self):
        self.manager.reserve('a',['s#0','s#1'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.record_generated('a','s#1',request_id='request-1',remote_ref={'key':'real-success'})
        proof=self.refunded_proof('s#0','s#1')
        self.assertEqual(self.manager.release_failed_refunded('a','request-1',['s#0','s#1'],proof),['s#0'])
        before=self.manager.quota('a')
        restarted=self.new_manager()
        restarted.release_failed_refunded('a','request-1',['s#0','s#1'],proof)
        self.assertEqual(restarted.quota('a'),before)
        request=restarted.request('a','request-1')
        self.assertEqual(len(request['entries']),2)
        self.assertEqual(request['entries']['s#1']['state'],'charged')
        self.assertEqual(request['entries']['s#1']['remote_ref'],{'key':'real-success'})
        self.assertIsNone(restarted.find_task('s#0'))

    def test_refund_requires_latest_complete_refund_and_failed_card_evidence_without_mutation(self):
        self.manager.reserve('a',['s#0'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.mark_unknown('a','request-1')
        valid=self.refunded_proof('s#0')
        invalid=[None,{},dict(valid,kind='quota'),dict(valid,definitive=False),
                 dict(valid,active=True),dict(valid,source='previous_assistant'),
                 dict(valid,quota_not_charged=False),dict(valid,failed_task_ids=[]),
                 dict(valid,failed_task_ids=['different-slot'])]
        for missing in ('kind','definitive','active','source','quota_not_charged','failed_task_ids'):
            proof=dict(valid);del proof[missing];invalid.append(proof)
        before=JsonStore(self.path).snapshot()
        for proof in invalid:
            with self.subTest(proof=proof):
                with self.assertRaises(ValueError):
                    self.manager.release_failed_refunded('a','request-1',['s#0'],proof)
                self.assertEqual(JsonStore(self.path).snapshot(),before)
        self.assertEqual(self.manager.quota('a')['unknown'],1)

    def test_refund_cannot_release_another_requests_slot_or_a_task_not_in_failed_cards(self):
        self.manager.reserve('a',['s#0'],'request-1')
        self.manager.reserve('a',['s#1'],'request-2')
        self.manager.mark_submitted('a','request-1')
        self.manager.mark_submitted('a','request-2')
        before=JsonStore(self.path).snapshot()
        for request_id,task_ids,proof in (
                ('request-1',['s#1'],self.refunded_proof('s#1')),
                ('request-1',['not-a-reserved-task'],self.refunded_proof('not-a-reserved-task')),
                ('request-1',['s#0'],self.refunded_proof('s#1'))):
            with self.subTest(request_id=request_id,task_ids=task_ids):
                with self.assertRaises(ValueError):
                    self.manager.release_failed_refunded('a',request_id,task_ids,proof)
                self.assertEqual(JsonStore(self.path).snapshot(),before)
        self.assertEqual(self.manager.quota('a')['unknown'],2)

    def test_refund_only_selected_failure_leaves_other_unknown_slot_held(self):
        self.manager.reserve('a',['s#0','s#1'],'request-1')
        self.manager.mark_submitted('a','request-1')
        self.manager.mark_unknown('a','request-1')
        self.assertEqual(self.manager.release_failed_refunded(
            'a','request-1',['s#0'],self.refunded_proof('s#0','s#1')),['s#0'])
        quota=self.manager.quota('a')
        self.assertEqual((quota['used'],quota['unknown'],quota['remaining']),(0,1,3))
        self.assertEqual(self.manager.find_task('s#1')['state'],'unknown')
        with self.assertRaises(ValueError):self.manager.reserve('a',['s#0'],'request-2')

    def test_inflight_platform_observation_does_not_double_charge_late_callbacks(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.mark_submitted('a', 'request-1', baseline={'video_count': 0})
        # The platform has already deducted this batch, while its generated
        # artifacts have not yet been reconciled by the local callback.
        self.manager.observe_remaining('a', 2)
        self.manager.record_generated('a', 's#0', request_id='request-1',
                                      remote_ref={'id': 'remote-0'})
        self.manager.record_generated('a', 's#1', request_id='request-1',
                                      remote_ref={'id': 'remote-1'})
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 2)
        self.assertEqual(quota['remaining'], 2)
        self.assertEqual(quota['reserved'] + quota['unknown'], 0)
        self.assertIs(quota['estimated'], False)

    def test_login_and_generic_status_changes_do_not_refill_used_quota(self):
        for i in range(4):
            self.charge('s#' + str(i), 'request-' + str(i))
        self.manager.mark_needs_login('a')
        self.manager.mark_available_after_login('a')
        self.manager._set('a', 'available')
        quota = self.manager.quota('a')
        self.assertEqual(quota['used'], 4)
        self.assertEqual(quota['remaining'], 0)
        self.assertNotEqual(self.manager.pick(), 'a')

    def test_login_does_not_enable_explicitly_disabled_account(self):
        self.manager.mark_disabled('a', 'disabled by operator')
        self.manager.mark_available_after_login('a')
        self.assertEqual(self.manager.status('a')['status'], 'disabled')
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#0'], 'request-1')

    def test_daily_reset_preserves_login_and_disabled_states(self):
        self.manager.mark_needs_login('a')
        self.manager.mark_disabled('b')
        self.clock.value += timedelta(days=1)
        self.manager.reset_daily()
        self.assertEqual(self.manager.status('a')['status'], 'needs_login')
        self.assertEqual(self.manager.status('b')['status'], 'disabled')
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['s#0'], 'request-1')

    def test_business_day_changes_at_china_midnight_not_utc_midnight(self):
        self.clock.value = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)
        self.assertEqual(self.manager.quota('a')['date'], '2026-10-30')
        self.clock.value += timedelta(minutes=2)
        self.assertEqual(self.manager.quota('a')['date'], '2026-10-31')

    def test_long_lived_manager_reads_new_day_without_explicit_reset(self):
        self.clock.value = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)
        self.manager.mark_exhausted('a')
        self.manager.mark_disabled('b')
        self.assertEqual(self.manager.status('a')['status'], 'exhausted')
        self.assertFalse(self.manager.any_available())
        self.clock.value += timedelta(minutes=2)
        quota = self.manager.quota('a')
        self.assertEqual((quota['date'], quota['used'], quota['remaining']), ('2026-10-31', 0, 4))
        self.assertEqual(self.manager.status('a')['status'], 'available')
        self.assertEqual(self.manager.status('b')['status'], 'disabled')
        self.assertTrue(self.manager.any_available())

    def test_yesterdays_unknown_request_still_blocks_unsafe_resubmission(self):
        self.clock.value = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)
        self.manager.reserve('a', ['s#0'], 'old-request')
        self.manager.mark_submitted('a', 'old-request')
        self.manager.mark_unknown('a', 'old-request')
        self.clock.value += timedelta(minutes=2)
        quota = self.manager.quota('a')
        self.assertEqual((quota['used'], quota['unknown']), (0, 1))
        with self.assertRaises(ValueError):
            self.manager.reserve('a', ['new-task'], 'new-request')
        self.assertEqual(self.manager.find_task('s#0')['state'], 'unknown')

    def test_cross_day_pending_request_is_not_reserved_or_charged_again_today(self):
        self.clock.value = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)
        yesterday = self.clock.value
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        self.manager.mark_submitted('a', 'request-1', baseline={'video_count': 0})
        self.manager.mark_unknown('a', 'request-1')
        self.clock.value += timedelta(minutes=2)
        manager = self.new_manager()
        before = manager.quota('a')
        manager.reserve('a', ['s#0', 's#1'], 'request-1')
        after = manager.quota('a')
        for key in ('used', 'reserved', 'unknown', 'remaining'):
            self.assertEqual(after[key], before[key], key)
        manager.record_generated('a', 's#0', request_id='request-1')
        self.assertEqual(manager.quota('a')['used'], before['used'])
        self.clock.value = yesterday
        old_quota = manager.quota('a')
        self.assertEqual(old_quota['used'], 1)
        self.assertEqual(old_quota['unknown'], 1)

    def test_reservation_survives_account_manager_restart(self):
        self.manager.reserve('a', ['s#0', 's#1'], 'request-1')
        restarted = self.new_manager()
        self.assertEqual(restarted.quota('a')['reserved'], 2)
        restarted.mark_submitted('a', 'request-1')
        restarted.record_generated('a', 's#0', request_id='request-1')
        self.assertEqual(self.manager.quota('a')['used'], 1)
        self.assertEqual(self.manager.quota('a')['remaining'], 2)

    def test_concurrent_reservations_never_exceed_four(self):
        managers = [self.new_manager() for _ in range(8)]
        barrier = threading.Barrier(len(managers))

        def reserve(index):
            barrier.wait(timeout=10)
            try:
                managers[index].reserve('a', ['task-' + str(index)], 'request-' + str(index))
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=len(managers)) as executor:
            accepted = list(executor.map(reserve, range(len(managers))))
        self.assertEqual(sum(accepted), 4)
        self.assertEqual(self.manager.quota('a')['reserved'], 4)
        self.assertEqual(self.manager.quota('a')['remaining'], 0)

    def test_concurrent_duplicate_generation_callbacks_charge_once(self):
        self.manager.reserve('a', ['s#0'], 'request-1')
        self.manager.mark_submitted('a', 'request-1')
        managers = [self.new_manager() for _ in range(6)]
        barrier = threading.Barrier(len(managers))

        def generated(index):
            barrier.wait(timeout=10)
            managers[index].record_generated('a', 's#0', request_id='request-1',
                                              remote_ref={'id': 'same-remote-video'})

        with ThreadPoolExecutor(max_workers=len(managers)) as executor:
            list(executor.map(generated, range(len(managers))))
        self.assertEqual(self.manager.quota('a')['used'], 1)
        self.assertEqual(self.manager.quota('a')['remaining'], 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
