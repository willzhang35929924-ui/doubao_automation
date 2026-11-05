"""Login startup contracts use disposable account state and mocked browsers."""
import copy
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import dashboard


class LoginStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-login-test-')
        self.root=Path(self.tmp.name)
        self.cfg={'accounts':{'doubao':[]},'browser':{'headless':True}}
        self.patches=[patch.object(dashboard,'load_config',return_value=self.cfg),
                      patch.object(dashboard,'_savecfg'),
                      patch.object(dashboard,'PROFILES',self.root/'profiles'),
                      patch.object(dashboard,'STATE_DIR',self.root/'state'),
                      patch.object(dashboard,'LOGS_DIR',self.root/'logs'),
                      patch.object(dashboard,'use_workspace_temp'),
                      patch.object(dashboard,'running',return_value=False),
                      patch.object(dashboard,'pending_accounts',{})]
        for p in self.patches:p.start()
        self.client=dashboard.app.test_client()
        self.client.environ_base.update(HTTP_ORIGIN='http://localhost',HTTP_X_CSRF_TOKEN=dashboard.CSRF_TOKEN)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def job(self,ready=True,**fields):
        j={'ready':threading.Event(),'finished':threading.Event(),'stop':threading.Event(),**fields}
        if ready:j['ready'].set()
        else:j['ready']=Mock(wait=Mock(return_value=False))
        return j

    def test_failed_add_reports_error_but_preserves_retryable_account(self):
        with patch.object(dashboard,'start_login',return_value=self.job(error='fixture browser missing')):
            r=self.client.post('/api/accounts/add',json={})
        self.assertEqual(r.status_code,503)
        self.assertEqual(r.json['code'],'browser_start_failed')
        self.assertTrue(r.json['saved'])
        self.assertEqual(len(self.cfg['accounts']['doubao']),1)
        self.assertFalse(self.cfg['accounts']['doubao'][0]['enabled'])
        self.assertIn('fixture browser missing',r.json['error'])

    def test_open_confirms_ready_window_and_reuses_saved_account(self):
        self.cfg['accounts']['doubao']=[dict(name='saved',profile='profiles/saved',enabled=False)]
        with patch.object(dashboard,'start_login',return_value=self.job()) as start:
            r=self.client.post('/api/accounts/open',json={'name':'saved'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['status'],'needs_login')
        self.assertIn('已打开',r.json['message'])
        self.assertEqual(len(self.cfg['accounts']['doubao']),1)
        start.assert_called_once_with('saved','profiles/saved')

    def test_slow_start_does_not_claim_window_is_open(self):
        with patch.object(dashboard,'start_login',return_value=self.job(ready=False)):
            r=self.client.post('/api/accounts/add',json={})
        self.assertEqual(r.status_code,202)
        self.assertEqual(r.json['status'],'opening')
        self.assertNotIn('已打开',r.json['message'])

    def test_login_overrides_headless_without_changing_generation_config(self):
        session=Mock();session.page.is_closed.return_value=True
        session.context.pages=[session.page]
        before=copy.deepcopy(self.cfg);j=self.job()
        with patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.return_value=session
            dashboard.login_worker('saved','profiles/saved',j)
        self.assertIs(browser.call_args.args[2]['browser']['headless'],False)
        self.assertEqual(self.cfg,before)
        session.stop.assert_called_once()
        self.assertTrue(j['finished'].is_set())

    def test_launch_failure_is_signalled_and_logged(self):
        j=self.job(ready=False);j['ready']=threading.Event()
        with patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.side_effect=RuntimeError('fixture startup failed')
            dashboard.login_worker('saved','profiles/saved',j)
        self.assertTrue(j['ready'].is_set())
        self.assertTrue(j['finished'].is_set())
        self.assertIn('fixture startup failed',j['error'])
        self.assertIn('fixture startup failed',(self.root/'logs/login_browser.log').read_text())


class PrivateConfigSaveTests(unittest.TestCase):
    @unittest.skipIf(os.name=='nt','POSIX private config permissions')
    def test_account_save_does_not_make_credentials_world_readable(self):
        with tempfile.TemporaryDirectory(prefix='doubao-private-config-') as folder:
            p=Path(folder)/'config.yaml'
            p.write_text('fixture: old\n');p.chmod(0o600)
            with patch.object(dashboard,'DEFAULT_CONFIG_PATH',p):
                dashboard._savecfg({'fixture':'new'})
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertEqual(list(Path(folder).iterdir()),[p])
            self.assertIn('new',p.read_text())


if __name__=='__main__':unittest.main()
