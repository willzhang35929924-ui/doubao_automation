"""Atomic state replacements remain locked and retry only Windows sharing errors."""
import errno
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from doubao_automation.state import JsonStore


def windows_error(code):
    error=PermissionError(errno.EACCES,'simulated Windows file conflict')
    error.winerror=code
    return error


class StateRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.tmp')
        self.path=Path(self.tmp.name)/'state.json'
        self.store=JsonStore(self.path)
        self.store.reset({'n':0})

    def tearDown(self):
        self.tmp.cleanup()

    def test_temporary_access_or_sharing_error_retries_same_snapshot(self):
        real_replace=os.replace
        for code in (5,32):
            with self.subTest(winerror=code):
                errors=[windows_error(code),windows_error(code)]
                def replace(source,target):
                    if errors:raise errors.pop(0)
                    return real_replace(source,target)
                with patch('doubao_automation.state.os.replace',side_effect=replace) as replacement, \
                        patch('doubao_automation.state.time.sleep') as sleep:
                    with self.store.txn() as data:data['n']=code
                self.assertEqual(self.store.snapshot()['n'],code)
                self.assertEqual(replacement.call_count,3)
                self.assertEqual(len({args.args[0] for args in replacement.call_args_list}),1)
                self.assertEqual(sleep.call_args_list,[call(0.01),call(0.02)])
                self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_retry_is_bounded_and_final_original_error_is_raised(self):
        errors=[windows_error(5) for _ in range(len(self.store._replace_retry_delays)+1)]
        with patch('doubao_automation.state.os.replace',side_effect=errors) as replacement, \
                patch('doubao_automation.state.time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                with self.store.txn() as data:data['n']=100
        self.assertIs(caught.exception,errors[-1])
        self.assertEqual(replacement.call_count,8)
        self.assertEqual(sleep.call_count,7)
        self.assertLess(sum(args.args[0] for args in sleep.call_args_list),2)
        self.assertEqual(self.store.snapshot(),{'n':0})
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_other_errors_are_not_retried(self):
        errors=[FileNotFoundError(errno.ENOENT,'missing file'),OSError(errno.ENOSPC,'disk full'),
                PermissionError(errno.EACCES,'non-Windows permission failure'),
                windows_error(33),windows_error(87)]
        for error in errors:
            with self.subTest(error=repr(error),winerror=getattr(error,'winerror',None)):
                with patch('doubao_automation.state.os.replace',side_effect=error) as replacement, \
                        patch('doubao_automation.state.time.sleep') as sleep:
                    with self.assertRaises(OSError) as caught:
                        with self.store.txn() as data:data['n']=100
                self.assertIs(caught.exception,error)
                replacement.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(self.store.snapshot(),{'n':0})
                self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_cleanup_failure_does_not_mask_original_replace_failure(self):
        original=windows_error(5)
        with patch('doubao_automation.state.os.replace',side_effect=original), \
                patch('doubao_automation.state.time.sleep'), \
                patch('doubao_automation.state.os.unlink',side_effect=windows_error(32)):
            with self.assertRaises(PermissionError) as caught:
                with self.store.txn() as data:data['n']=100
        self.assertIs(caught.exception,original)
        self.assertTrue(any('临时状态文件清理失败' in note for note in original.__notes__))
        self.assertEqual(self.store.snapshot(),{'n':0})

    @unittest.skipUnless(os.name=='nt','Windows delete-sharing behavior')
    def test_real_windows_reader_can_temporarily_block_atomic_replace(self):
        real_replace=os.replace
        reader=self.path.open('rb')
        errors=[]; timers=[]
        def replace(source,target):
            try:return real_replace(source,target)
            except OSError as error:
                errors.append(getattr(error,'winerror',None))
                if not timers:
                    timer=threading.Timer(0.03,reader.close)
                    timers.append(timer);timer.start()
                raise
        try:
            with patch('doubao_automation.state.os.replace',side_effect=replace):
                with self.store.txn() as data:data['n']=1
        finally:
            reader.close()
            for timer in timers:timer.join(timeout=1)
        self.assertTrue(errors,'the controlled external reader must deny delete sharing')
        self.assertTrue(all(code in (5,32) for code in errors))
        self.assertEqual(self.store.snapshot(),{'n':1})


if __name__=='__main__':unittest.main()
