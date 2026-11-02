"""The Windows venv launcher must not leave an old Flask child on port 8782."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_service as service


class WorkerStopTests(unittest.TestCase):
    def test_windows_stops_only_owned_worker_tree(self):
        worker=Mock(pid=12345)
        worker.poll.return_value=None
        with patch.object(service.os,'name','nt'), patch.object(service.subprocess,'run',return_value=Mock(returncode=0)) as stop:
            service.stop_worker(worker)
        self.assertEqual(stop.call_args.args[0],['taskkill','/PID','12345','/T','/F'])
        worker.terminate.assert_not_called()
        worker.wait.assert_called_once_with(timeout=8)

    def test_finished_worker_does_not_kill_reused_pid(self):
        worker=Mock(pid=12345)
        worker.poll.return_value=0
        with patch.object(service.subprocess,'run') as stop:
            service.stop_worker(worker)
        stop.assert_not_called()


if __name__=='__main__':unittest.main()
