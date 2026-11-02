"""回归：保存、运行输入、视频传输和跨进程状态一致性。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dashboard
from doubao_automation.state import JsonStore
from doubao_automation.orchestrator import Pipeline


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doubao-tests-')
        self.path = Path(self.tmp.name)
        self.patchers = [patch.object(dashboard, 'STATE_DIR', self.path),
                         patch.object(dashboard, 'LOGS_DIR', self.path),
                         patch.object(dashboard, 'proc', None),
                         patch.object(dashboard.postprocess, 'STORE_PATH', self.path / 'postprocess.json')]
        for p in self.patchers:
            p.start()
        self.client = dashboard.app.test_client()
        self.client.environ_base.update({'HTTP_ORIGIN':'http://localhost','HTTP_X_CSRF_TOKEN':dashboard.CSRF_TOKEN})
        with dashboard.store().txn() as d:
            d.update(sources=[{'record_id':'r1','keyword':'产品','prompt_text':'原提示词','videos':[]}],
                     products=[{'keyword':'产品','images':['image.png']}],
                     tasks=[{'id':'old','status':'done'}], scripts=[{'id':'old'}])

    def tearDown(self):
        for p in reversed(self.patchers): p.stop()
        self.tmp.cleanup()

    def test_save_persists_and_noop_preserves_completed_tasks(self):
        body={'rows':[{'record_id':'r1','keyword':'产品','prompt_text':'原提示词'}]}
        self.assertEqual(self.client.post('/api/edit',json=body).status_code,200)
        self.assertEqual(dashboard.store().snapshot()['tasks'][0]['status'],'done')
        body['rows'][0]['prompt_text']='修改后提示词'
        self.assertEqual(self.client.post('/api/edit',json=body).json['updated'],1)
        snap=JsonStore(self.path/'pipeline_state.json').snapshot()
        self.assertEqual(snap['sources'][0]['prompt_text'],'修改后提示词')
        self.assertEqual(snap['tasks'],[])

    def test_stale_record_is_rejected(self):
        r=self.client.post('/api/edit',json={'rows':[{'record_id':'missing','keyword':'x','prompt_text':'y'}]})
        self.assertEqual(r.status_code,400)

    def test_run_uses_saved_sources_and_unique_output(self):
        child=Mock();child.pid=123;child.poll.return_value=None
        with patch.object(dashboard.subprocess,'Popen',return_value=child) as popen, patch.object(dashboard.threading,'Thread'):
            # A stale browser may still send the former setting; it cannot limit sources.
            r=self.client.post('/api/run',json={'task_count':1})
        self.assertEqual(r.status_code,200)
        self.assertIn('--from-saved',popen.call_args.args[0])
        self.assertNotIn('--limit',popen.call_args.args[0])
        snap=dashboard.store().snapshot()
        self.assertNotIn('requested_script_count',snap)
        self.assertTrue(snap['run_id'].startswith('r'))
        self.assertEqual(snap['sources'][0]['prompt_text'],'原提示词')
        self.assertEqual(self.client.post('/api/edit',json={'rows':[]}).status_code,400)

    def test_saved_pipeline_skips_remote_fetch(self):
        p=Pipeline.__new__(Pipeline);p.store=dashboard.store()
        p.stage_fetch=Mock();p.stage_scripts=Mock();p.stage_plan=Mock();p.stage_run=Mock()
        p.run_all(force=True,from_saved=True,limit=1)
        p.stage_fetch.assert_not_called()
        p.stage_scripts.assert_called_once_with(force=True,limit=1)

    def test_video_range_and_path_boundary(self):
        with patch.object(dashboard,'VIDEOS_DIR',self.path):
            p=self.path/'中文 视频.mp4';p.write_bytes(b'0123456789')
            r=self.client.get('/media-video',query_string={'path':str(p)},headers={'Range':'bytes=2-5'})
            self.assertEqual(r.status_code,206);self.assertEqual(r.data,b'2345');r.close()
            self.assertEqual(self.client.get('/media-video',query_string={'path':str(ROOT/'dashboard.py')}).status_code,404)

    def test_approval_uses_resolved_video_and_exposes_job_status(self):
        with patch.object(dashboard,'VIDEOS_DIR',self.path):
            source=self.path/'待审核 视频.mp4';source.write_bytes(b'video')
            job={'id':'approval-test','source':str(source.resolve()),'status':'queued','progress':0}
            with patch.object(dashboard.postprocess,'submit',return_value=job) as submit:
                response=self.client.post('/api/videos/approve',json={'path':str(source)})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json['job']['id'],job['id'])
                submit.assert_called_once_with(source.resolve())
                self.assertEqual(self.client.post('/api/videos/approve',json={'path':str(ROOT/'dashboard.py')}).status_code,404)
                self.assertEqual(submit.call_count,1)
            job['log']=str(self.path/'missing-test.log')
            with JsonStore(self.path/'postprocess.json').txn() as data:
                data['jobs']=[job]
            self.assertEqual(self.client.get('/api/videos').json['videos'][0]['postprocess'],job)
            self.assertEqual(self.client.get('/api/postprocess').json['jobs'],[job])

    def test_video_delete_removes_disk_file_and_row_and_reports_busy(self):
        with patch.object(dashboard,'VIDEOS_DIR',self.path), \
             patch.object(dashboard.postprocess,'VIDEOS_DIR',self.path), \
             patch.object(dashboard.postprocess,'STORE_PATH',self.path/'postprocess-test.json'):
            source=self.path/'测试删除 视频.mp4';source.write_bytes(b'disposable fixture')
            response=self.client.post('/api/videos/delete',json={'path':str(source)})
            self.assertEqual(response.status_code,200)
            self.assertFalse(source.exists())
            self.assertEqual(self.client.get('/api/videos').json['videos'],[])
            source.write_bytes(b'busy fixture')
            with patch.object(dashboard,'running',return_value=True), \
                 patch.object(dashboard.postprocess,'delete_source') as delete:
                self.assertEqual(self.client.post('/api/videos/delete',json={'path':str(source)}).status_code,409)
                delete.assert_not_called()
                self.assertTrue(source.exists())
            for error in (dashboard.postprocess.VideoInUse('正在后处理'),PermissionError()):
                with patch.object(dashboard.postprocess,'delete_source',side_effect=error):
                    response=self.client.post('/api/videos/delete',json={'path':str(source)})
                self.assertEqual(response.status_code,409)
                self.assertTrue(source.exists())

    def test_vanished_file_during_list_does_not_break_polling(self):
        vanished=Mock();vanished.stat.side_effect=FileNotFoundError()
        with patch.object(dashboard,'VIDEOS_DIR') as directory:
            directory.glob.return_value=[vanished]
            response=self.client.get('/api/videos')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['videos'],[])

    def test_missing_login_does_not_report_success(self):
        r=self.client.post('/api/accounts/confirm',json={'name':'not_logged_in'})
        self.assertEqual(r.status_code,400)

    def test_open_folder_uses_shell_and_reports_failure(self):
        with patch.object(dashboard,'VIDEOS_DIR',self.path):
            p=self.path/"中文 有空格's.mp4";p.write_bytes(b'video')
            with patch.object(dashboard,'open_video_folder',return_value=True) as reveal:
                r=self.client.post('/api/open-video',json={'path':str(p)})
                self.assertEqual(r.status_code,200)
                reveal.assert_called_once_with(p.resolve())
                self.assertEqual(r.json['folder'],str(self.path))
            with patch.object(dashboard,'open_video_folder',side_effect=OSError('0x80070005')):
                r=self.client.post('/api/open-video',json={'path':str(p)})
                self.assertEqual(r.status_code,500)
                self.assertIn('0x80070005',r.json['error'])
            with patch.object(dashboard,'open_video_folder') as reveal:
                self.assertEqual(self.client.post('/api/open-video',json={'path':str(ROOT/'dashboard.py')}).status_code,404)
                reveal.assert_not_called()

    def test_repeated_confirm_does_not_duplicate_account_config(self):
        with JsonStore(self.path/'accounts.json').txn() as d:
            d['accounts']={'verified':{'status':'available'}}
        with patch.object(dashboard,'_savecfg') as save:
            for _ in range(2):
                self.assertEqual(self.client.post('/api/accounts/confirm',json={'name':'verified'}).status_code,200)
        save.assert_not_called()

    def test_exhausted_account_is_not_reenabled_or_launched(self):
        p=Pipeline.__new__(Pipeline);p.accounts=Mock();p.accounts.status.return_value={'status':'exhausted'}
        with patch('doubao_automation.orchestrator.BrowserSession') as browser:
            p._worker({'name':'exhausted','profile':'unused'})
        browser.assert_not_called();p.accounts._set.assert_not_called()


class StateTests(unittest.TestCase):
    def test_cross_instance_reload_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix='doubao-tests-') as td:
            path=Path(td)/'state.json';a=JsonStore(path);b=JsonStore(path)
            with a.txn() as d:d['a']=1
            with b.txn() as d:d['b']=2
            self.assertEqual(a.snapshot(),{'a':1,'b':2})
            with self.assertRaises(ValueError):
                with a.txn() as d:
                    d['a']=3;raise ValueError('abort')
            self.assertEqual(a.snapshot()['a'],1)

    def test_concurrent_processes_do_not_lose_writes(self):
        with tempfile.TemporaryDirectory(prefix='doubao-tests-') as td:
            path=Path(td)/'state.json'
            code="from doubao_automation.state import JsonStore\nimport sys\ns=JsonStore(sys.argv[1])\nfor i in range(30):\n with s.txn() as d:d['n']=d.get('n',0)+1"
            children=[]
            try:
                for _ in range(3):
                    children.append(subprocess.Popen([sys.executable,'-c',code,str(path)],cwd=ROOT))
                results=[child.wait(timeout=30) for child in children]
                self.assertEqual(results,[0,0,0])
                self.assertEqual(JsonStore(path).snapshot()['n'],90)
            finally:
                # Finish only children created by this test before deleting its directory.
                for child in children:
                    if child.poll() is None:
                        try:
                            child.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)


if __name__=='__main__':
    unittest.main(verbosity=2)
