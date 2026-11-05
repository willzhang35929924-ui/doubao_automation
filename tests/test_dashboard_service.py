"""Offline supervisor regressions: never terminate workers without a safe protocol."""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_service as service


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(service, 'TEMP_DIR', root / 'temp'))
        self.stack.enter_context(patch.object(service, 'LOGS_DIR', root / 'logs'))
        self.stack.enter_context(patch.object(service, 'validate_sources'))
        self.revision = self.stack.enter_context(patch.object(service, 'revision', return_value=('initial',)))
        self.port = self.stack.enter_context(patch.object(service, 'local_port_in_use', return_value=False))
        self.worker = Mock(pid=12345)
        self.worker.poll.return_value = None
        self.start = self.stack.enter_context(patch.object(service, 'start_worker', return_value=self.worker))
        self.publish = self.stack.enter_context(patch.object(service, 'publish'))
        self.sleep = self.stack.enter_context(patch.object(service.time, 'sleep', side_effect=KeyboardInterrupt))
        self.run = self.stack.enter_context(patch.object(service.subprocess, 'run'))
        self.popen = self.stack.enter_context(patch.object(service.subprocess, 'Popen'))
        self.kill = self.stack.enter_context(patch.object(service.os, 'kill'))

    def assert_no_termination(self):
        self.run.assert_not_called()
        self.popen.assert_not_called()
        self.kill.assert_not_called()
        self.worker.terminate.assert_not_called()
        self.worker.kill.assert_not_called()
        self.worker.wait.assert_not_called()

    def test_manager_exit_leaves_active_worker_running(self):
        with self.assertRaises(KeyboardInterrupt):
            service.main()
        self.start.assert_called_once_with()
        self.assert_no_termination()

    def test_code_update_reports_pending_without_restarting_worker(self):
        self.revision.side_effect = [('initial',), ('updated',)]
        self.sleep.side_effect = [None, KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            service.main()
        self.start.assert_called_once_with()
        fields = self.publish.call_args.kwargs
        self.assertTrue(fields['reload_pending'])
        self.assertIn('缺少安全关停协议', fields['error'])
        self.assert_no_termination()

    def test_existing_listener_is_neither_started_over_nor_stopped(self):
        self.port.return_value = True
        service.main()
        self.start.assert_not_called()
        self.assert_no_termination()

    def test_finished_worker_does_not_kill_reused_pid_or_existing_listener(self):
        self.worker.poll.return_value = 0
        self.worker.returncode = 0
        self.port.side_effect = [False, True]
        self.sleep.side_effect = None
        service.main()
        self.start.assert_called_once_with()
        self.assertIn('不会关闭该服务', self.publish.call_args.kwargs['error'])
        self.assert_no_termination()


if __name__=='__main__':unittest.main()
