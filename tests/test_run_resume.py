"""Dashboard launches are mocked; all journals, logs and runner locks are temporary."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import dashboard
from doubao_automation.orchestrator import Pipeline
from doubao_automation.state import JsonStore


class RunResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-tests-')
        self.directory=Path(self.tmp.name)
        self.logs=self.directory/'logs';self.logs.mkdir()
        self.videos=self.directory/'videos';self.videos.mkdir()
        self.child=Mock(pid=321)
        self.child.poll.return_value=None
        self.patches=[patch.object(dashboard,'STATE_DIR',self.directory),
                      patch.object(dashboard,'LOGS_DIR',self.logs),
                      patch.object(dashboard,'VIDEOS_DIR',self.videos),
                      patch.object(dashboard,'proc',None),patch.object(dashboard,'pending_accounts',{}),
                      patch.object(dashboard.threading,'Thread'),
                      patch.object(dashboard.subprocess,'Popen',return_value=self.child),
                      patch.object(dashboard.postprocess,'busy',return_value=False),
                      patch.object(dashboard.postprocess,'STORE_PATH',self.directory/'postprocess.json')]
        self.mocks=[item.start() for item in self.patches]
        self.popen=self.mocks[6]
        self.client=dashboard.app.test_client()
        self.client.environ_base.update({'HTTP_ORIGIN':'http://localhost','HTTP_X_CSRF_TOKEN':dashboard.CSRF_TOKEN})
        self.state=dashboard.store()
        self.log=self.logs/'original.log';self.log.write_text('original log\n',encoding='utf-8')
        self.original={
            'run_id':'original-run','run_log':str(self.log),'run_returncode':1,'requested_script_count':2,
            'sources':[dict(record_id='fixture',keyword='product',prompt_text='saved prompt',videos=[])],
            'products':[dict(keyword='product',images=['fixture.png'])],
            'scripts':[dict(id='s1',text='original script'),dict(id='s2',text='another script')],
            'tasks':[dict(id='s1#0',script_id='s1',status='done',request_id='accepted'),
                     dict(id='s1#1',script_id='s1',status='awaiting_result',request_id='accepted')],
            'requests':{'accepted':dict(phase='unknown',submitted=True,task_ids=['s1#0','s1#1'])},
            'assemblies':{}}
        self.state.reset(self.original)
        self.ledger=JsonStore(self.directory/'accounts.json')
        self.ledger.reset({'accounts':{'fixture':{'status':'available','quota_days':{'fixture-day':{'entries':{'accepted:s1#0':{'state':'charged'}}}}}}})

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def post(self,count=9):
        return self.client.post('/api/run',json={'task_count':count})

    def test_incomplete_plan_preserves_journal_count_ledger_and_appends_log(self):
        before_ledger=self.ledger.snapshot()
        response=self.post()
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['resumed'])
        self.assertEqual(response.json['script_count'],2)
        self.assertIn('沿用原计划',response.json['message'])
        self.assertEqual(self.popen.call_args.args[0],[sys.executable,'-u','run_pipeline.py','run'])
        actual=self.state.snapshot()
        for key in ('run_id','scripts','tasks','requests','assemblies','requested_script_count'):
            self.assertEqual(actual[key],self.original[key])
        self.assertEqual(self.ledger.snapshot(),before_ledger)
        self.assertEqual(actual['run_log'],str(self.log))
        self.assertIsNone(actual['run_returncode'])
        self.assertTrue(self.log.read_text(encoding='utf-8').startswith('original log\n'))

    def test_all_non_done_task_states_resume_without_force(self):
        for status in ('pending','reserved','running','failed','needs_user','downloaded','local_failed'):
            with self.subTest(status=status):
                dashboard.proc=None
                data=copy.deepcopy(self.original);data['tasks'][1]['status']=status
                self.state.reset(data)
                response=self.post(1)
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json['resumed'])
                self.assertEqual(self.popen.call_args.args[0][-1],'run')
                self.assertEqual(self.state.snapshot()['tasks'],data['tasks'])

    def test_missing_failed_or_incomplete_assembly_resumes_finished_segments(self):
        for assembly in (None,{'status':'failed'},{'status':'running'},{'status':'done','output':str(self.videos/'missing.mp4')}):
            with self.subTest(assembly=assembly):
                dashboard.proc=None
                data=copy.deepcopy(self.original)
                for task in data['tasks']:task['status']='done'
                data['assemblies']={} if assembly is None else {'s1':assembly}
                self.state.reset(data)
                response=self.post()
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json['resumed'])
                self.assertEqual(self.state.snapshot()['tasks'],data['tasks'])
                self.assertEqual(self.state.snapshot()['assemblies'],data['assemblies'])

    def test_complete_plan_or_no_plan_starts_new_generation(self):
        final=self.videos/'s1_final.mp4';final.write_bytes(b'owned fixture')
        for no_plan in (False,True):
            with self.subTest(no_plan=no_plan):
                dashboard.proc=None
                data=copy.deepcopy(self.original)
                for task in data['tasks']:task['status']='done'
                data['assemblies']={'s1':dict(status='done',output=str(final))}
                if no_plan:data.update(tasks=[],scripts=[],assemblies={})
                self.state.reset(data)
                response=self.post(3)
                self.assertEqual(response.status_code,200)
                self.assertFalse(response.json['resumed'])
                self.assertIsNone(response.json['script_count'])
                command=self.popen.call_args.args[0]
                self.assertIn('all',command);self.assertIn('--force',command);self.assertIn('--from-saved',command)
                self.assertNotIn('--limit',command)
                actual=self.state.snapshot()
                self.assertNotEqual(actual['run_id'],data['run_id'])
                self.assertEqual(actual['requested_script_count'],data['requested_script_count'])
                for key in ('scripts','tasks'):self.assertEqual(actual[key],[])
                self.assertEqual(actual['requests'],{})
                self.assertEqual(actual['assemblies'],{})
                self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')

    def test_resume_does_not_require_sources_for_a_new_gemini_plan(self):
        data=copy.deepcopy(self.original);data.update(sources=[],products=[])
        self.state.reset(data)
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.popen.call_args.args[0][-1],'run')

    def test_state_exposes_small_resume_summary_without_scripts_or_requests(self):
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['can_resume'])
        self.assertEqual(response.json['resume_script_count'],2)
        self.assertEqual(response.json['resume_run_id'],'original-run')
        self.assertNotIn('scripts',response.json);self.assertNotIn('requests',response.json)

    def test_live_runner_guard_blocks_reset_even_after_idle_probe_race(self):
        owner=Pipeline.__new__(Pipeline);owner.store=self.state
        before=self.state.snapshot()
        with owner._run_lock(),patch.object(dashboard,'running',return_value=False):
            response=self.post()
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state.snapshot(),before)
        self.popen.assert_not_called()
        self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')

    def test_runner_lock_is_released_before_mock_child_starts(self):
        def spawn(*args,**kwargs):
            child=Pipeline.__new__(Pipeline);child.store=self.state
            with child._run_lock():pass
            return self.child
        self.popen.side_effect=spawn
        self.assertEqual(self.post().status_code,200)

    def test_second_click_is_rejected_without_mutation(self):
        self.assertEqual(self.post().status_code,200)
        before=self.state.snapshot();log=self.log.read_text(encoding='utf-8')
        self.assertEqual(self.post().status_code,409)
        self.assertEqual(self.state.snapshot(),before)
        self.assertEqual(self.log.read_text(encoding='utf-8'),log)
        self.popen.assert_called_once()

    def test_cli_acquiring_lock_after_prepare_blocks_dashboard_spawn(self):
        before=self.state.snapshot()
        with patch.object(dashboard,'running',side_effect=[False,True]):
            response=self.post()
        self.assertEqual(response.status_code,409)
        for key in ('run_id','scripts','tasks','requests','assemblies','requested_script_count'):
            self.assertEqual(self.state.snapshot()[key],before[key])
        self.popen.assert_not_called()

    def test_stale_watcher_cannot_override_resumed_run_returncode(self):
        with self.state.txn() as data:data['run_attempt_id']='current-attempt'
        stale=Mock();stale.wait.return_value=99
        dashboard.watch_run(stale,'original-run','old-attempt')
        self.assertEqual(self.state.snapshot()['run_returncode'],1)
        dashboard.watch_run(stale,'original-run')
        self.assertEqual(self.state.snapshot()['run_returncode'],1)
        current=Mock();current.wait.return_value=0
        dashboard.watch_run(current,'original-run','current-attempt')
        self.assertEqual(self.state.snapshot()['run_returncode'],0)


if __name__=='__main__':unittest.main()
