"""Sync visibility and opt-in restoration use disposable state and fake Feishu."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from doubao_automation.feishu_client import FeishuError


class SyncVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-sync-visibility-')
        self.directory=Path(self.tmp.name)
        self.path=self.directory/'pipeline_state.json'
        self.patches=[patch.object(dashboard,'STATE_DIR',self.directory),
                      patch.object(dashboard,'LOGS_DIR',self.directory/'logs'),
                      patch.object(dashboard,'running',return_value=False),
                      patch.object(dashboard,'readonly_postprocess_busy',return_value=False),
                      patch.object(dashboard,'pending_accounts',{}),
                      patch.object(dashboard,'load_config',return_value={})]
        for p in self.patches:p.start()
        self.headers={'Host':'localhost','Origin':'http://localhost','X-CSRF-Token':dashboard.CSRF_TOKEN}
        self.client=dashboard.app.test_client()
        self.products=[dict(record_id='p1',keyword='linked',name='One',images=['a.png']),
                       dict(record_id='p2',keyword='product-only',name='Two',images=['b.png'])]
        self.sources=[dict(record_id=f'r{i}',keyword='linked',prompt_text=f'fixture {i}',videos=[]) for i in range(1,4)]
        self.initial={'sources':[dict(record_id='local_keep',keyword='local',prompt_text='local edited fixture',videos=[])],
                      'products':[dict(record_id='local_product',keyword='local',images=['local.png'])],
                      'hidden_source_ids':['r2','r3','stale-hidden'],'tasks':[],'scripts':[]}
        self.save(self.initial)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def save(self,d):
        self.path.write_text(json.dumps(d),encoding='utf-8')

    def read(self):return json.loads(self.path.read_text())

    def fake_client(self,client_type):
        fc=client_type.return_value
        fc.load_fixed_prompt.return_value='fixture fixed prompt'
        fc.fetch_products.return_value=self.products
        fc.fetch_sources.return_value=self.sources
        fc.sync_stats={'products':dict(remote_records=5,imported_records=2,skipped_no_images=3),
                       'sources':dict(remote_records=8,imported_records=3,skipped_empty=5,
                                      skipped_oversized_only=0,oversized_attachments=0)}
        return fc

    def test_products_are_visible_without_sources_and_duplicate_keywords_keep_all_images(self):
        self.save({'products':[dict(record_id='p1',keyword='same',images=['one.png'],prompt_text='product prompt fixture'),
                               dict(record_id='p2',keyword='same',images=['one.png','two.png']),
                               dict(record_id='p3',keyword='alone',images=['three.png'])],
                   'sources':[dict(record_id='r',keyword='same',prompt_text='fixture',videos=[])]})
        data=self.client.get('/api/state',headers={'Host':'localhost'}).json
        self.assertEqual(len(data['product_rows']),3)
        self.assertEqual(data['product_rows'][0]['prompt'],'product prompt fixture')
        self.assertEqual(data['product_rows'][2]['linked_sources'],0)
        self.assertEqual(data['product_rows'][2]['images'],['three.png'])
        self.assertEqual(data['rows'][0]['images'],['one.png','two.png'])
        self.assertEqual(len(data['rows']),1,'product-only rows must not become generation sources')

    def test_explicit_legacy_filter_reports_remote_filtered_and_hidden_counts(self):
        with patch.object(dashboard,'FeishuClient') as client_type:
            self.fake_client(client_type)
            response=self.client.post('/api/sync',json={'restore_hidden':False},headers=self.headers)
        self.assertEqual(response.status_code,200)
        data=response.json;summary=data['summary']
        self.assertEqual(summary['products']['remote_records'],5)
        self.assertEqual(summary['products']['skipped_no_images'],3)
        self.assertEqual(summary['sources']['remote_records'],8)
        self.assertEqual(summary['sources']['imported_records'],3)
        self.assertEqual(summary['sources']['hidden_records'],2)
        self.assertEqual(summary['sources']['visible_records'],1)
        self.assertEqual(summary['sources']['skipped_empty'],5)
        self.assertEqual(data['restored_hidden'],0)
        self.assertEqual(self.read()['hidden_source_ids'],self.initial['hidden_source_ids'])
        self.assertIn(self.initial['sources'][0],self.read()['sources'])
        self.assertIn(self.initial['products'][0],self.read()['products'])
        self.assertEqual(self.read()['sync_summary'],summary)

    def test_missing_keywords_do_not_cross_link_unrelated_records(self):
        products=[dict(record_id='p',keyword='',images=['unmatched.png'])]
        sources=[dict(record_id='s',keyword='',prompt_text='fixture',videos=[])]
        self.save(dict(products=products,sources=sources))
        data=self.client.get('/api/state',headers={'Host':'localhost'}).json
        self.assertEqual(data['product_rows'][0]['linked_sources'],0)
        self.assertEqual(data['rows'][0]['images'],[])
        report=dashboard.sync_report({},products,sources,sources,0)
        self.assertEqual(report['unmatched_products'],1)
        self.assertEqual(report['sources_without_images'],1)

    def test_normal_sync_restores_deleted_remote_records_still_present_in_feishu(self):
        with patch.object(dashboard,'FeishuClient') as client_type:
            fc=self.fake_client(client_type)
            response=self.client.post('/api/sync',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['sources'],3)
        self.assertEqual(response.json['restored_hidden'],2)
        self.assertEqual(response.json['summary']['sources']['hidden_records'],0)
        self.assertEqual(self.read()['hidden_source_ids'],['stale-hidden'])
        self.assertEqual(response.json['hidden_sources'],0)
        self.assertEqual(response.json['unavailable_hidden_sources'],1)
        state=self.client.get('/api/state',headers={'Host':'localhost'}).json
        self.assertEqual(state['hidden_sources'],0,'stale hidden marks must not advertise recoverable rows')
        self.assertEqual(state['unavailable_hidden_sources'],1)
        self.assertEqual({s['record_id'] for s in self.read()['sources']},{'r1','r2','r3','local_keep'})
        fc.close.assert_called_once()

    def test_newly_hidden_current_record_remains_restorable_without_counting_stale_marks(self):
        with patch.object(dashboard,'FeishuClient') as client_type:
            self.fake_client(client_type)
            self.client.post('/api/sync',json={'restore_hidden':True},headers=self.headers)
        self.client.post('/api/rows/delete',json={'record_id':'r1'},headers=self.headers)
        state=self.client.get('/api/state',headers={'Host':'localhost'}).json
        self.assertEqual(state['hidden_sources'],1)
        self.assertEqual(state['unavailable_hidden_sources'],1)

    def test_delete_only_hides_once_and_next_normal_sync_restores_the_record(self):
        with patch.object(dashboard,'FeishuClient') as client_type:
            self.fake_client(client_type)
            self.client.post('/api/sync',json={},headers=self.headers)
            self.client.post('/api/rows/delete',json={'record_id':'r1'},headers=self.headers)
            self.assertNotIn('r1',{s['record_id'] for s in self.read()['sources']})
            response=self.client.post('/api/sync',json={},headers=self.headers)
        self.assertEqual(response.json['restored_hidden'],1)
        self.assertIn('r1',{s['record_id'] for s in self.read()['sources']})
        self.assertIn('local_keep',{s['record_id'] for s in self.read()['sources']})

    def test_failed_restore_keeps_state_hidden_records_and_local_edits_unchanged(self):
        before=self.path.read_bytes()
        with patch.object(dashboard,'FeishuClient') as client_type:
            fc=self.fake_client(client_type)
            fc.fetch_sources.side_effect=FeishuError('fixture network failure')
            response=self.client.post('/api/sync',json={'restore_hidden':True},headers=self.headers)
        self.assertEqual(response.status_code,500)
        self.assertEqual(self.path.read_bytes(),before)
        fc.close.assert_called_once()

    def test_restore_requires_a_boolean_and_does_not_start_network_for_bad_body(self):
        for body in ({'restore_hidden':'false'},{'restore_hidden':1},[]):
            with self.subTest(body=body),patch.object(dashboard,'FeishuClient') as network:
                response=self.client.post('/api/sync',json=body,headers=self.headers)
                self.assertEqual(response.status_code,400)
                network.assert_not_called()

    def test_page_keeps_source_editor_without_sync_overview(self):
        text=self.client.get('/',headers={'Host':'localhost'}).get_data(as_text=True)
        for name in ('productRows','sync-summary','restore-hidden-feishu'):
            self.assertNotIn('id="'+name+'"',text)
        for label in ('同步记录','产品图记录','产品提示词','删除只影响当前工作台'):
            self.assertNotIn(label,text)
        self.assertIn('提示词与视频记录',text)
        self.assertIn('id="rows"',text)
        self.assertIn('onclick="syncFeishu()"',text)


if __name__=='__main__':unittest.main()
