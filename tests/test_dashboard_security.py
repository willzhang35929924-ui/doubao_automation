"""Offline local API security tests; all files and state are disposable fixtures."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dashboard


class LocalSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dashboard_security_', dir=os.environ.get('DOUBAO_TEST_TEMP_DIR') or None)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.videos = self.root / 'videos'
        self.videos.mkdir()
        self.patches = [
            patch.object(dashboard, 'STATE_DIR', self.state),
            patch.object(dashboard, 'LOGS_DIR', self.root / 'logs'),
            patch.object(dashboard, 'VIDEOS_DIR', self.videos),
            patch.object(dashboard, 'proc', None),
            patch.object(dashboard, 'pending_accounts', {}),
            patch.object(dashboard, 'load_config', return_value={'accounts': {'doubao': []}}),
            patch.object(dashboard.postprocess, 'STORE_PATH', self.state / 'postprocess.json'),
            patch.object(dashboard.postprocess, 'JOBS_DIR', self.root / 'postprocess'),
            patch.object(dashboard.postprocess, 'VIDEOS_DIR', self.videos),
        ]
        for item in self.patches: item.start()
        self.client = dashboard.app.test_client()
        self.good = {'Host': 'localhost', 'Origin': 'http://localhost', 'X-CSRF-Token': dashboard.CSRF_TOKEN}

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def test_page_and_csrf_refresh_are_local_no_store(self):
        page = self.client.get('/', headers={'Host': 'localhost'})
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.get_data(as_text=True), r'<meta\s+name=["\x27]csrf-token["\x27]')
        self.assertTrue(dashboard.CSRF_TOKEN in page.get_data(as_text=True))
        token = self.client.get('/api/csrf', headers={'Host':'localhost', 'Origin':'http://localhost'})
        self.assertEqual(token.status_code, 200)
        self.assertTrue(token.json['token'] == dashboard.CSRF_TOKEN)
        self.assertIn('no-store', token.headers.get('Cache-Control', ''))
        self.assertEqual(token.headers.get('X-Content-Type-Options'), 'nosniff')

    def test_get_rejects_bad_host_and_cross_origin_token_read(self):
        for host in ('attacker.example', 'localhost.attacker.example', '127.0.0.1.attacker.example', 'localhost:12345'):
            with self.subTest(host=host):
                self.assertEqual(self.client.get('/', headers={'Host':host}).status_code, 403)
                self.assertEqual(self.client.get('/api/csrf', headers={'Host':host}).status_code, 403)
        self.assertEqual(self.client.get('/api/csrf', headers={'Host':'localhost', 'Origin':'https://attacker.example'}).status_code, 403)

    def test_every_write_route_rejects_missing_and_wrong_token(self):
        routes = sorted((rule.rule, method) for rule in dashboard.app.url_map.iter_rules()
                        for method in rule.methods & {'POST','PUT','PATCH','DELETE'})
        self.assertGreaterEqual(len(routes), 10)
        for url, method in routes:
            if '<' in url: self.fail('Add fixture for dynamic write route: '+url)
            for token in (None, 'wrong-token'):
                headers = {'Host':'localhost', 'Origin':'http://localhost'}
                if token is not None: headers['X-CSRF-Token'] = token
                with self.subTest(url=url, method=method, token='missing' if token is None else 'wrong'):
                    response = self.client.open(url, method=method, json={}, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json.get('code'), 'csrf_invalid')

    def test_missing_or_invalid_origin_rejected_even_with_token(self):
        for origin in (None, 'null', 'https://attacker.example', 'http://localhost.attacker.example',
                       'https://localhost', 'http://localhost:12345', 'http://127.0.0.1'):
            headers = {'Host':'localhost', 'X-CSRF-Token':dashboard.CSRF_TOKEN}
            if origin is not None: headers['Origin'] = origin
            with self.subTest(origin=origin):
                self.assertEqual(self.client.post('/api/edit', json={}, headers=headers).status_code, 403)

    def test_cross_site_form_cannot_delete_a_real_fixture(self):
        video = self.videos / 'fixture.mp4'; video.write_bytes(b'not-real-video')
        headers = {'Host':'localhost', 'Origin':'https://attacker.example'}
        with patch.object(dashboard.postprocess, 'delete_source') as delete:
            r = self.client.post('/api/videos/delete', data={'path':str(video)}, headers=headers)
        self.assertEqual(r.status_code, 403); delete.assert_not_called(); self.assertTrue(video.exists())

    def test_authorized_delete_only_removes_disposable_fixture(self):
        video = self.videos / 'fixture.mp4'; video.write_bytes(b'not-real-video')
        r = self.client.post('/api/videos/delete', json={'path':str(video)}, headers=self.good)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['ok']); self.assertFalse(video.exists())

    def test_authorized_save_with_in_memory_store(self):
        from contextlib import contextmanager
        state = {'sources':[{'record_id':'fixture', 'keyword':'old', 'prompt_text':'old'}]}
        @contextmanager
        def transaction():
            yield state
        with patch.object(dashboard, 'store') as store:
            store.return_value.txn = transaction
            r = self.client.post('/api/edit', json={'rows':[{'record_id':'fixture', 'keyword':'new', 'prompt_text':'new'}]}, headers=self.good)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['updated'], 1)
        self.assertEqual(state['sources'][0]['prompt_text'], 'new')

    def test_delete_handler_is_never_called_without_valid_authentication(self):
        video = self.videos / 'fixture.mp4'; video.write_bytes(b'not-real-video')
        with patch.object(dashboard.postprocess, 'delete_source') as delete:
            for token in (None, 'wrong-token'):
                headers = {'Host':'localhost', 'Origin':'http://localhost'}
                if token is not None: headers['X-CSRF-Token'] = token
                self.assertEqual(self.client.post('/api/videos/delete', json={'path':str(video)}, headers=headers).status_code, 403)
            response = self.client.post('/api/videos/delete', json={'path':str(video)}, headers=self.good,
                                        environ_overrides={'REMOTE_ADDR':'192.0.2.1'})
            self.assertEqual(response.status_code, 403)
            delete.assert_not_called()
        self.assertTrue(video.exists())
    def test_authorized_delete_calls_handler_with_disposable_video(self):
        video = self.videos / 'fixture.mp4'; video.write_bytes(b'not-real-video')
        with patch.object(dashboard.postprocess, 'delete_source', return_value=str(video)) as delete:
            r = self.client.post('/api/videos/delete', json={'path':str(video)}, headers=self.good)
        self.assertEqual(r.status_code, 200)
        delete.assert_called_once_with(video.resolve())
    def test_authorized_save_still_operates(self):
        path=self.state / 'pipeline_state.json'; self.state.mkdir()
        path.write_text(json.dumps({'sources':[{'record_id':'fixture','keyword':'old','prompt_text':'x'}]}),encoding='utf-8')
        body={'rows':[{'record_id':'fixture','keyword':'new','prompt_text':'updated'}]}
        r=self.client.post('/api/edit', json=body, headers=self.good)
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json['updated'], 1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['sources'][0]['prompt_text'],'updated')

    def test_get_polling_never_creates_or_changes_state_or_starts_worker(self):
        with patch.object(dashboard.postprocess, 'ensure_worker') as worker:
            for path in ('/api/state','/api/accounts','/api/videos','/api/postprocess'):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path, headers={'Host':'localhost'}).status_code, 200)
            worker.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root/'postprocess').exists())

    def test_get_existing_state_files_do_not_change(self):
        self.state.mkdir(); saved = {'pipeline_state.json':'{"sources":[],"products":[]}',
                                    'accounts.json':'{"accounts":{}}',
                                    'postprocess.json':'{"jobs":[]}'}
        for name,content in saved.items(): (self.state/name).write_text(content,encoding='utf-8')
        before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.state.iterdir()}
        with patch.object(dashboard.postprocess, 'ensure_worker') as worker:
            for path in ('/api/state','/api/accounts','/api/videos','/api/postprocess'):
                self.assertEqual(self.client.get(path, headers={'Host':'localhost'}).status_code, 200)
            worker.assert_not_called()
        after={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.state.iterdir()}
        self.assertEqual(before,after)

    def test_non_loopback_client_and_cross_origin_preflight_are_rejected(self):
        response = self.client.get('/', headers={'Host':'localhost'}, environ_overrides={'REMOTE_ADDR':'192.0.2.1'})
        self.assertEqual(response.status_code, 403)
        response = self.client.options('/api/videos/delete', headers={'Host':'localhost', 'Origin':'https://attacker.example',
                                        'Access-Control-Request-Method':'POST', 'Access-Control-Request-Headers':'X-CSRF-Token'})
        self.assertNotEqual(response.headers.get('Access-Control-Allow-Origin'), '*')
        self.assertNotEqual(response.headers.get('Access-Control-Allow-Origin'), 'https://attacker.example')

    def test_get_cannot_call_a_mutating_endpoint(self):
        response = self.client.get('/api/videos/delete', headers={'Host':'localhost'})
        self.assertEqual(response.status_code, 405)
        self.assertFalse(self.state.exists())
    def test_unsafe_methods_and_loopback_variants(self):
        for method in ('PUT','PATCH','DELETE'):
            self.assertEqual(self.client.open('/api/edit',method=method,headers={'Host':'localhost','Origin':'http://localhost'},json={}).status_code,403)
        for host in ('localhost:8782','127.0.0.1:8782'):
            with self.subTest(host=host):
                self.assertEqual(self.client.get('/',headers={'Host':host}).status_code,200)
                h={'Host':host,'Origin':'http://'+host,'X-CSRF-Token':dashboard.CSRF_TOKEN}
                self.assertEqual(self.client.post('/api/edit',headers=h,json={'rows':[]}).status_code,200)


if __name__ == '__main__':
    unittest.main()