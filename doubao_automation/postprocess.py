"""审核后处理：上游 VSR 去水印 → 本机 SeedVR2 → 验证后原子发布。"""
import copy
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path, PurePosixPath

import requests

from .paths import ROOT, DATA_ROOT, STATE_DIR, TEMP_DIR, VIDEOS_DIR, WORKSPACE
from .video_utils import find_ffmpeg, find_ffprobe
from .state import JsonStore

APPROVED_DIR = VIDEOS_DIR / 'approved'
JOBS_DIR = WORKSPACE / 'postprocess'
SETUP = Path(os.environ.get('DOUBAO_SEEDVR_CONFIG', str(DATA_ROOT / 'postprocess_setup' / 'seedvr_config.json'))).expanduser()
STORE_PATH = STATE_DIR / 'postprocess.json'
_launch_lock = threading.Lock()
_worker = None


def _try_lock(handle):
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


def _vsr_paths():
    venv = ROOT / 'third_party' / 'video-subtitle-remover' / '.venv'
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return python, WORKSPACE / 'postprocess_setup' / 'vsr_run.py'


def _assert_vsr_path_contract():
    # This release contains no verified VSR adapter. Upstream VSR selects its
    # bundled binary rather than PATH; do not silently execute that binary.
    raise ValueError('第三方去水印适配器尚未通过 PATH-only FFmpeg 契约验证，暂不支持运行')

def _read_setup():
    if not SETUP.is_file():
        raise ValueError('后处理配置不存在；请提供独立的 SeedVR 工作流配置')
    config = json.loads(SETUP.read_text('utf-8'))
    override = os.environ.get('DOUBAO_COMFY_URL', '').strip().rstrip('/')
    if override:
        from urllib.parse import urlsplit
        parsed = urlsplit(override)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('DOUBAO_COMFY_URL 必须是无凭据的 HTTP(S) 服务地址')
        endpoint = config.setdefault('endpoint', {})
        original = endpoint.get('preferred', '').rstrip('/')
        endpoint['preferred'] = override
        endpoint['queue_check_urls'] = [override + url[len(original):] if original and url.startswith(original + '/') else url
                                        for url in endpoint.get('queue_check_urls', [])]
    return config

class SeedVROutOfMemory(RuntimeError):
    pass


class VideoInUse(RuntimeError):
    pass


def delete_source(path):
    source=Path(path).resolve()
    if source.parent!=VIDEOS_DIR.resolve() or not source.is_file() or source.suffix.lower()!='.mp4':
        raise ValueError('只能删除下载目录中该行的原始 MP4 文件')
    # The same process lock protects worker claims/approvals and the physical unlink.
    with store().txn() as data:
        if any(job['source']==str(source) and job['status'] in ('queued','running')
               for job in data.get('jobs',[])):
            raise VideoInUse('该视频正在后处理，完成后才能删除')
        source.unlink()
    return str(source)


def seedvr_failure(status):
    """Keep machine-readable error identity separate from shortened UI details."""
    messages=status.get('messages',[])
    errors=[item[1] for item in messages if isinstance(item,(tuple,list)) and len(item)>1
            and item[0]=='execution_error' and isinstance(item[1],dict)]
    identity=' '.join(str(error.get(key,'')) for error in errors for key in ('exception_type','exception_message'))
    if not identity:identity=json.dumps(messages,ensure_ascii=False)
    detail='SeedVR 运行失败：'+identity[-1800:]
    if any(marker in identity.lower() for marker in ('outofmemory','out of memory','cuda_error_out_of_memory')):
        return SeedVROutOfMemory(detail)
    return RuntimeError(detail)


def clip_ranges(frame_count, max_frames=49):
    if frame_count<1 or max_frames<5 or (max_frames-1)%4:
        raise ValueError('视频帧数或 SeedVR 分段长度无效')
    starts=list(range(0,frame_count-1,max_frames-1)) or [0]
    return [(start,min(start+max_frames-1,frame_count-1)) for start in starts]


def target_size(width,height):
    if min(width,height)<1:raise ValueError('视频尺寸无效')
    return (1080,round(1080*height/width/2)*2) if height>=width else (round(1080*width/height/2)*2,1080)


def store():
    return JsonStore(STORE_PATH, initial={'jobs': []})


def _now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def fingerprint(path):
    path = Path(path)
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def snapshots():
    result = {}
    for job in store().snapshot().get('jobs', []):
        public = dict(job)
        log = Path(job['log'])
        if log.is_file() and JOBS_DIR.resolve() in log.resolve().parents:
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size-1800))
                public['log_tail'] = f.read().decode('utf-8', errors='replace')
        result[job['source']] = public
    return result


def busy():
    active=any(j['status'] in ('queued', 'running') for j in store().snapshot().get('jobs', []))
    if active:ensure_worker()
    return active


def update(job_id, **fields):
    with store().txn() as data:
        for job in data.get('jobs', []):
            if job['id'] == job_id:
                job.update(fields, updated=_now())
                return dict(job)
    raise ValueError('处理任务不存在')


def ensure_worker():
    global _worker
    with _launch_lock:
        TEMP_DIR.mkdir(parents=True,exist_ok=True)
        with (TEMP_DIR/'postprocess_worker.lock').open('a+b') as guard:
            guard.seek(0,2)
            if not guard.tell():guard.write(b'0');guard.flush()
            if not _try_lock(guard):return
            _unlock(guard)
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', TEMP=str(TEMP_DIR), TMP=str(TEMP_DIR))
        with (JOBS_DIR/'worker.log').open('a', encoding='utf-8') as log:
            _worker = subprocess.Popen([sys.executable, '-u', str(ROOT/'run_postprocess.py')],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def submit(path):
    source = Path(path).resolve()
    if source.parent != VIDEOS_DIR.resolve() or not source.is_file() or source.suffix.lower() != '.mp4':
        raise ValueError('只能审核下载目录中的原始 MP4 视频')
    vsr_python, vsr_runner = _vsr_paths()
    ready_path=WORKSPACE/'postprocess_setup/vsr_ready.json'
    ready=json.loads(ready_path.read_text('utf-8')) if ready_path.is_file() else {}
    if not vsr_python.is_file() or not vsr_runner.is_file() or not ready.get('ready'):
        raise ValueError('发布副本未包含第三方 video-subtitle-remover 及适配器；去水印处理暂不可用')
    _assert_vsr_path_contract()
    _read_setup()
    find_ffmpeg()
    find_ffprobe()
    digest = fingerprint(source)
    with store().txn() as data:
        jobs = data.setdefault('jobs', [])
        existing = next((j for j in reversed(jobs) if j['source']==str(source) and j['source_sha256']==digest), None)
        if existing and (existing['status'] in ('queued','running') or
                         (existing['status']=='done' and Path(existing['output_path']).is_file())):
            job = dict(existing)
        else:
            job_id = datetime.datetime.now().strftime('approved_%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]
            directory = JOBS_DIR/job_id
            directory.mkdir(parents=True, exist_ok=True)
            job = dict(id=job_id, source=str(source), source_sha256=digest,
                       name=source.name, status='queued', stage='等待处理', progress=0,
                       error='', output_path='', log=str(directory/'processing.log'),
                       created=_now(), updated=_now())
            jobs.append(job)
    ensure_worker()
    return job


class Processor:
    def __init__(self, job):
        self.job = job
        self.id = job['id']
        self.progress = int(job.get('progress',0))
        self.stage_name = job.get('stage','')
        self.source = Path(job['source'])
        self.directory = JOBS_DIR/self.id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = _read_setup()
        self.ffmpeg = find_ffmpeg()
        self.ffprobe = find_ffprobe()
        self.session = requests.Session()
        self.session.trust_env = False  # Local GPU requests must remain on loopback.
        self.endpoint = self.config['endpoint']['preferred']

    def stage(self, text, progress, **fields):
        self.progress=max(self.progress,progress)
        progress=self.progress
        self.stage_name=text
        print(f'[{_now()}] {text} ({progress}%)', flush=True)
        update(self.id, stage=text, progress=progress, **fields)

    def command(self, args, timeout=1800):
        print('[command] '+subprocess.list2cmdline([str(x) for x in args]), flush=True)
        env = os.environ.copy()
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', TEMP=str(TEMP_DIR), TMP=str(TEMP_DIR))
        result = subprocess.run([str(x) for x in args], cwd=ROOT, env=env, timeout=timeout,
                                stdout=sys.stdout, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise RuntimeError(f'处理程序退出码 {result.returncode}，详情见处理日志')

    def probe(self, path):
        result = subprocess.run([self.ffprobe,'-v','error','-count_frames','-show_streams','-show_format',
                                 '-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',
                                errors='replace',timeout=60,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise RuntimeError('视频读取失败：'+result.stderr[-500:])
        data = json.loads(result.stdout)
        video = next(s for s in data['streams'] if s['codec_type']=='video')
        data['video'] = video
        data['frames'] = int(video.get('nb_read_frames') or video.get('nb_frames') or 0)
        data['fps'] = Fraction(video.get('avg_frame_rate') or video['r_frame_rate'])
        return data

    def wait_gpu(self, progress=48, same_server_fifo=False):
        deadline = time.monotonic()+7200
        noted = False
        previous_stage=self.stage_name
        while time.monotonic()<deadline:
            active = False
            for endpoint in self.config['endpoint']['queue_check_urls']:
                # The preferred ComfyUI serializes its own jobs. Enqueue fairly there;
                # waiting for its queue to become empty can starve behind other clients.
                if same_server_fifo and endpoint.startswith(self.endpoint+'/'):
                    continue
                try:
                    q = self.session.get(endpoint,timeout=3).json()
                    active |= bool(q.get('queue_running') or q.get('queue_pending'))
                except requests.RequestException:
                    if endpoint.startswith(self.endpoint):raise RuntimeError('SeedVR 本地服务不可达：'+self.endpoint)
            if not active:
                if noted:self.stage(previous_stage,self.progress)
                return
            if not noted:
                self.stage('等待本机已有 GPU 任务完成',progress);noted=True
            time.sleep(3)
        raise RuntimeError('本机 GPU 队列等待超时；未中断其他任务')

    def seedvr_chunk(self, path, index, width, height):
        rendered = self.directory/f'seedvr_{index:03d}.mp4'
        if rendered.is_file():
            try:
                p = self.probe(rendered)
                if (p['video']['width'],p['video']['height'])==(width,height):return rendered
            except Exception:pass
        # A dashboard/worker update must not submit the same expensive GPU clip twice.
        submission=self.directory/f'seedvr_{index:03d}.submission.json'
        tiles=list(self.config.get('vae_tile_fallbacks',[512,384,256]))
        if submission.is_file():
            previous=json.loads(submission.read_text('utf-8')).get('prompt_id')
            if previous:
                # Queue must be observed first: completion atomically moves it to history.
                queue=self.session.get(self.endpoint+'/queue',timeout=15)
                queue.raise_for_status();queue=queue.json()
                history=self.session.get(self.endpoint+'/history/'+previous,timeout=15)
                history.raise_for_status();history=history.json().get(previous)
                pending=any(v[1]==previous for v in queue.get('queue_running',[])+queue.get('queue_pending',[]))
                if pending or history:
                    try:
                        print(f'[SeedVR] resume chunk {index} prompt_id={previous}',flush=True)
                        return self.collect_seedvr(previous,index,rendered)
                    except SeedVROutOfMemory:
                        old_graph=json.loads((self.directory/f'seedvr_{index:03d}.prompt.json').read_text('utf-8'))
                        previous_tile=old_graph['12']['inputs']['tile_size']
                        tiles=[tile for tile in tiles if tile<previous_tile]
        self.wait_gpu(same_server_fifo=True)
        with path.open('rb') as f:
            response = self.session.post(self.endpoint+'/upload/image',
                files={'image':(path.name,f,'video/mp4')},
                data={'type':'input','subfolder':'doubao_approved/'+self.id,'overwrite':'false'}, timeout=90)
        response.raise_for_status();upload=response.json()
        relative=PurePosixPath(upload.get('subfolder',''))/upload['name']
        if relative.is_absolute() or '..' in relative.parts or upload.get('type')!='input':
            raise RuntimeError('SeedVR 上传接口返回非法路径')
        graph=copy.deepcopy(self.config['prompt_template'])
        graph['1']['inputs']['file']=str(relative)
        graph['3']['inputs'].update(width=width,height=height)
        graph['10']['inputs']['seed']=20261030
        graph['15']['inputs']['filename_prefix']=f'doubao_approved/{self.id}/chunk_{index:03d}'
        # No input audio in split clips; the original compressed audio is restored once at the end.
        graph['14']['inputs'].pop('audio',None)
        for tile in tiles:
            graph['7']['inputs']['tile_size']=tile
            graph['12']['inputs']['tile_size']=tile
            graph_path=self.directory/f'seedvr_{index:03d}.prompt.json'
            graph_text=json.dumps(graph,ensure_ascii=False,indent=2)
            graph_path.write_text(graph_text,encoding='utf-8')
            graph_path.with_name(f'seedvr_{index:03d}.tile_{tile}.prompt.json').write_text(graph_text,encoding='utf-8')
            response=self.session.post(self.endpoint+'/prompt',json={'client_id':self.id,'prompt':graph},timeout=30)
            response.raise_for_status();submitted=response.json()
            if submitted.get('node_errors') or not submitted.get('prompt_id'):
                raise RuntimeError('SeedVR 工作流校验失败：'+json.dumps(submitted,ensure_ascii=False)[:1500])
            prompt_id=submitted['prompt_id']
            submission.write_text(json.dumps(submitted),encoding='utf-8')
            submission.with_name(f'seedvr_{index:03d}.tile_{tile}.submission.json').write_text(json.dumps(submitted),encoding='utf-8')
            print(f'[SeedVR] chunk {index} tile={tile} prompt_id={prompt_id}',flush=True)
            try:
                return self.collect_seedvr(prompt_id,index,rendered)
            except SeedVROutOfMemory:
                print(f'[SeedVR] tile={tile} 显存不足，使用更小切块重试',flush=True)
                self.wait_gpu(same_server_fifo=True)
        raise RuntimeError('SeedVR 最小切块仍显存不足，请待其他本机程序释放显存后重试')

    def collect_seedvr(self,prompt_id,index,rendered):
        deadline=time.monotonic()+7200
        while time.monotonic()<deadline:
            response=self.session.get(self.endpoint+'/history/'+prompt_id,timeout=15)
            response.raise_for_status();history=response.json().get(prompt_id)
            if history:
                (self.directory/f'seedvr_{index:03d}.history.json').write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
                status=history.get('status',{})
                if status.get('status_str') in ('error','failed','interrupted','cancelled'):
                    raise seedvr_failure(status)
                entries=history.get('outputs',{}).get('15',{}).get('images',[])
                if entries and status.get('status_str')=='success' and status.get('completed') is True:
                    entry=next((v for v in entries if Path(v.get('filename','')).suffix.lower()=='.mp4'),None)
                    if not entry or entry.get('type')!='output':raise RuntimeError('SeedVR 未返回成片')
                    temporary=rendered.with_suffix('.downloading.mp4')
                    with self.session.get(self.endpoint+'/view',params={k:entry[k] for k in ('filename','subfolder','type')},stream=True,timeout=120) as media:
                        media.raise_for_status()
                        with temporary.open('wb') as out:
                            for chunk in media.iter_content(1024*1024):out.write(chunk)
                    os.replace(temporary,rendered)
                    return rendered
            time.sleep(2)
        raise RuntimeError('SeedVR 生成超时，保留任务记录供排查')

    def run(self):
        if fingerprint(self.source)!=self.job['source_sha256']:
            raise RuntimeError('源文件已变化，请重新审核')
        snapshot=self.directory/'source_snapshot.mp4'
        if not snapshot.is_file() or fingerprint(snapshot)!=self.job['source_sha256']:
            shutil.copy2(self.source,snapshot)
        if fingerprint(snapshot)!=self.job['source_sha256']:
            raise RuntimeError('源视频在复制过程中变化，请重新审核')
        self.source=snapshot
        self.stage('检查源视频',2)
        original=self.probe(self.source)
        if original['frames']<1:raise RuntimeError('源视频没有可处理帧')
        w,h=original['video']['width'],original['video']['height']
        width,height=target_size(w,h)
        # For the usual 9:16 or16:9 source this is exactly1080x1920 or1920x1080.
        self.stage('自动检测角落水印并修复画面',8)
        clean=self.directory/'watermark_removed.mp4'
        detection=self.directory/'watermark_report.json'
        vsr_python,runner=_vsr_paths()
        if not clean.is_file():
            self.wait_gpu(progress=8)
            self.command([vsr_python,'-u',runner,'--input',self.source,'--output',clean,'--report',detection],timeout=10800)
        clean_info=self.probe(clean)
        if clean_info['frames']!=original['frames'] or (clean_info['video']['width'],clean_info['video']['height'])!=(w,h):
            raise RuntimeError('去水印结果帧数或分辨率异常，未发布成片')
        self.stage('准备 SeedVR 1080P 分段超分',45)
        max_frames=int(self.config['memory_strategy']['external_clip_recommendation']['max_frames'])
        ranges=clip_ranges(original['frames'],max_frames)
        segments=[]
        fps=str(original['fps'])
        for i,(start,end) in enumerate(ranges):
            clip=self.directory/f'input_{i:03d}.mp4'
            if not clip.is_file():
                self.command([self.ffmpeg,'-nostdin','-y','-v','error','-i',clean,'-an','-vf',
                    f'select=between(n\\,{start}\\,{end}),setpts=N/({fps}*TB)',
                    '-fps_mode','passthrough','-c:v','libx264','-crf','0','-preset','fast',clip])
            self.stage(f'SeedVR 1080P 超分，第 {i+1}/{len(ranges)} 段',48+int(42*i/len(ranges)))
            rendered=self.seedvr_chunk(clip,i,width,height)
            rendered_info=self.probe(rendered)
            if rendered_info['frames']!=end-start+1 or (rendered_info['video']['width'],rendered_info['video']['height'])!=(width,height):
                raise RuntimeError(f'SeedVR 第 {i+1} 段分辨率或帧数不符')
            segment=self.directory/f'assemble_{i:03d}.mp4'
            if not segment.is_file():
                filters=(f'trim=start_frame=1,setpts=N/({fps}*TB)' if i else f'setpts=N/({fps}*TB)')
                self.command([self.ffmpeg,'-nostdin','-y','-v','error','-i',rendered,'-an','-vf',filters,
                    '-r',fps,'-c:v','libx264','-crf','17','-preset','fast','-pix_fmt','yuv420p',segment])
            segments.append(segment)
        self.stage('合并成片并恢复原始音频',92)
        concat=self.directory/'concat.txt'
        entries=[]
        for i,p in enumerate(segments):
            frame_count=ranges[i][1]-ranges[i][0]+1-(1 if i else 0)
            entries.append("file '"+str(p.resolve()).replace('\\','/').replace("'","'\\''")+"'")
            # MP4 edit-list/container duration can round short even for CFR packets.
            # Explicit frame-derived durations keep every following segment on its grid.
            entries.append(f'duration {float(Fraction(frame_count)/original["fps"]):.12f}')
        concat.write_text('\n'.join(entries)+'\n',encoding='utf-8')
        assembled=self.directory/'assembled.mp4'
        self.command([self.ffmpeg,'-nostdin','-y','-v','error','-f','concat','-safe','0','-i',concat,'-c','copy',assembled])
        APPROVED_DIR.mkdir(parents=True,exist_ok=True)
        pending=self.directory/'publish.pending.mp4'
        self.command([self.ffmpeg,'-nostdin','-y','-v','error','-i',assembled,'-i',self.source,
            '-map','0:v:0','-map','1:a?','-map_metadata','1','-c','copy','-movflags','+faststart+use_metadata_tags',pending])
        self.stage('验证完整视频与 1080P 结果',97)
        final=self.probe(pending)
        if (final['frames']!=original['frames'] or final['fps']!=original['fps']
                or (final['video']['width'],final['video']['height'])!=(width,height)):
            raise RuntimeError('最终视频验证失败，未发布到审核目录')
        if any(s['codec_type']=='audio' for s in original['streams']) and not any(s['codec_type']=='audio' for s in final['streams']):
            raise RuntimeError('最终视频缺失原音频，未发布')
        self.command([self.ffmpeg,'-nostdin','-v','error','-i',pending,'-f','null','-'],timeout=900)
        output=APPROVED_DIR/self.job['name']
        os.replace(pending,output)
        receipt=dict(job_id=self.id,source=self.job['source'],source_sha256=self.job['source_sha256'],
            output=str(output),output_sha256=fingerprint(output),width=width,height=height,
            frames=final['frames'],fps=fps,seedvr_models=self.config['models'],
            watermark_report=str(detection),finished=_now(),full_decode_passed=True)
        (APPROVED_DIR/(Path(self.job['name']).stem+'.processing.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        self.stage('去水印与 1080P 超分完成',100,status='done',output_path=str(output),error='')


def worker_main():
    TEMP_DIR.mkdir(parents=True,exist_ok=True)
    with (TEMP_DIR/'postprocess_worker.lock').open('a+b') as guard:
        guard.seek(0,2)
        if not guard.tell():guard.write(b'0');guard.flush()
        if not _try_lock(guard):return
        try:
            code_revision=Path(__file__).stat().st_mtime_ns
            with store().txn() as data:
                for job in data.get('jobs',[]):
                    if job['status']=='running':
                        job.update(status='failed',stage='处理被中断，可重试',error='处理进程意外中断，保留中间文件')
            while True:
                with store().txn() as data:
                    job=next((j for j in data.get('jobs',[]) if j['status']=='queued'),None)
                    if job:job.update(status='running',stage='开始审核后处理',worker_pid=os.getpid());job=dict(job)
                if not job:
                    if Path(__file__).stat().st_mtime_ns!=code_revision:return
                    time.sleep(1)
                    continue
                with open(job['log'],'a',encoding='utf-8',buffering=1) as log:
                    oldout,olderr=sys.stdout,sys.stderr
                    sys.stdout=sys.stderr=log
                    try:Processor(job).run()
                    except Exception as exc:
                        import traceback
                        traceback.print_exc()
                        update(job['id'],status='failed',stage='处理失败，可重试',error=str(exc)[-2500:])
                    finally:sys.stdout,sys.stderr=oldout,olderr
        finally:
            _unlock(guard)
