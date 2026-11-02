"""Account API tests use temporary state and mocked browser sessions only."""
import sys
import datetime
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import dashboard
from doubao_automation.account_manager import AccountManager
from doubao_automation.state import JsonStore


class AccountQuotaApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-tests-')
        self.directory=Path(self.tmp.name)
        self.cfg={'accounts':{'doubao':[{'name':'test-account','profile':'profiles/test-account','enabled':False}]}}
        self.patches=[patch.object(dashboard,'STATE_DIR',self.directory),
                      patch.object(dashboard,'load_config',return_value=self.cfg),
                      patch.object(dashboard,'pending_accounts',{}),
                      patch.object(dashboard,'_savecfg'),
                      patch.object(dashboard,'use_workspace_temp')]
        for item in self.patches:item.start()
        self.store=JsonStore(self.directory/'accounts.json')
        self.client=dashboard.app.test_client()
        self.client.environ_base.update({'HTTP_ORIGIN':'http://localhost','HTTP_X_CSRF_TOKEN':dashboard.CSRF_TOKEN})

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def manager(self,status='needs_login'):
        with self.store.txn() as data:
            today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
            data.setdefault('accounts',{})['test-account']={'status':status,'note':'original note',
                                                          'date':today if status=='exhausted' else ''}
        return AccountManager(self.store,['test-account'])

    def test_list_adds_quota_without_claiming_login_for_unverified_account(self):
        response=self.client.get('/api/accounts')
        self.assertEqual(response.status_code,200)
        account=response.json['accounts'][0]
        self.assertEqual(account['status'],'needs_login')
        self.assertFalse(account['enabled'])
        self.assertEqual(account['quota']['limit'],4)
        self.assertEqual(account['quota']['timezone'],'Asia/Shanghai')
        for field in ('used','reserved','unknown','remaining','estimated','date','blocked_reason'):
            self.assertIn(field,account['quota'])

    def test_login_window_is_separate_from_exhausted_identity(self):
        manager=self.manager('exhausted')
        manager.observe_remaining('test-account',0)
        dashboard.pending_accounts['test-account']={'thread':Mock(is_alive=lambda:True),
                                                   'status':'opening','note':'waiting for login'}
        account=self.client.get('/api/accounts').json['accounts'][0]
        self.assertEqual(account['status'],'exhausted')
        self.assertEqual(account['quota']['remaining'],0)
        self.assertTrue(account['login_window_open'])
        self.assertEqual(account['login_status'],'opening')

    def test_confirm_never_resets_quota_or_enables_needs_login(self):
        manager=self.manager('needs_login')
        manager.observe_remaining('test-account',0)
        before=manager.quota('test-account')
        response=self.client.post('/api/accounts/confirm',json={'name':'test-account'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(manager.status('test-account')['status'],'needs_login')
        self.assertEqual(manager.quota('test-account'),before)
        self.assertEqual(self.client.post('/api/accounts/confirm',json={'name':'missing'}).status_code,400)

    def test_confirm_exhausted_reports_zero_without_reenabling(self):
        manager=self.manager('exhausted')
        manager.observe_remaining('test-account',0)
        before=manager.quota('test-account')
        response=self.client.post('/api/accounts/confirm',json={'name':'test-account'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['status'],'exhausted')
        self.assertEqual(response.json['quota']['remaining'],0)
        self.assertIn('自动跳过',response.json['message'])
        self.assertEqual(manager.status('test-account')['status'],'exhausted')
        self.assertEqual(manager.quota('test-account'),before)

    def test_starting_login_does_not_overwrite_exhaustion(self):
        manager=self.manager('exhausted')
        manager.observe_remaining('test-account',0)
        with patch.object(dashboard.threading,'Thread') as thread:
            dashboard.start_login('test-account','profiles/test-account')
        thread.return_value.start.assert_called_once()
        self.assertEqual(manager.status('test-account')['status'],'exhausted')
        self.assertEqual(manager.quota('test-account')['remaining'],0)

    def test_real_login_success_keeps_quota_exhausted(self):
        manager=self.manager('exhausted')
        manager.observe_remaining('test-account',0)
        session=Mock()
        session.page.is_closed.return_value=False
        session.has_any_cookie.return_value=True
        session.page.locator.return_value.count.return_value=1
        job={'stop':threading.Event(),'ready':threading.Event(),'finished':threading.Event()}
        with patch('doubao_automation.browser_pool.BrowserSession') as browser:
            browser.return_value.start.return_value=session
            dashboard.login_worker('test-account','profiles/test-account',job)
        self.assertTrue(job['verified'])
        self.assertTrue(job['finished'].is_set())
        session.stop.assert_called_once()
        self.assertEqual(manager.status('test-account')['status'],'exhausted')
        self.assertEqual(manager.quota('test-account')['remaining'],0)

    def test_status_updates_preserve_daily_ledger(self):
        manager=self.manager('needs_login')
        manager.observe_remaining('test-account',2)
        before=manager.quota('test-account')
        dashboard.account_status('test-account','needs_login','login expired')
        self.assertEqual(manager.quota('test-account'),before)
        self.assertEqual(manager.status('test-account')['status'],'needs_login')

    def test_genuine_login_preserves_observed_balance(self):
        manager=self.manager('needs_login')
        manager.observe_remaining('test-account',2)
        before=manager.quota('test-account')
        dashboard.account_status('test-account','available','login verified')
        self.assertEqual(manager.status('test-account')['status'],'available')
        after=manager.quota('test-account')
        for field in ('limit','used','reserved','unknown','remaining','estimated'):
            self.assertEqual(after[field],before[field])

    def test_legacy_exhaustion_survives_verified_login(self):
        manager=self.manager('exhausted')
        dashboard.account_status('test-account','available','login verified')
        self.assertEqual(manager.status('test-account')['status'],'exhausted')
        self.assertEqual(manager.quota('test-account')['remaining'],0)
        self.assertEqual(manager.quota('test-account')['used'],0,'migration must not invent generated videos')

    def test_verified_login_does_not_clear_explicit_disable(self):
        manager=self.manager('disabled')
        manager.observe_remaining('test-account',2)
        dashboard.account_status('test-account','available','login verified')
        self.assertEqual(manager.status('test-account')['status'],'disabled')
        self.assertEqual(manager.quota('test-account')['remaining'],2)

    def test_daily_refresh_does_not_clear_login_or_disable(self):
        self.cfg['accounts']['doubao'].extend([
            dict(name='login-required',profile='profiles/login-required',enabled=True),
            dict(name='disabled',profile='profiles/disabled',enabled=False)])
        with self.store.txn() as data:
            data['accounts']={
                'test-account':dict(status='exhausted',date='2000-01-01'),
                'login-required':dict(status='needs_login',date='2000-01-01'),
                'disabled':dict(status='disabled',date='2000-01-01')}
        accounts={a['name']:a for a in self.client.get('/api/accounts').json['accounts']}
        self.assertEqual(accounts['test-account']['status'],'available')
        self.assertEqual(accounts['test-account']['quota']['remaining'],4)
        self.assertEqual(accounts['login-required']['status'],'needs_login')
        self.assertEqual(accounts['disabled']['status'],'disabled')


if __name__=='__main__':unittest.main()
