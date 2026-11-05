"""总编排：飞书 → Gemini → 精确分段 → 逐账号双条批次 → 尾帧与本地拼接。

状态全部持久化在 workspace/state/pipeline_state.json，任何一步中断都可重跑续接。
"""
import threading
import time
import traceback
import uuid
import os
from contextlib import contextmanager
from pathlib import Path

from .account_manager import AccountManager
from .browser_pool import BrowserSession
from .config import get, require_doubao_accounts
from .doubao_agent import DoubaoAgent, GenerationFailed, NeedsLogin, QuotaExhausted
from .doubao_batch import verified_download_matches
from .feishu_client import FeishuClient, FeishuError
from .gemini_web import GeminiError, GeminiNotLoggedIn, GeminiSubmissionUncertain, GeminiWeb
from .paths import (FRAMES_DIR, ROOT, SCRIPTS_DIR, STATE_DIR, VIDEOS_DIR,
                    ensure_dirs, resolve, safe_filename)
from .script_splitter import build_segments, parse_scripts, detect_total_seconds
from .state import JsonStore
from .video_utils import concat_videos, extract_last_frame, find_ffmpeg

_INITIAL = {"products": [], "sources": [], "fixed_prompt": "", "scripts": [], "tasks": []}


def _valid_remote_ref(value):
    return isinstance(value,dict) and any(value.get(k) for k in ('key','url','src'))


def _uses_source_prompt_column(cfg):
    """An automatically scanned source column is row data, not a global template."""
    fixed = get(cfg, 'feishu.fixed_prompt', {}) or {}
    source = get(cfg, 'feishu.scripts_table', {}) or {}
    table = str(fixed.get('table_id', '') or '').strip()
    field = str(fixed.get('field', '') or '').strip()
    return (fixed.get('source', 'file') == 'bitable'
            and int(fixed.get('record_index', -1)) < 0
            and bool(table) and table == str(source.get('table_id', '') or '').strip()
            and bool(field) and field == str(source.get('prompt_field', '提示词') or '').strip())


class Pipeline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        ensure_dirs()
        self.store = JsonStore(STATE_DIR / "pipeline_state.json", initial=_INITIAL)
        names = [a["name"] for a in (get(cfg, "accounts.doubao") or []) if a.get("enabled", True)]
        self.account_names = names
        self.accounts = AccountManager(JsonStore(STATE_DIR / "accounts.json"), names)
        self.cond = threading.Condition()
        self.ffmpeg = None  # 延迟初始化，避免未用到视频的 stage 也要求 ffmpeg
        self._attempted_requests = set()
        self._local_attempts_this_run = {}
        self._local_blocked_tasks = set()

    # ================= Stage 1: 飞书 =================
    def stage_fetch(self, force: bool = False):
        snap = self.store.snapshot()
        if snap.get("products") and snap.get("sources") and not force:
            print(f"[fetch] 已有 {len(snap['products'])} 个产品、{len(snap['sources'])} 条源记录，跳过（--force 强制重拉）")
            return
        print("[fetch] 连接飞书，下载产品图 / 提示词 / 视频 / 固定指令 …")
        with FeishuClient(self.cfg) as fc:
            products = fc.fetch_products()
            sources = fc.fetch_sources()
            fixed = fc.load_fixed_prompt()
        if not products:
            raise FeishuError("产品图表没有下载到任何图片，请检查 feishu.products_table 配置与附件列名")
        with self.store.txn() as data:
            data["products"] = products
            data["sources"] = sources
            data["fixed_prompt"] = fixed
        print(f"[fetch] 完成：产品 {len(products)} 个，源记录 {len(sources)} 条，固定指令 {len(fixed)} 字")

    # ================= Stage 2: Gemini 生成脚本 =================
    def stage_scripts(self, force: bool = False, limit: int = 0):
        with self._run_lock():
            return self._stage_scripts_locked(force=force,limit=limit)

    def _stage_scripts_locked(self, force=False, limit=0):
        snap = self.store.snapshot()
        if snap.get("scripts") and not force:
            print(f"[scripts] 已有 {len(snap['scripts'])} 个脚本，跳过（--force 强制重新生成）")
            return
        sources = snap.get("sources") or []
        if not sources:
            raise RuntimeError("还没有源数据，请先执行: python run_pipeline.py fetch")
        row_prompt = _uses_source_prompt_column(self.cfg)
        fixed = "" if row_prompt else snap.get("fixed_prompt") or ""
        if not fixed and not row_prompt:
            p = resolve(get(self.cfg, "feishu.fixed_prompt.file", "config/gemini_prompt.txt"))
            fixed = p.read_text(encoding="utf-8").strip() if p.exists() else ""
        if not fixed and not row_prompt:
            raise RuntimeError("固定提示词指令为空：请在 config.yaml 配置来源或写入 config/gemini_prompt.txt")

        existing = list(snap.get("scripts") or [])
        have_idx = {int(sc.get("source_index", -1)) for sc in existing} if not force else set()
        scripts_out = [] if force else existing
        generation = uuid.uuid4().hex[:10] if force else ""
        todo_idx = [si for si in range(len(sources)) if si not in have_idx]
        if limit:
            todo_idx = todo_idx[:limit]
        messages = {}
        for si in todo_idx:
            src = sources[si]
            extra = (src.get("prompt_text") or "").strip()
            if row_prompt:
                if not extra:
                    raise RuntimeError(f"源记录 {src.get('record_id')} 的提示词为空：固定指令配置指向素材表同一列，不能借用其他记录的提示词")
                messages[si] = extra
            else:
                message = fixed
                if extra and extra != fixed.strip():
                    message += "\n\n【本次附加提示词】\n" + extra
                messages[si] = message

        gemini_acct = get(self.cfg, "accounts.gemini") or {}
        session = BrowserSession(
            gemini_acct.get("name", "gemini"),
            gemini_acct.get("profile", "profiles/gemini"),
            self.cfg,
            start_url=get(self.cfg, "login.gemini_url", "https://gemini.google.com/app"),
        )
        session.start()
        try:
            gw = GeminiWeb(session, self.cfg)
            gw.wait_logged_in()
            retries = int(get(self.cfg, "gemini.retry_on_error", 2))
            for si in todo_idx:
                src = sources[si]
                message = messages[si]
                text = ""
                for attempt in range(retries + 1):
                    try:
                        text = gw.generate(src.get("videos") or [], message)
                        break
                    except (GeminiNotLoggedIn, GeminiSubmissionUncertain):
                        raise
                    except GeminiError as e:
                        print(f"[scripts] 源 {src.get('record_id')} 第 {attempt + 1} 次失败: {e}")
                        if attempt < retries:
                            time.sleep(60)
                if not text:
                    print(f"[scripts] ⚠ 源 {src.get('record_id')} 生成失败，跳过")
                    continue
                parsed = parse_scripts(text)
                print(f"[scripts] 源 {src.get('record_id')} → 本次保留 {len(parsed)} 个脚本")
                for j, body in enumerate(parsed):
                    prefix = snap.get("run_id", "")
                    if generation:
                        prefix = (prefix + "_" if prefix else "") + "g" + generation
                    sid = f"{prefix}_s{si}_{j + 1}" if prefix else f"s{si}_{j + 1}"
                    fpath = SCRIPTS_DIR / f"{sid}_{safe_filename(src.get('record_id', 'src'))}.txt"
                    fpath.write_text(body, encoding="utf-8")
                    scripts_out.append({
                        "id": sid, "source_index": si,
                        "source_record": src.get("record_id", ""),
                        "keyword": src.get("keyword", ""),
                        "file": str(fpath), "text": body,
                        "total_seconds": detect_total_seconds(body),
                    })
            if not scripts_out:
                raise RuntimeError("Gemini 没有产出任何脚本")
            with self.store.txn() as data:
                data["scripts"] = scripts_out
                if force:
                    data.update(tasks=[],requests={},assemblies={})
        finally:
            session.stop()

    # ================= Stage 3: 切分规划 =================
    def _product_for_script(self, script: dict, products: list) -> dict:
        keyword = (script.get("keyword") or "").strip()
        matches = [p for p in products if keyword and (p.get("keyword") or "").strip() == keyword]
        if not matches:
            raise RuntimeError(f"脚本 {script.get('id')} keyword={keyword!r} 无对应产品图，停止以免错配")
        return matches[0]

    def stage_plan(self, force: bool = False):
        with self._run_lock():
            return self._stage_plan_locked(force=force)

    def _stage_plan_locked(self, force=False):
        snap = self.store.snapshot()
        if snap.get("tasks") and not force:
            print(f"[plan] 已有 {len(snap['tasks'])} 个任务，跳过（--force 强制重新规划）")
            return
        scripts = snap.get("scripts") or []
        if force:
            revision=uuid.uuid4().hex[:10]
            scripts=[dict(sc,id=f"{sc.get('origin_id',sc['id'])}_p{revision}",
                          origin_id=sc.get('origin_id',sc['id'])) for sc in scripts]
        products = snap.get("products") or []
        if not scripts:
            raise RuntimeError("还没有脚本，请先执行: python run_pipeline.py scripts")
        tasks = []
        for sc in scripts:
            keyword = (sc.get("keyword") or "").strip()
            matches = [p for p in products if keyword and (p.get("keyword") or "").strip() == keyword]
            if not matches:
                raise RuntimeError(f"脚本 {sc['id']} keyword={keyword!r} 找不到对应产品图；已停止，防止串用其他商品")
            product = matches[0]
            segs = build_segments(sc["text"], self.cfg, product_name=product.get("name", ""))
            if not segs:
                print(f"[plan] ⚠ 脚本 {sc['id']} 切分为空，跳过")
                continue
            if any(not 0<float(seg['est_seconds'])<=15 for seg in segs):
                raise ValueError('规划中的每段必须大于0且不超过15秒，未提交任何视频生成')
            total_est = round(sum(s["est_seconds"] for s in segs), 1)
            print(f"[plan] 脚本 {sc['id']}: {len(segs)} 段，预计总时长 {total_est}s，产品「{product.get('name', '')}」")
            for seg in segs:
                tasks.append({
                    "id": f"{sc['id']}#seg{seg['index']}",
                    "script_id": sc["id"],
                    "seg_index": seg["index"],
                    "seg_total": seg["total"],
                    "prompt": seg["prompt"],
                    "narration": seg["narration"],
                    "est_seconds": seg["est_seconds"],
                    "target_seconds": seg["est_seconds"],
                    "start_seconds": seg.get("start_seconds", sum(s["est_seconds"] for s in segs[:seg["index"]])),
                    "end_seconds": seg.get("end_seconds", sum(s["est_seconds"] for s in segs[:seg["index"] + 1])),
                    "product_images": list(product.get("images") or []),
                    "status": "pending",
                    "account": "",
                    "attempts": 0,
                    "video": "",
                    "error": "",
                    "request_id": "",
                    "raw_video": "",
                    "remote_ref": {},
                })
        if not tasks:
            raise RuntimeError("规划结果为空：所有脚本切分失败")
        with self.store.txn() as data:
            data["tasks"] = tasks
            if force:
                data.update(scripts=scripts,requests={},assemblies={})
        print(f"[plan] 共规划 {len(tasks)} 个分段任务")

    # ================= Stage 4: 逐账号、同脚本连续双条批次 =================
    def _task_images(self, task):
        if task['seg_index']==0:return list(task.get('product_images') or [])
        frame=FRAMES_DIR/f"{task['script_id']}_seg{task['seg_index']-1}_end.png"
        if not frame.is_file():raise RuntimeError('前一批尾帧尚未就绪，不能发送后续批次')
        images=[str(frame)]
        if get(self.cfg,'video.later_segment_images','prev_frame')=='prev_frame_and_product':
            images+=list(task.get('product_images') or [])
        return images

    @contextmanager
    def _run_lock(self):
        """Only one process may submit against this persistent task journal."""
        owner=threading.get_ident()
        if getattr(self,'_run_lock_owner',None)==owner:
            yield
            return
        path=self.store.path.with_suffix('.runner.lock')
        with path.open('a+b') as handle:
            handle.seek(0,os.SEEK_END)
            if not handle.tell():handle.write(b'0');handle.flush()
            handle.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('已有流水线正在处理这份任务，不会重复启动生成') from exc
            self._run_lock_owner=owner
            try:yield
            finally:
                self._run_lock_owner=None
                handle.seek(0)
                if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle,fcntl.LOCK_UN)

    def _finish(self, task_id, **fields):
        with self.cond:
            with self.store.txn() as data:
                for task in data.get('tasks',[]):
                    if task['id']==task_id:task.update(fields);break
            self.cond.notify_all()

    def _progress(self):
        tasks=self.store.snapshot().get('tasks',[])
        done=sum(t['status']=='done' for t in tasks)
        failed=sum(t['status']=='failed' for t in tasks)
        running=sum(t['status'] in ('running','reserved') for t in tasks)
        return done,running,len(tasks)-done-failed-running,failed,len(tasks)

    def _all_finished(self):
        done,running,pending,failed,total=self._progress()
        return not (running+pending)

    def _no_way_forward(self):
        return self._all_finished() or not self.accounts.any_available()

    def _request_tasks(self, request):
        tasks={t['id']:t for t in self.store.snapshot().get('tasks',[])}
        return [dict(tasks[tid]) for tid in request['task_ids'] if tid in tasks
                and tasks[tid].get('request_id')==request.get('request_id')]

    def _task_retry_history(self,requests):
        """Separate proven unsent setup failures from real cloud failures in old task counters."""
        history={}
        def item(tid):return history.setdefault(tid,dict(cancelled=set(),refunded=set(),submitted=False))
        for acc in self.accounts.store.snapshot().get('accounts',{}).values():
            saved=acc.get('quota_requests',{})
            for day in acc.get('quota_days',{}).values():
                for entry in day.get('entries',{}).values():
                    tid=entry.get('task_id');rid=entry.get('request_id')
                    if not tid or not rid:continue
                    current=item(tid);record=saved.get(rid,{})
                    journal=requests.get(rid,{})
                    if (entry.get('state')=='cancelled' and record.get('submitted') is False
                            and journal.get('submitted') is not True):current['cancelled'].add(rid)
                    if entry.get('rejection',{}).get('kind')=='generation_failed_refunded':current['refunded'].add(rid)
                    if (record.get('submitted') or journal.get('submitted')
                            or entry.get('state') in ('submitted','unknown','charged')):current['submitted']=True
        for request in requests.values():
            if request.get('submitted') or request.get('phase') in ('submitted','unknown','downloading','complete','refunded'):
                for tid in request.get('original_task_ids',request.get('task_ids',[])):item(tid)['submitted']=True
        return history

    @staticmethod
    def _retry_counts(task,history,refund_gap=False):
        local=len(history.get('cancelled',()))
        cloud=int(task.get('cloud_attempts',max(0,int(task.get('attempts',0))-local)))
        if refund_gap:cloud+=1
        return max(cloud,len(history.get('refunded',()))),max(int(task.get('local_attempts',0)),local)

    def _recover_state(self):
        """Repair the journal/ledger gap without ever converting an uncertain send into a new send."""
        with self.store.txn() as data:
            requests=data.setdefault('requests',{})
            history=self._task_retry_history(requests)
            for task in data.get('tasks',[]):
                task.setdefault('target_seconds',task.get('est_seconds',15))
                task.setdefault('raw_video','')
                task.setdefault('remote_ref',{})
                record=self.accounts.find_task(task['id'])
                if record:
                    rid=record['request_id'];saved=record['request']
                    entries=self.accounts.request(record['account'],rid).get('entries',{})
                    active_ids=[tid for tid in saved['task_ids']
                                if entries.get(tid,{}).get('state') not in ('cancelled','rejected')]
                    request=requests.setdefault(rid,dict(
                        request_id=rid,account=record['account'],task_ids=active_ids,
                        script_id=task['script_id'],phase='submitted' if saved.get('submitted') else 'reserved',
                        submitted=bool(saved.get('submitted')),chat_url=saved.get('chat_url',''),
                        baseline=saved.get('baseline',{}),continuation_safe=False))
                    request.setdefault('request_id',rid)
                    request.setdefault('account',record['account'])
                    request.setdefault('original_task_ids',list(saved['task_ids']))
                    if saved.get('refunded_task_ids'):
                        request['refunded_task_ids']=list(saved['refunded_task_ids'])
                    request['task_ids']=active_ids
                    # The account ledger is durable even if the process stopped before journal update.
                    if saved.get('submitted'):
                        request.update(submitted=True,chat_url=saved.get('chat_url') or request.get('chat_url',''),
                                       baseline=saved.get('baseline') or request.get('baseline',{}))
                    task.update(account=record['account'],request_id=rid)
                    if record.get('remote_ref'):task['remote_ref']=record['remote_ref']
                    if task['status']!='done':
                        cloud,local=self._retry_counts(task,history.get(task['id'],{}))
                        task.update(attempts=cloud,cloud_attempts=cloud,local_attempts=local)
                        task['status']='awaiting_result' if saved.get('submitted') else 'reserved'
                else:
                    inactive=self.accounts.find_task(task['id'],include_inactive=True)
                    old=task.get('request_id','')
                    if old and task.get('account'):
                        bound=self.accounts.request(task['account'],old)
                        entry=bound.get('entries',{}).get(task['id'],{})
                        if entry.get('state') in ('cancelled','rejected'):
                            inactive=dict(account=task['account'],date=bound.get('date',''),
                                          **entry,request=bound)
                    if inactive and inactive['state'] in ('cancelled','rejected') and task['status']!='done':
                        stats=history.get(task['id'],{})
                        local_failure=(inactive['state']=='cancelled' and inactive['request_id'] in stats.get('cancelled',()))
                        if inactive['state']=='cancelled' and not local_failure:
                            task.update(status='needs_user',error='任务取消与提交记录不一致，需核对原请求，不能自动重发')
                            continue
                        refunded=inactive.get('rejection',{}).get('kind')=='generation_failed_refunded'
                        if (inactive['state']=='rejected' and not refunded and inactive['account'] in self.accounts.names
                                and inactive['date']==self.accounts.quota(inactive['account'])['date']):
                            self.accounts.mark_exhausted(inactive['account'],'最新回复明确拒绝了该批额度')
                        attempts,local=self._retry_counts(task,stats,refund_gap=refunded and old==inactive['request_id'])
                        exhausted=attempts>=int(get(self.cfg,'doubao.max_attempts_per_task',3))
                        task.update(status='failed' if exhausted else 'pending',attempts=attempts,cloud_attempts=attempts,
                                    local_attempts=local,local_blocked=False,
                                    account='',request_id='',remote_ref={},raw_video='',
                                    failure_phase='before_submit' if local_failure else 'submitted',
                                    error=('发送前准备失败已恢复，继续原计划' if local_failure else
                                           '失败视频已确认未扣额度，可重新请求' if refunded else '原请求明确未生成，可由下一个账号继续'))
                        if old in requests:
                            request=requests[old]
                            if local_failure:
                                request.update(phase='cancelled',submitted=False,failure_phase='before_submit')
                                continue
                            request.setdefault('original_task_ids',list(request.get('task_ids',[])))
                            request['task_ids']=[tid for tid in request.get('task_ids',[]) if tid!=task['id']]
                            if refunded:
                                request['refunded_task_ids']=sorted(set(request.get('refunded_task_ids',[]))|{task['id']})
                            if not request['task_ids']:request['phase']='refunded' if refunded else 'rejected'
                    elif task['status'] in ('running','reserved','awaiting_result'):
                        task.update(status='needs_user',error='旧任务提交状态不明；请核对原会话，禁止自动重新生成')
            # Legacy versions permanently failed later untouched segments after a setup failure.
            for task in sorted(data.get('tasks',[]),key=lambda t:(t['script_id'],t['seg_index'])):
                if task['status']!='failed' or task.get('error')!='前置分段永久失败':continue
                if (history.get(task['id'],{}).get('submitted') or task.get('request_id')
                        or _valid_remote_ref(task.get('remote_ref')) or task.get('raw_video')):continue
                earlier=[t for t in data['tasks'] if t['script_id']==task['script_id'] and t['seg_index']<task['seg_index']]
                if not any(t['status']=='failed' for t in earlier):
                    task.update(status='pending',error='前置发送前故障已解除，继续原计划',failure_phase='before_submit')
            by_id={t['id']:t for t in data.get('tasks',[])}
            for rid,request in requests.items():
                if (request.get('refunded_task_ids')
                        and not any(by_id.get(tid,{}).get('request_id')==rid and by_id[tid]['status']!='done'
                                    for tid in request.get('task_ids',[]))):
                    request['phase']='refunded'
        # Local work can be retried with no browser, no quota and no new remote request.
        for task in self.store.snapshot().get('tasks',[]):
            if task['status']=='done':continue
            raw=Path(task.get('raw_video') or VIDEOS_DIR/f"{task['script_id']}_seg{task['seg_index']}.raw.mp4")
            if verified_download_matches(raw, task.get('remote_ref', {})):
                try:self._finalize_local(task,raw)
                except Exception as exc:self._finish(task['id'],status='local_failed',raw_video=str(raw),error=str(exc)[:500])

    def _claim_batch(self, account, limit=2):
        attempted=getattr(self,'_attempted_requests',set())
        local_blocked=getattr(self,'_local_blocked_tasks',set())
        with self.cond:
            with self.store.txn() as data:
                tasks=data.get('tasks',[]);requests=data.setdefault('requests',{})
                by_id={t['id']:t for t in tasks}
                for rid,request in requests.items():
                    if request.get('account')!=account or rid in attempted:continue
                    batch=[by_id[tid] for tid in request.get('task_ids',[]) if tid in by_id
                           and by_id[tid].get('request_id')==rid]
                    if not batch or all(t['status']=='done' for t in batch):continue
                    if any(t['status'] != 'done' and verified_download_matches(
                            Path(t.get('raw_video') or VIDEOS_DIR/f"{t['script_id']}_seg{t['seg_index']}.raw.mp4"),
                            t.get('remote_ref', {})) for t in batch):
                        continue  # A local trim/frame failure must not cause more generation.
                    if request.get('phase') in ('reserved','submitted','unknown','downloading'):
                        for task in batch:
                            if task['status']!='done':task['status']='running'
                        return [dict(t) for t in batch]
                quota=self.accounts.quota(account)
                if quota['remaining']<1 or quota['unknown']:return None
                failed={t['script_id']:min(x['seg_index'] for x in tasks if x['script_id']==t['script_id'] and x['status']=='failed')
                        for t in tasks if t['status']=='failed'}
                for task in tasks:
                    if task['status']=='pending' and task['script_id'] in failed and task['seg_index']>failed[task['script_id']]:
                        task.update(status='failed',error='前置分段永久失败')
                first=None
                for task in tasks:
                    if task['status']!='pending' or task['id'] in local_blocked:continue
                    earlier=[t for t in tasks if t['script_id']==task['script_id'] and t['seg_index']<task['seg_index']]
                    if all(t['status']=='done' for t in earlier):first=task;break
                if first is None:return None
                batch=[first]
                capacity=min(2,int(limit),quota['remaining'])
                for task in tasks:
                    if (len(batch)<capacity and task['script_id']==first['script_id'] and task['status']=='pending'
                            and task['id'] not in local_blocked
                            and task['seg_index']==batch[-1]['seg_index']+1):batch.append(task)
                if any(not 0<float(t['target_seconds'])<=15 for t in batch):
                    raise ValueError('每个视频的目标时长必须大于0且不超过15秒')
                rid='request_'+uuid.uuid4().hex
                self.accounts.reserve(account,[t['id'] for t in batch],rid)
                previous=[r for r in requests.values() if r.get('script_id')==first['script_id'] and r.get('account')==account
                          and r.get('phase')=='complete' and r.get('continuation_safe')]
                requests[rid]=dict(request_id=rid,account=account,task_ids=[t['id'] for t in batch],
                                   script_id=first['script_id'],phase='reserved',submitted=False,
                                   chat_url=previous[-1].get('chat_url','') if previous else '',baseline={},continuation_safe=False)
                for task in batch:task.update(status='reserved',account=account,request_id=rid,error='')
                return [dict(t) for t in batch]

    def _claim(self, account):
        batch=self._claim_batch(account,limit=1)
        return batch[0] if batch else None

    def _finalize_local(self,task,raw):
        from .video_utils import trim_video
        if not 0<float(task['target_seconds'])<=15:
            raise ValueError('旧分段目标不在0至15秒范围，需重新规划，不能按旧结果自动完成')
        if self.ffmpeg is None:self.ffmpeg=find_ffmpeg(get(self.cfg,'video.ffmpeg','auto'))
        dest=VIDEOS_DIR/f"{task['script_id']}_seg{task['seg_index']}.mp4"
        trim_video(Path(raw),dest,float(task['target_seconds']),ffmpeg=self.ffmpeg)
        if task['seg_index']+1<task['seg_total']:
            frame=FRAMES_DIR/f"{task['script_id']}_seg{task['seg_index']}_end.png"
            extract_last_frame(dest,frame,self.ffmpeg,float(get(self.cfg,'video.tail_frame_offset_seconds',0.3)))
        self._finish(task['id'],status='done',video=str(dest),raw_video=str(raw),error='',tail_ready=True)

    def _assemble_all(self):
        snap=self.store.snapshot();tasks=snap.get('tasks',[])
        for sid in dict.fromkeys(t['script_id'] for t in tasks):
            group=sorted((t for t in tasks if t['script_id']==sid),key=lambda t:t['seg_index'])
            if not group or any(t['status']!='done' for t in group):continue
            if len(group)==1:continue
            existing=snap.get('assemblies',{}).get(sid,{})
            out=VIDEOS_DIR/f'{sid}_final.mp4'
            if existing.get('status')=='done' and out.is_file():continue
            with self.store.txn() as data:
                data.setdefault('assemblies',{})[sid]=dict(status='running',output=str(out),error='',
                    target_seconds=sum(float(t['target_seconds']) for t in group))
            try:
                if self.ffmpeg is None:self.ffmpeg=find_ffmpeg(get(self.cfg,'video.ffmpeg','auto'))
                concat_videos([Path(t['video']) for t in group],out,self.ffmpeg,
                              target_seconds=sum(float(t['target_seconds']) for t in group))
            except Exception as exc:
                with self.store.txn() as data:data['assemblies'][sid].update(status='failed',error=str(exc)[:500])
            else:
                with self.store.txn() as data:data['assemblies'][sid].update(status='done',error='')

    def _execute_batch(self,agent,account,tasks):
        rid=tasks[0]['request_id'];ids=[t['id'] for t in tasks]
        self._attempted_requests.add(rid)
        request=self.store.snapshot()['requests'][rid]
        ledger=self.accounts.request(account,rid)
        submitted=bool(ledger.get('submitted') or request.get('submitted'))
        script=next((s for s in self.store.snapshot().get('scripts',[]) if s['id']==tasks[0]['script_id']),{})
        script_tasks=[t for t in self.store.snapshot().get('tasks',[]) if t['script_id']==tasks[0]['script_id']]
        context=dict(script_text=script.get('text',''),total_seconds=sum(float(t['target_seconds']) for t in script_tasks),
                     chat_url=request.get('chat_url',''),request_id=rid,baseline=request.get('baseline',{}),
                     task_ids=ids,original_task_ids=list(request.get('original_task_ids') or ledger.get('task_ids') or ids),
                     remote_refs={t['id']:t['remote_ref'] for t in tasks
                                               if _valid_remote_ref(t.get('remote_ref'))},resume_only=submitted)
        dests={t['id']:VIDEOS_DIR/f"{t['script_id']}_seg{t['seg_index']}.raw.mp4" for t in tasks}

        def on_quota_remaining(count):
            self.accounts.observe_remaining(account,count)
            if not context['resume_only']:
                q=self.accounts.quota(account)
                if count==0:raise QuotaExhausted('平台明确显示剩余额度为0')
                if q['used']+q['reserved']+q['unknown']>q['limit']:
                    raise GenerationFailed('平台余额少于本批次，请先释放预留再缩小批次')

        context['on_quota_remaining']=on_quota_remaining

        def on_submitted(chat_url,baseline):
            self.accounts.mark_submitted(account,rid,chat_url=chat_url,baseline=baseline)
            with self.store.txn() as data:
                data['requests'][rid].update(phase='submitted',submitted=True,chat_url=chat_url,baseline=baseline)
                for task in data['tasks']:
                    if task['id'] in ids and task['status']!='done':task['status']='running'

        def on_generated(task_id,remote_ref):
            if task_id not in ids:raise ValueError('生成产物不属于当前预留批次')
            if not _valid_remote_ref(remote_ref):raise ValueError('产物没有稳定标识，不能确认生成或扣费')
            self.accounts.record_generated(account,task_id,request_id=rid,remote_ref=remote_ref)
            self._finish(task_id,remote_ref=remote_ref,generation_phase='generated')

        try:
            images=[] if submitted else self._task_images(tasks[0])
            result=agent.run_batch(tasks,images,dests,context,on_submitted,on_generated)
        except Exception as exc:
            kind='quota' if isinstance(exc,QuotaExhausted) else 'login' if isinstance(exc,NeedsLogin) else 'failed'
            result=dict(videos={},remote_refs={},unresolved=ids,submitted=self.accounts.request(account,rid).get('submitted',False),
                        chat_url=context['chat_url'],error_kind=kind,error=str(exc),continuation_safe=False)
        if not isinstance(result,dict):raise RuntimeError('生成程序未返回批次结果')
        if isinstance(result.get('quota_remaining'),int):
            # Observation after submission is accounting-only; it cannot cancel an accepted request.
            self.accounts.observe_remaining(account,result['quota_remaining'])
        for tid,ref in result.get('remote_refs',{}).items():
            if tid in ids and _valid_remote_ref(ref):on_generated(tid,ref)
        for task in tasks:
            raw=result.get('videos',{}).get(task['id'])
            if not raw or task['status']=='done':continue
            raw=Path(raw)
            if not raw.is_file():continue
            saved=next(t for t in self.store.snapshot()['tasks'] if t['id']==task['id'])
            ref=result.get('remote_refs',{}).get(task['id']) or saved.get('remote_ref',{})
            if not _valid_remote_ref(ref):continue
            on_generated(task['id'],ref)
            self._finish(task['id'],raw_video=str(raw),status='downloaded')
            try:self._finalize_local(task,raw)
            except Exception as exc:self._finish(task['id'],status='local_failed',raw_video=str(raw),error=str(exc)[:500])
        current={t['id']:t for t in self.store.snapshot()['tasks']}
        unresolved=[tid for tid in ids if current[tid]['status']!='done']
        ledger=self.accounts.request(account,rid)
        submitted=bool(result.get('submitted') or ledger.get('submitted'))
        error=str(result.get('error') or '')[:500];kind=result.get('error_kind','')
        with self.store.txn() as data:
            data['requests'][rid].update(chat_url=result.get('chat_url') or data['requests'][rid].get('chat_url',''),
                                        continuation_safe=bool(result.get('continuation_safe')),error_kind=kind,error=error)
        if not unresolved:
            with self.store.txn() as data:
                saved=data['requests'][rid]
                saved['phase']='refunded' if saved.get('refunded_task_ids') else 'complete'
            self._assemble_all()
            return True
        refunded=[]
        if submitted and kind=='partial_failed_refunded' and result.get('continuation_safe') is True:
            # A failed slot remains its original slot; never infer it from the number/order of successes.
            try:
                rejected=result.get('rejected_task_ids')
                if (not isinstance(rejected,(list,tuple)) or not rejected
                        or any(tid not in ids for tid in rejected)):
                    raise ValueError('缺少本批次确切失败任务编号')
                refunded=self.accounts.release_failed_refunded(account,rid,rejected,result.get('rejection_evidence'))
            except ValueError as exc:
                with self.store.txn() as data:data['requests'][rid]['rejection_error']=str(exc)
            if refunded:
                with self.store.txn() as data:
                    saved=data['requests'][rid]
                    saved.setdefault('original_task_ids',list(saved['task_ids']))
                    saved['refunded_task_ids']=sorted(set(saved.get('refunded_task_ids',[]))|set(refunded))
                    saved['rejected_task_ids']=sorted(set(saved.get('rejected_task_ids',[]))|set(refunded))
                    saved['rejection_evidence']=result['rejection_evidence']
                    saved['task_ids']=[tid for tid in saved['task_ids'] if tid not in refunded]
                    for task in data['tasks']:
                        if task['id'] in refunded:
                            attempts=int(task.get('cloud_attempts',task.get('attempts',0)))+1
                            task.update(status='failed' if attempts>=int(get(self.cfg,'doubao.max_attempts_per_task',3)) else 'pending',
                                        attempts=attempts,cloud_attempts=attempts,account='',request_id='',remote_ref={},raw_video='',error=error)
                unresolved=[tid for tid in unresolved if tid not in refunded]
            if not unresolved:
                with self.store.txn() as data:data['requests'][rid]['phase']='refunded'
                self._assemble_all()
                return True
        if submitted and kind=='quota' and result.get('continuation_safe') is True:
            # This is a definitive refusal, not a timeout or an uncertain accepted request.
            evidence=dict(kind='quota',definitive=True,active=False,source='latest_assistant')
            released=self.accounts.release_rejected(account,rid,unresolved,evidence)
            if released:
                with self.store.txn() as data:
                    saved=data['requests'][rid]
                    saved.setdefault('original_task_ids',list(saved['task_ids']))
                    saved['rejected_task_ids']=sorted(set(saved.get('rejected_task_ids',[]))|set(released))
                    saved['task_ids']=[tid for tid in saved['task_ids'] if tid not in released]
                    for task in data['tasks']:
                        if task['id'] in released:
                            task.update(status='pending',account='',request_id='',remote_ref={},raw_video='',error=error)
                unresolved=[tid for tid in unresolved if tid not in released]
            if not unresolved:
                with self.store.txn() as data:data['requests'][rid]['phase']='rejected'
                self.accounts.mark_exhausted(account,error)
                return False
        if submitted:
            self.accounts.mark_unknown(account,rid,task_ids=unresolved,note=error)
            with self.store.txn() as data:
                data['requests'][rid].update(phase='unknown',submitted=True)
                for task in data['tasks']:
                    if task['id'] in unresolved and not task.get('raw_video'):
                        task.update(status='needs_user' if kind=='needs_user' else 'awaiting_result',error=error)
        else:
            self.accounts.release_unsubmitted(account,rid)
            local_counts=getattr(self,'_local_attempts_this_run',{})
            local_blocked=getattr(self,'_local_blocked_tasks',set())
            self._local_attempts_this_run=local_counts;self._local_blocked_tasks=local_blocked
            local_limit=max(1,int(get(self.cfg,'doubao.max_local_attempts_per_run',3)))
            with self.store.txn() as data:
                data['requests'][rid].update(phase='cancelled',failure_phase='before_submit',recoverable=True)
                for task in data['tasks']:
                    if task['id'] in unresolved:
                        local_counts[task['id']]=local_counts.get(task['id'],0)+1
                        blocked=local_counts[task['id']]>=local_limit
                        if blocked:local_blocked.add(task['id'])
                        cloud=int(task.get('cloud_attempts',task.get('attempts',0)))
                        task.update(status='pending',request_id='',account='',attempts=cloud,cloud_attempts=cloud,
                                    local_attempts=int(task.get('local_attempts',0))+1,local_blocked=blocked,
                                    failure_phase='before_submit',error=error)
        if kind=='quota':self.accounts.mark_exhausted(account,error)
        elif kind=='login':self.accounts.mark_needs_login(account,error)
        return ((bool(refunded) and not self.accounts.quota(account)['unknown'])
                or (not submitted and kind not in ('quota','login')))

    def _execute(self,agent,account,task):
        request=self.store.snapshot().get('requests',{}).get(task.get('request_id',''))
        return self._execute_batch(agent,account,self._request_tasks(request) if request else [task])

    def _has_unresolved(self,name):
        if not getattr(self,'store',None):return False
        snap=self.store.snapshot();tasks={t['id']:t for t in snap.get('tasks',[])}
        return any(r.get('account')==name and r.get('phase') in ('reserved','submitted','unknown','downloading')
                   and any(tasks.get(tid,{}).get('request_id')==r.get('request_id')
                           and tasks[tid].get('status')!='done' for tid in r.get('task_ids',[]))
                   for r in snap.get('requests',{}).values())

    def _worker(self,acct_cfg):
        name=acct_cfg['name'];status=self.accounts.status(name).get('status')
        if status in ('disabled','needs_login'):return
        if status=='exhausted' and not self._has_unresolved(name):return
        if not self._has_unresolved(name) and self.accounts.quota(name)['remaining']<1:return
        # No pending work: do not launch another account merely because it has quota.
        batch=self._claim_batch(name)
        if not batch:return
        session=BrowserSession(name,acct_cfg['profile'],self.cfg,start_url=get(self.cfg,'doubao.chat_url','https://www.doubao.com/chat/'))
        opened=False
        try:
            session.start();agent=DoubaoAgent(session,self.cfg,name);agent.open_chat()
            opened=True
            self.accounts.mark_available_after_login(name)
            while batch:
                self.accounts.mark_busy(name)
                try:keep_going=self._execute_batch(agent,name,batch)
                finally:self.accounts.release(name)
                if not keep_going:return
                batch=self._claim_batch(name)
        except NeedsLogin as exc:
            self.accounts.mark_needs_login(name,str(exc))
            self._release_startup_reservation(name,batch,str(exc))
        except Exception as exc:
            traceback.print_exc()
            if not opened:
                self._release_startup_reservation(name,batch,str(exc))
                self.accounts.mark_disabled(name,str(exc)[:300])
            self.accounts.release(name)
        finally:session.stop()

    def _release_startup_reservation(self,account,batch,error):
        rid=batch[0]['request_id'];ledger=self.accounts.request(account,rid)
        if ledger.get('submitted'):
            for task in batch:
                if task['status']!='done':self._finish(task['id'],status='awaiting_result',error=error[:500])
            return
        self.accounts.release_unsubmitted(account,rid)
        ids={t['id'] for t in batch}
        with self.store.txn() as data:
            data['requests'][rid]['phase']='cancelled'
            for task in data['tasks']:
                if task['id'] in ids:task.update(status='pending',account='',request_id='',error=error[:500])

    def stage_run(self):
        require_doubao_accounts(self.cfg)
        with self._run_lock():
            return self._stage_run_locked()

    def _stage_run_locked(self):
        self._attempted_requests=set()
        self._local_attempts_this_run={};self._local_blocked_tasks=set()
        self.accounts.reset_daily()
        self._recover_state()
        if not self.store.snapshot().get('tasks'):raise RuntimeError('没有任务，请先规划脚本')
        self._assemble_all()
        for acct in get(self.cfg,'accounts.doubao',[]) or []:
            if acct.get('enabled',True):self._worker(acct)
            if self._all_finished():break
        self._assemble_all()
        self.report()
        done,running,pending,failed,total=self._progress()
        assembly_failed=any(a.get('status')!='done' for a in self.store.snapshot().get('assemblies',{}).values())
        if pending or running or failed or assembly_failed:
            raise RuntimeError(f'流程未完成：完成{done}/{total}，待恢复{pending+running}，失败{failed}；未决请求只恢复原会话，不重新生成')

    # ================= 汇总 =================
    def report(self):
        snap = self.store.snapshot()
        done, running, pending, failed, total = self._progress()
        print("\n========== 流水线汇总 ==========")
        print(f"任务: 共 {total}，完成 {done}，执行中 {running}，待执行 {pending}，失败 {failed}")
        for t in snap.get("tasks", []):
            mark = {"done": "✓", "failed": "✗", "running": "▶", "pending": "·"}.get(t["status"], "?")
            extra = t.get("video") or t.get("error") or ""
            print(f"  {mark} {t['id']:<14} [{t['status']:<7}] {extra}")
        print("账号状态:")
        print(self.accounts.blocked_report() or "  暂无启用的豆包账号，请在工作台「账号库」添加账号并完成登录验证。")
        if pending or failed:
            print("\n提示: 待执行任务会在账号额度恢复（次日）或重新登录后，重跑 python run_pipeline.py run 自动续接。")

    def run_all(self, force: bool = False, from_saved: bool = False, limit: int = 0):
        require_doubao_accounts(self.cfg)
        with self._run_lock():
            return self._run_all_locked(force=force,from_saved=from_saved,limit=limit)

    def _run_all_locked(self, force=False, from_saved=False, limit=0):
        if from_saved:
            if not self.store.snapshot().get("sources"):
                raise RuntimeError("没有已保存素材，请先同步飞书或添加素材")
            print("[fetch] 使用工作台已保存素材，保留本地修改")
        else:
            self.stage_fetch(force=force)
        self.stage_scripts(force=force, limit=limit)
        self.stage_plan(force=force)
        self.stage_run()
