"""Caption API contracts use only disposable files and a mocked local model."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dashboard
from doubao_automation import caption_context, caption_settings
from doubao_automation.caption_page import CaptionPageError, ProductPage
from doubao_automation.caption_runtime import CaptionRuntimeError
from doubao_automation.state import JsonStore


class CaptionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='caption-api-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state=self.root/'state'
        self.products=self.root/'products'
        self.sources=self.root/'sources'
        self.logs=self.root/'logs'
        for path in (self.state,self.products,self.sources,self.logs):path.mkdir()
        self.image=self.products/'product.png';self.image.write_bytes(b'fixture product image')
        self.video=self.sources/'reference.mp4';self.video.write_bytes(b'fixture source video')
        self.cfg={'accounts':{'doubao':[]},'gemini':{}}
        self.patches=[patch.object(dashboard,'STATE_DIR',self.state),
                      patch.object(dashboard,'PRODUCTS_DIR',self.products),
                      patch.object(dashboard,'SOURCES_DIR',self.sources),
                      patch.object(dashboard,'LOGS_DIR',self.logs),
                      patch.object(caption_context,'PRODUCTS_DIR',self.products),
                      patch.object(caption_context,'SOURCES_DIR',self.sources),
                      patch.object(caption_settings,'CONFIG_DIR',self.root/'config'),
                      patch.object(dashboard,'load_config',return_value=self.cfg),
                      patch.object(dashboard,'running',return_value=False),
                      patch.object(dashboard,'caption_model_lock',threading.Lock())]
        for item in self.patches:item.start()
        self.store=JsonStore(self.state/'pipeline_state.json')
        self.store.reset(dict(sources=[dict(record_id='fixture-record',keyword='fixture',
                                            prompt_text='fixture instruction',videos=[str(self.video)])],
                              products=[dict(record_id='fixture-product',keyword='fixture',
                                             images=[str(self.image)])],
                              scripts=[dict(id='existing-script',text='existing planned script')],
                              tasks=[dict(id='existing-task',script_id='existing-script',status='awaiting_result')],
                              requests={'existing-request':dict(phase='unknown')}))
        self.client=self.new_client()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()

    @staticmethod
    def new_client():
        client=dashboard.app.test_client()
        client.environ_base.update(HTTP_ORIGIN='http://localhost',
                                   HTTP_X_CSRF_TOKEN=dashboard.CSRF_TOKEN)
        return client

    def target(self,kind='images',index=None,role=None):
        return dict(record_id='fixture-record',kind=kind,index=index,
                    role=role or ('product' if kind=='images' else 'reference'))

    def generated(self,model,kind='images',index=None,role=None):
        with patch('doubao_automation.caption_runtime.generate_caption',model):
            return self.client.post('/api/captions/generate',json=self.target(kind,index,role))

    def test_state_and_generation_use_only_saved_media_without_erasing_plan(self):
        row=self.client.get('/api/state').json['rows'][0]
        self.assertEqual(row['caption_images'],[str(self.image)])
        self.assertEqual(row['caption_videos'],[{'index':0,'path':str(self.video)}])
        self.assertEqual(row['images'],[str(self.image)])
        self.assertEqual(row['captions'],{'images':None,'video':None,'link':None})
        model=Mock(return_value='柔软玩具熊的近景。')
        response=self.generated(model)
        self.assertEqual(response.status_code,200,response.get_json())
        entry=response.json['entry']
        self.assertEqual(entry['text'],'柔软玩具熊的近景。')
        self.assertFalse(entry['edited'])
        self.assertTrue(entry['enabled'])
        self.assertEqual(entry['role'],'product')
        self.assertIsNone(entry['index'])
        model.assert_called_once()
        self.assertEqual([Path(p).resolve() for p in model.call_args.args[0]],[self.image.resolve()])
        self.assertEqual(model.call_args.args[1:],('images','product'))
        snap=self.store.snapshot()
        self.assertEqual(snap['scripts'][0]['id'],'existing-script')
        self.assertEqual(snap['tasks'][0]['status'],'awaiting_result')
        self.assertIn('existing-request',snap['requests'])
        self.assertEqual(self.client.get('/api/state').json['rows'][0]['captions']['images']['text'],entry['text'])

    def test_manual_edit_returns_candidate_and_save_requires_matching_revision(self):
        first=self.generated(Mock(return_value='初始描述。')).json['entry']
        save=self.client.post('/api/captions/save',json={**self.target(),
            'text':'人工修改。','revision':first['revision'],
            'fingerprint':first['fingerprint'],'enabled':True})
        self.assertEqual(save.status_code,200,save.get_json())
        edited=save.json['entry']
        self.assertTrue(edited['edited'])
        before=self.store.snapshot()
        candidate=self.generated(Mock(return_value='新模型候选。'))
        self.assertEqual(candidate.status_code,200,candidate.get_json())
        self.assertEqual(candidate.json['entry']['text'],'人工修改。')
        self.assertEqual(candidate.json['candidate_text'],'新模型候选。')
        self.assertEqual(candidate.json['candidate_target']['fingerprint'],edited['fingerprint'])
        self.assertEqual(self.store.snapshot(),before)
        stale=self.client.post('/api/captions/save',json={**self.target(),
            'text':'旧版本覆盖。','revision':first['revision'],
            'fingerprint':first['fingerprint'],'enabled':True})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.store.snapshot(),before)

    def test_concurrent_edit_wins_over_in_flight_generation_and_only_one_model_runs(self):
        entered=threading.Event();release=threading.Event();result=[]
        def model(_paths,_kind,_role):
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture timed out')
            return '迟到的模型结果。'
        def background():
            with patch('doubao_automation.caption_runtime.generate_caption',side_effect=model):
                result.append(self.new_client().post('/api/captions/generate',json=self.target()))
        worker=threading.Thread(target=background)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch('doubao_automation.caption_runtime.generate_caption') as duplicate:
                blocked=self.client.post('/api/captions/generate',json=self.target())
            self.assertEqual(blocked.status_code,409)
            duplicate.assert_not_called()
            fingerprint=dashboard.caption_target(self.store.snapshot(),
                dashboard.caption_source(self.store.snapshot(),'fixture-record'),
                'images',index=None,role='product')['fingerprint']
            saved=self.client.post('/api/captions/save',json={**self.target(),
                'text':'人工先保存。','revision':None,'fingerprint':fingerprint,'enabled':True})
            self.assertEqual(saved.status_code,200,saved.get_json())
        finally:
            release.set();worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].status_code,409,result[0].get_json())
        self.assertEqual(self.client.get('/api/state').json['rows'][0]['captions']['images']['text'],'人工先保存。')

    def test_changed_material_and_outside_media_never_reach_model(self):
        target=self.target()
        current=dashboard.caption_target(self.store.snapshot(),
            dashboard.caption_source(self.store.snapshot(),'fixture-record'),
            'images',index=None,role='product')
        self.image.write_bytes(b'changed image')
        stale=self.client.post('/api/captions/save',json={**target,'text':'旧素材描述',
            'revision':None,'fingerprint':current['fingerprint'],'enabled':True})
        self.assertEqual(stale.status_code,409)
        outside=self.root/'outside.png';outside.write_bytes(b'outside')
        with self.store.txn() as data:data['products'][0]['images']=[str(outside)]
        model=Mock(return_value='should not run')
        rejected=self.generated(model)
        self.assertGreaterEqual(rejected.status_code,400)
        model.assert_not_called()

    def test_material_replacement_during_inference_discards_old_candidate(self):
        entered=threading.Event();release=threading.Event();result=[]
        def model(_paths,_kind,_role):
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture timed out')
            return '已过期素材的描述。'
        def background():
            with patch('doubao_automation.caption_runtime.generate_caption',side_effect=model):
                result.append(self.new_client().post('/api/captions/generate',json=self.target()))
        worker=threading.Thread(target=background)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            replacement=self.products/'new-product.png'
            replacement.write_bytes(b'new fixture product image')
            with self.store.txn() as data:
                data['products'][0]['images']=[str(replacement)]
        finally:
            release.set();worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].status_code,409,result[0].get_json())
        self.assertIsNone(self.client.get('/api/state').json['rows'][0]['captions']['images'])
        self.assertEqual(self.store.snapshot()['tasks'][0]['status'],'awaiting_result')

    def test_video_target_preview_and_container_gate(self):
        model=Mock(return_value='参考视频描述。')
        video=self.generated(model,kind='video')
        self.assertEqual(video.status_code,200,video.get_json())
        self.assertEqual(video.json['entry']['role'],'reference')
        self.assertEqual([Path(p).resolve() for p in model.call_args.args[0]],[self.video.resolve()])
        with patch('doubao_automation.orchestrator.build_script_prompt',return_value='prepared') as build, \
             patch('doubao_automation.gemini_web.format_gemini_request',return_value=('full prompt',1,[])) as format_request:
            preview=self.client.post('/api/captions/preview',json={'record_id':'fixture-record'})
        self.assertEqual(preview.status_code,200,preview.get_json())
        self.assertEqual(preview.json['prompt'],'full prompt')
        build.assert_called_once()
        format_request.assert_called_once_with('prepared')
        with patch.dict(os.environ,{'DOUBAO_CONTAINER_PREVIEW':'1'}), \
             patch('doubao_automation.caption_runtime.generate_caption') as blocked_model:
            denied=self.client.post('/api/captions/generate',json=self.target())
        self.assertEqual(denied.status_code,501)
        blocked_model.assert_not_called()

    def test_mixed_source_attachments_offer_only_video_with_original_index(self):
        pictures=[]
        for index in range(6):
            image=self.sources/f'reference-{index}.webp'
            image.write_bytes(b'fixture reference picture')
            pictures.append(str(image))
        with self.store.txn() as data:
            data['sources'][0]['videos']=[*pictures,str(self.video)]
        row=self.client.get('/api/state').json['rows'][0]
        self.assertEqual(row['videos'],[*pictures,str(self.video)])
        self.assertEqual(row['caption_videos'],[{'index':6,'path':str(self.video)}])
        model=Mock(return_value='第七个附件是视频。')
        response=self.generated(model,kind='video',index=6)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.json['entry']['index'],6)
        self.assertEqual([Path(p).resolve() for p in model.call_args.args[0]],[self.video.resolve()])

    def test_model_error_and_invalid_target_preserve_journal(self):
        before=self.store.snapshot()
        with patch('doubao_automation.caption_runtime.generate_caption',side_effect=RuntimeError('fixture failure')):
            failed=self.client.post('/api/captions/generate',json=self.target())
        self.assertEqual(failed.status_code,502)
        self.assertEqual(self.store.snapshot(),before)
        for body in ({**self.target(), 'index':-1},{**self.target(), 'role':'unknown'},
                     {**self.target(), 'record_id':'missing'}):
            response=self.client.post('/api/captions/generate',json=body)
            self.assertGreaterEqual(response.status_code,400)

    def test_settings_are_masked_on_save_and_reload(self):
        endpoint = '/api/captions/settings'
        defaults = self.client.get(endpoint).json
        self.assertEqual(defaults['mode'], 'local')
        self.assertTrue(defaults['image_prompt'])
        self.assertIn('openCaptionModelSettings()', self.client.get('/').get_data(as_text=True))
        update = dict(base_url='https://vision.example.test/v1', model='vision',
                      api_key='fixture-only-key', image_prompt='识别图片中的图案')
        self.assertEqual(dashboard.app.test_client().post(endpoint, json=update).status_code, 403)
        saved = self.client.post(endpoint, json=update)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.json['mode'], 'cloud')
        self.assertEqual(saved.json['api_key_mask'], '****')
        self.assertNotIn(update['api_key'], saved.get_data(as_text=True))
        reloaded = self.new_client().get(endpoint)
        self.assertEqual(reloaded.json, saved.json)
        edited = self.client.post(endpoint, json={'model': 'updated-vision'})
        self.assertTrue(edited.json['api_key_configured'])
        self.assertEqual(edited.json['image_prompt'], update['image_prompt'])

    def test_missing_each_cloud_field_uses_local_with_saved_prompt(self):
        full = dict(base_url='https://vision.example.test/v1', model='vision',
                    api_key='fixture-only-key', image_prompt='优先描述图案')
        for field in ('base_url', 'model', 'api_key'):
            with self.subTest(field=field):
                payload = full | {field: ''}
                if field == 'api_key':
                    payload['clear_api_key'] = True
                saved = self.client.post('/api/captions/settings', json=payload)
                self.assertEqual(saved.json['mode'], 'local')
                with patch('doubao_automation.caption_cloud.generate_cloud_caption') as cloud:
                    local = Mock(return_value='本地识别结果。')
                    response = self.generated(local)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(local.call_args.kwargs, {'instructions': full['image_prompt']})
                cloud.assert_not_called()

    def test_configured_cloud_receives_target_and_corresponding_instructions(self):
        options = dict(base_url='https://vision.example.test/v1', model='vision',
                       api_key='fixture-only-key', image_prompt='识别图案',
                       product_video_prompt='描述商品实拍', reference_video_prompt='总结参考镜头')
        self.assertEqual(self.client.post('/api/captions/settings', json=options).status_code, 200)
        with patch('doubao_automation.caption_cloud.generate_cloud_caption',
                   return_value='已识别素材。') as cloud, \
             patch('doubao_automation.caption_runtime.generate_caption') as local:
            for kind, role, prompt in (('images', 'product', 'image_prompt'),
                                       ('video', 'product', 'product_video_prompt'),
                                       ('video', 'reference', 'reference_video_prompt')):
                with self.subTest(kind=kind, role=role):
                    response = self.client.post('/api/captions/generate', json=self.target(kind, role=role))
                    self.assertEqual(response.status_code, 200, response.get_json())
                    self.assertEqual(cloud.call_args.args[1:], (kind, role))
                    self.assertEqual(cloud.call_args.kwargs,
                                     {name: options[name] for name in ('base_url', 'model', 'api_key')}
                                     | {'instructions': options[prompt]})
            local.assert_not_called()
            cloud.side_effect = CaptionRuntimeError('云端识别服务暂不可用')
            before = self.store.snapshot()
            failed = self.client.post('/api/captions/generate', json=self.target())
            self.assertEqual(failed.status_code, 502)
            self.assertEqual(failed.json['code'], 'caption_cloud_error')
            self.assertEqual(self.store.snapshot(), before)
            local.assert_not_called()


    def test_link_caption_combines_with_image_in_actual_gemini_preview(self):
        url = 'https://products.example.com/item?tag=local-test'
        payload = dict(record_id='fixture-record', url=url, revision=None)
        endpoint = '/api/captions/link/generate'
        with self.store.txn() as data:
            data['fixed_prompt'] = '本次只写1个15秒的视频脚本。'
        self.assertEqual(self.generated(Mock(return_value='图片是一只白色马克杯。')).status_code, 200)
        self.assertEqual(self.client.post('/api/captions/settings',
                                         json={'link_prompt': '提取公开规格与容量'}).status_code, 200)
        self.assertEqual(dashboard.app.test_client().post(endpoint, json=payload).status_code, 403)
        with patch('doubao_automation.caption_page.fetch_product_page',
                   return_value=ProductPage(url, '马克杯', '容量 350 毫升。')) as fetch, \
             patch('doubao_automation.caption_runtime.generate_page_caption',
                   return_value='网页标注容量 350 毫升。') as model:
            result = self.client.post(endpoint, json=payload)
        self.assertEqual(result.status_code, 200, result.json)
        fetch.assert_called_once_with(url)
        model.assert_called_once_with('马克杯', '容量 350 毫升。', instructions='提取公开规格与容量')
        saved = result.json['entry']
        self.assertTrue(saved['enabled'])
        self.assertEqual(self.new_client().get('/api/state').json['rows'][0]['captions']['link'], saved)
        preview = self.client.post('/api/captions/preview', json={'record_id': 'fixture-record'})
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertIn('图片是一只白色马克杯。', preview.json['prompt'])
        self.assertIn(saved['text'], preview.json['prompt'])
        self.assertIn('本次最终只输出1个脚本', preview.json['prompt'])
        self.assertNotIn(url, preview.json['prompt'])
        disabled = self.client.post('/api/captions/link/save', json={
            **payload, 'text': saved['text'], 'enabled': False, 'revision': saved['revision']})
        self.assertEqual(disabled.status_code, 200, disabled.json)
        self.assertTrue(disabled.json['entry']['enabled'])
        preview = self.client.post('/api/captions/preview', json={'record_id': 'fixture-record'})
        self.assertIn('图片是一只白色马克杯。', preview.json['prompt'])
        self.assertIn(saved['text'], preview.json['prompt'])
        self.assertEqual(self.store.snapshot()['tasks'][0]['status'], 'awaiting_result')

    def test_link_cloud_uses_extracted_text_and_failures_preserve_previous_description(self):
        endpoint = '/api/captions/link/generate'
        url = 'https://products.example.com/item'
        payload = dict(record_id='fixture-record', url=url, revision=None)
        cloud = dict(base_url='https://vision.example.com/v1', model='vision', api_key='fixture-only-key')
        self.assertEqual(self.client.post('/api/captions/settings',
                                         json={**cloud, 'link_prompt': '提取明确规格'}).status_code, 200)
        with patch('doubao_automation.caption_page.fetch_product_page',
                   return_value=ProductPage(url + '/redirected', '杯子', '网页：350 毫升')) as fetch, \
             patch('doubao_automation.caption_runtime.generate_page_caption') as local, \
             patch('doubao_automation.caption_cloud.generate_cloud_page_caption',
                   return_value='杯子容量 350 毫升。') as remote:
            generated = self.client.post(endpoint, json=payload)
            self.assertEqual(generated.status_code, 200, generated.json)
            saved = generated.json['entry']
            self.assertEqual(saved['url'], url)
            remote.assert_called_once_with('杯子', '网页：350 毫升', instructions='提取明确规格', **cloud)
            local.assert_not_called()
            before = self.store.snapshot()
            fetch.side_effect = CaptionPageError('商品页面拒绝访问')
            blocked = self.client.post(endpoint, json={**payload, 'revision': saved['revision']})
            self.assertEqual(blocked.status_code, 502, blocked.json)
            self.assertEqual(blocked.json['code'], 'caption_page_unavailable')
            remote.assert_called_once()
            self.assertEqual(before, self.store.snapshot())

    def test_tiktok_uses_browser_before_model_and_stops_at_verification(self):
        url = 'https://shop.tiktok.com/us/pdp/1732813990412915186'
        payload = dict(record_id='fixture-record', url=url + '?source=feed', revision=None)
        with patch('doubao_automation.caption_browser.read_browser_product_page',
                   return_value=ProductPage(url, '公开商品', '浏览器显示 PP 棉玩具与蒸笼外观收纳篮。')) as browser, \
             patch('doubao_automation.caption_page.fetch_product_page') as static, \
             patch('doubao_automation.caption_runtime.generate_page_caption',
                   return_value='商品页面展示 PP 棉玩具与蒸笼外观收纳篮。') as model:
            result = self.client.post('/api/captions/link/generate', json=payload)
            self.assertEqual(result.status_code, 200, result.json)
            browser.assert_called_once_with(url)
            static.assert_not_called()
            model.assert_called_once()
            saved = result.json['entry']
            before = self.store.snapshot()
            browser.side_effect = CaptionPageError('商品页面需要安全验证，读取已停止')
            rejected = self.client.post('/api/captions/link/generate',
                                        json={**payload, 'revision': saved['revision']})
            self.assertEqual(rejected.status_code, 502, rejected.json)
            self.assertEqual(rejected.json['code'], 'caption_page_unavailable')
            self.assertEqual(before, self.store.snapshot())
            model.assert_called_once()
            static.assert_not_called()

    def test_link_conflict_protects_edits_and_rejects_private_url_before_fetch(self):
        generate = '/api/captions/link/generate'
        save = '/api/captions/link/save'
        url = 'https://products.example.com/item'
        payload = dict(record_id='fixture-record', url=url, revision=None)
        edited = self.client.post(save, json={**payload, 'text': '保留人工描述。', 'enabled': True})
        self.assertEqual(edited.status_code, 200, edited.json)
        entry = edited.json['entry']
        self.assertTrue(entry['edited'])
        with patch('doubao_automation.caption_page.fetch_product_page',
                   return_value=ProductPage(url, '杯子', '网页可见规格')) as fetch, \
             patch('doubao_automation.caption_runtime.generate_page_caption', return_value='新候选。') as model:
            denied = self.client.post(generate, json={**payload, 'url': 'http://127.0.0.1/'})
            self.assertEqual(denied.status_code, 400, denied.json)
            stale = self.client.post(generate, json=payload)
            self.assertEqual(stale.status_code, 409, stale.json)
            fetch.assert_not_called()
            candidate = self.client.post(generate, json={**payload, 'revision': entry['revision']})
            self.assertEqual(candidate.status_code, 200, candidate.json)
            self.assertEqual(candidate.json, dict(entry=entry, candidate_text='新候选。', candidate_url=url))
            before = self.store.snapshot()
            with patch.dict(os.environ, {'DOUBAO_CONTAINER_PREVIEW': '1'}):
                blocked = self.client.post(generate, json={**payload, 'revision': entry['revision']})
            self.assertEqual(blocked.status_code, 501, blocked.json)
            fetch.assert_called_once()
            self.assertEqual(before, self.store.snapshot())

            def reassign_product(*args, **kwargs):
                with self.store.txn() as data:
                    data['sources'][0]['keyword'] = 'different product'
                return '描述属于已经更换的商品。'

            model.side_effect = reassign_product
            changed = self.client.post(generate, json={**payload, 'revision': entry['revision']})
            self.assertEqual(changed.status_code, 409, changed.json)
            self.assertIsNone(self.client.get('/api/state').json['rows'][0]['captions']['link'])


if __name__=='__main__':unittest.main()
