"""审核队列和 SeedVR 分段帧数回归，不运行 GPU。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from doubao_automation import postprocess as pp


class FrameTests(unittest.TestCase):
    def test_boundary_overlap_removes_no_unique_frames(self):
        for maximum in (17,49):
            for count in (1,2,17,18,49,50,97,361):
                reconstructed=[]
                for i,(start,end) in enumerate(pp.clip_ranges(count,maximum)):
                    self.assertLessEqual(end-start+1,maximum)
                    reconstructed.extend(range(start+(1 if i else 0),end+1))
                self.assertEqual(reconstructed,list(range(count)))
        self.assertEqual(len(pp.clip_ranges(361)),8)
        self.assertEqual(pp.clip_ranges(361)[-1],(336,360))
        self.assertEqual(len(pp.clip_ranges(361,17)),23)
        self.assertEqual(pp.clip_ranges(361,17)[-1],(352,360))

    def test_1080_dimensions_preserve_orientation(self):
        self.assertEqual(pp.target_size(720,1280),(1080,1920))
        self.assertEqual(pp.target_size(1280,720),(1920,1080))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.root=Path(self.tmp.name)
        self.workspace=self.root/'workspace';self.videos=self.workspace/'videos';self.videos.mkdir(parents=True)
        self.source=self.videos/'source.mp4';self.source.write_bytes(b'original source')
        executable = 'Scripts/python.exe' if pp.os.name == 'nt' else 'bin/python'
        python=self.root/'third_party/video-subtitle-remover/.venv'/executable;python.parent.mkdir(parents=True);python.write_bytes(b'fake')
        runner=self.workspace/'postprocess_setup/vsr_run.py';runner.parent.mkdir(parents=True);runner.write_text('')
        (runner.parent/'vsr_ready.json').write_text('{"ready":true}')
        model=self.root/'model';model.write_text('fake model')
        config=self.root/'setup.json';config.write_text(json.dumps({'models':{'diffusion':{'path':str(model)}}}))
        values={'ROOT':self.root,'WORKSPACE':self.workspace,'VIDEOS_DIR':self.videos,
                'JOBS_DIR':self.workspace/'postprocess','STORE_PATH':self.workspace/'state/jobs.json','SETUP':config}
        self.patchers=[patch.object(pp,k,v) for k,v in values.items()]
        self.patchers.append(patch.object(pp,'ensure_worker'))
        self.patchers.append(patch.object(pp,'_assert_vsr_path_contract'))
        self.patchers.append(patch.object(pp,'find_ffmpeg',return_value='ffmpeg'))
        self.patchers.append(patch.object(pp,'find_ffprobe',return_value='ffprobe'))
        for p in self.patchers:p.start()

    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.tmp.cleanup()

    def test_double_approval_is_one_job_and_original_is_preserved(self):
        a=pp.submit(self.source);b=pp.submit(self.source)
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(len(pp.store().snapshot()['jobs']),1)
        self.assertEqual(self.source.read_bytes(),b'original source')
        self.assertTrue(pp.busy())

    def test_missing_vsr_dependencies_prevent_submission(self):
        python, _ = pp._vsr_paths()
        python.unlink()
        with self.assertRaisesRegex(ValueError, '发布副本未包含'):
            pp.submit(self.source)
        pp.ensure_worker.assert_not_called()
        self.assertFalse(pp.STORE_PATH.exists())

    def test_failed_retry_new_job_and_done_output_reuse(self):
        a=pp.submit(self.source);pp.update(a['id'],status='failed',error='GPU error')
        b=pp.submit(self.source);self.assertNotEqual(a['id'],b['id'])
        output=self.videos/'approved/source.mp4';output.parent.mkdir();output.write_bytes(b'done')
        pp.update(b['id'],status='done',output_path=str(output))
        self.assertEqual(pp.submit(self.source)['id'],b['id'])

    def test_output_or_foreign_path_cannot_be_approved(self):
        output=self.videos/'approved/source.mp4';output.parent.mkdir();output.write_bytes(b'done')
        with self.assertRaises(ValueError):pp.submit(output)
        with self.assertRaises(ValueError):pp.submit(self.root/'model')

    def test_deletion_is_blocked_while_source_is_in_processing(self):
        job=pp.submit(self.source)
        for status in ('queued','running'):
            pp.update(job['id'],status=status)
            with self.assertRaises(pp.VideoInUse):pp.delete_source(self.source)
            self.assertTrue(self.source.is_file())

    def test_deletion_removes_original_disk_file_only(self):
        job=pp.submit(self.source)
        output=self.videos/'approved/source.mp4';output.parent.mkdir();output.write_bytes(b'final')
        pp.update(job['id'],status='done',output_path=str(output))
        self.assertEqual(pp.delete_source(self.source),str(self.source.resolve()))
        self.assertFalse(self.source.exists())
        self.assertEqual(output.read_bytes(),b'final')
        self.assertEqual(len(pp.store().snapshot()['jobs']),1)
        with self.assertRaises(ValueError):pp.delete_source(output)


class SeedVRRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.tmp')
        self.directory=Path(self.tmp.name)
        self.processor=pp.Processor.__new__(pp.Processor)
        self.processor.directory=self.directory
        self.processor.endpoint='http://127.0.0.1:8189'
        self.processor.id='job-test'
        self.processor.session=Mock()
        self.processor.wait_gpu=Mock()
        self.processor.config={'prompt_template':{str(i):{'inputs':{}} for i in (1,3,7,10,12,14,15)},'vae_tile_fallbacks':[512,384,256]}
        self.input=self.directory/'input_000.mp4';self.input.write_bytes(b'clip')

    def tearDown(self):
        self.tmp.cleanup()

    def response(self,data):
        response=Mock();response.json.return_value=data
        return response

    def test_existing_gpu_prompt_is_collected_without_resubmission(self):
        (self.directory/'seedvr_000.submission.json').write_text('{"prompt_id":"existing"}')
        self.processor.session.get.side_effect=[self.response({'queue_running':[],'queue_pending':[]}),self.response({'existing':{'status':{'status_str':'success'}}})]
        self.processor.collect_seedvr=Mock(return_value=self.directory/'seedvr_000.mp4')
        self.processor.seedvr_chunk(self.input,0,1080,1920)
        self.processor.session.post.assert_not_called()
        self.processor.collect_seedvr.assert_called_once_with('existing',0,self.directory/'seedvr_000.mp4')

    def test_only_memory_failure_retries_with_smaller_tiles(self):
        for error,retry in (('CUDA out of memory',True),('invalid model',False)):
            with self.subTest(error=error):
                (self.directory/'seedvr_000.submission.json').unlink(missing_ok=True)
                self.processor.session.reset_mock()
                self.processor.session.post.side_effect=[self.response({'name':self.input.name,'subfolder':'test','type':'input'}),self.response({'prompt_id':'large'}),self.response({'prompt_id':'small'})]
                exception=pp.SeedVROutOfMemory(error) if retry else RuntimeError(error)
                self.processor.collect_seedvr=Mock(side_effect=[exception,self.directory/'seedvr_000.mp4'])
                if retry:
                    self.processor.seedvr_chunk(self.input,0,1080,1920)
                    self.assertEqual(self.processor.session.post.call_count,3)
                    for tile in (512,384):
                        graph=json.loads((self.directory/f'seedvr_000.tile_{tile}.prompt.json').read_text('utf-8'))
                        self.assertEqual(graph['12']['inputs']['tile_size'],tile)
                else:
                    with self.assertRaisesRegex(RuntimeError,'invalid model'):
                        self.processor.seedvr_chunk(self.input,0,1080,1920)
                    self.assertEqual(self.processor.session.post.call_count,2)

    def test_recovered_oom_continues_at_next_smaller_tile(self):
        (self.directory/'seedvr_000.submission.json').write_text('{"prompt_id":"existing"}')
        (self.directory/'seedvr_000.prompt.json').write_text('{"12":{"inputs":{"tile_size":512}}}')
        self.processor.session.get.side_effect=[self.response({'queue_running':[[0,'existing']],'queue_pending':[]}),self.response({})]
        self.processor.session.post.side_effect=[self.response({'name':self.input.name,'subfolder':'test','type':'input'}),self.response({'prompt_id':'smaller'})]
        self.processor.collect_seedvr=Mock(side_effect=[pp.SeedVROutOfMemory('CUDA OOM'),self.directory/'seedvr_000.mp4'])
        self.processor.seedvr_chunk(self.input,0,1080,1920)
        graph=json.loads((self.directory/'seedvr_000.prompt.json').read_text('utf-8'))
        self.assertEqual(graph['12']['inputs']['tile_size'],384)

    def test_long_comfy_traceback_does_not_hide_oom_identity(self):
        error={'exception_type':'torch.OutOfMemoryError','exception_message':'CUDA out of memory','traceback':['x'*10000]}
        exception=pp.seedvr_failure({'messages':[['execution_error',error]]})
        self.assertIsInstance(exception,pp.SeedVROutOfMemory)

    def test_same_server_fifo_cannot_starve_while_other_server_is_respected(self):
        self.processor.config['endpoint']={'queue_check_urls':['http://127.0.0.1:8189/queue','http://127.0.0.1:8188/queue']}
        self.processor.stage_name='SeedVR 第12段';self.processor.progress=68;self.processor.stage=Mock()
        self.processor.session.get.side_effect=[self.response({'queue_running':[[1,'external-job']]}),self.response({'queue_running':[],'queue_pending':[]})]
        with patch.object(pp.time,'sleep'):
            pp.Processor.wait_gpu(self.processor,same_server_fifo=True)
        self.assertEqual([call.args[0] for call in self.processor.session.get.call_args_list],['http://127.0.0.1:8188/queue']*2)
        self.processor.stage.assert_any_call('SeedVR 第12段',68)


if __name__=='__main__':unittest.main(verbosity=2)
