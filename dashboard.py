from flask import Flask, jsonify, request, render_template_string, send_from_directory, send_file, abort
from pathlib import Path
import datetime
import copy
import json
import ipaddress
import secrets
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import yaml
if sys.platform == 'darwin':
    sys.dont_write_bytecode = True
from doubao_automation.paths import DATA_ROOT, STATE_DIR, ROOT, PROFILES, PRODUCTS_DIR, SOURCES_DIR, VIDEOS_DIR, LOGS_DIR, resolve, use_workspace_temp
from doubao_automation.state import JsonStore
from doubao_automation.account_manager import AccountManager
from doubao_automation.orchestrator import Pipeline
from doubao_automation.config import DEFAULT_CONFIG_PATH, load_config
from doubao_automation.feishu_client import FeishuClient
from doubao_automation.windows_shell import open_video_folder
from doubao_automation import postprocess
from scripts.clear_history import preview_history

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024

# A process-local token is never persisted or logged. All mutating endpoints
# share this gate, including routes added later.
CSRF_TOKEN = secrets.token_urlsafe(32)
_ALLOWED_HOSTS = frozenset(('127.0.0.1', '127.0.0.1:8782', 'localhost', 'localhost:8782'))


@app.before_request
def enforce_local_request():
    try:
        local_peer = ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        local_peer = False
    if not local_peer:
        return jsonify(error='只允许本机访问', code='host_invalid'), 403
    host = request.headers.get('Host', '').lower()
    if request.scheme != 'http' or host not in _ALLOWED_HOSTS:
        return jsonify(error='不允许的本地 Host', code='host_invalid'), 403
    origin = request.headers.get('Origin')
    same_origin = 'http://' + host
    if origin is not None and origin != same_origin:
        return jsonify(error='不允许的请求来源', code='origin_invalid'), 403
    if request.method == 'OPTIONS':
        return jsonify(error='不允许跨域请求', code='origin_invalid'), 403
    if request.method not in ('GET', 'HEAD'):
        if origin != same_origin:
            return jsonify(error='写操作必须来自本机同源页面', code='origin_invalid'), 403
        token = request.headers.get('X-CSRF-Token', '')
        if not token.isascii() or not secrets.compare_digest(token, CSRF_TOKEN):
            return jsonify(error='安全令牌无效，请刷新页面', code='csrf_invalid'), 403
    elif request.path == '/api/csrf' and request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(error='不允许跨站读取安全令牌', code='origin_invalid'), 403
    if (os.environ.get('DOUBAO_CONTAINER_PREVIEW') == '1'
            and request.method not in ('GET', 'HEAD')
            and request.path in {'/api/run', '/api/accounts/add', '/api/accounts/open',
                                 '/api/open-video', '/api/videos/approve'}):
        return jsonify(error='容器预览版不支持启动浏览器、桌面操作或视频后处理',
                       code='container_unsupported'), 501


@app.after_request
def protect_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response

lock = threading.RLock()
proc = None
pending_accounts = {}

# HTML is inserted here by the migration script.
HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>豆包 Studio</title><style>*{box-sizing:border-box}body{margin:0;background:#f5f5f7;color:#1d1d1f;font:15px -apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui}.shell{max-width:1160px;margin:auto;padding:28px 22px}header{display:flex;align-items:center;gap:24px;margin-bottom:24px}.logo{font-size:24px;font-weight:750}.nav{display:flex;gap:6px;background:#e9e9ed;padding:5px;border-radius:14px}.nav button{background:transparent;color:#515154;padding:9px 16px;border:0;cursor:pointer}.nav button.active{background:white;box-shadow:0 1px 5px #0002}.status{margin-left:auto;background:#e8f8ee;color:#168044;border-radius:99px;padding:8px 13px;font-size:13px}.card{background:#fff;border:1px solid #e8e8ed;border-radius:20px;padding:22px;margin:14px 0;box-shadow:0 3px 12px #00000008}h1{font-size:28px;margin:0 0 6px}h2{font-size:18px;margin:0 0 14px}.muted{color:#777;font-size:13px}.btn{border:0;border-radius:12px;padding:11px 17px;font-weight:650;background:#007aff;color:white;cursor:pointer}.btn.gray{background:#e9e9ed;color:#222}.btn.red{background:#fff0f0;color:#c22}.actions{display:flex;gap:9px;flex-wrap:wrap}.row{padding:16px;border:1px solid #e8e8ed;border-radius:16px;margin:12px 0}.grid{display:grid;grid-template-columns:220px 1fr;gap:12px}input,textarea{font:inherit;padding:11px;border:1px solid #d2d2d7;border-radius:11px;width:100%;background:white}textarea{min-height:110px}.thumbs{display:flex;gap:8px;overflow:auto;margin:8px 0}.thumbs img{width:90px;height:75px;object-fit:cover;border-radius:10px}.log{white-space:pre-wrap;background:#17171a;color:#eee;border-radius:14px;padding:14px;max-height:190px;overflow:auto}@media(max-width:700px){.grid{grid-template-columns:1fr}header{flex-wrap:wrap}}</style></head><body><main class="shell"><header><div class="logo">豆包 Studio</div><nav class="nav"><button id="navstudio" class="active" onclick="tab('studio')">工作台</button><button id="navaccounts" onclick="tab('accounts')">账号库</button><button id="navvideos" onclick="tab('videos')">已下载视频</button></nav><span class="status" id="status">● 连接中</span></header><section id="studio"><h1>视频自动化工作台</h1><p class="muted">飞书同步、Gemini脚本与豆包视频生成</p><article class="card"><h2>运行控制</h2><div class="actions"><button class="btn" onclick="syncFeishu()">同步飞书</button><button class="btn gray" onclick="saveRows()">保存修改</button><button class="btn" onclick="run()">开启运行</button><button class="btn red" onclick="stop()">停止</button></div></article><article class="card"><h2>手动添加素材</h2><div class="grid"><label>keyword<input id="newkw"></label><label>提示词<textarea id="newprompt"></textarea></label><label>视频<input id="newvid" type="file" accept="video/*" multiple></label><label>产品图<input id="newimg" type="file" accept="image/*" multiple></label></div><button class="btn" onclick="upload()">添加素材</button></article><article class="card"><h2>同步记录</h2><div id="rows"></div></article></section><section id="accounts" style="display:none"><h1>豆包账号库</h1><article class="card"><div id="accountRows"></div><button class="btn" onclick="addAccount()">＋ 添加豆包账号</button></article></section><section id="videos" style="display:none"><h1>已下载视频</h1><div id="videoRows"></div></section><article class="card"><h2>操作消息</h2><pre class="log" id="log">就绪</pre></article><article class="card"><h2>运行日志</h2><pre class="log" id="runlog">暂无运行日志</pre></article></main><script src="/dashboard.js"></script></body></html>'''

def store():
    return JsonStore(STATE_DIR / 'pipeline_state.json')


def readonly_snapshot(path, initial=None):
    # JsonStore uses atomic replace; readers need no lock file or transaction.
    try:
        with Path(path).open(encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return copy.deepcopy(initial or {})
    if not isinstance(value, dict):
        raise ValueError('状态文件格式错误')
    return value


class _ReadOnlyStore:
    def __init__(self, data):
        self.data = data

    def snapshot(self):
        return copy.deepcopy(self.data)


def readonly_account_manager(names):
    data = readonly_snapshot(STATE_DIR / 'accounts.json')
    accounts = data.setdefault('accounts', {})
    for name in names:
        accounts.setdefault(name, dict(status='needs_login', note='尚未验证登录', date=''))
    manager = AccountManager.__new__(AccountManager)
    manager.store, manager.names, manager.daily_limit = _ReadOnlyStore(data), list(names), 4
    manager.clock = lambda: datetime.datetime.now(datetime.timezone.utc)
    return manager


def readonly_postprocess_snapshots():
    data = readonly_snapshot(postprocess.STORE_PATH, {'jobs': []})
    result = {}
    for job in data.get('jobs', []):
        public = dict(job)
        log = Path(job['log'])
        if log.is_file() and postprocess.JOBS_DIR.resolve() in log.resolve().parents:
            with log.open('rb') as handle:
                handle.seek(max(0, log.stat().st_size - 1800))
                public['log_tail'] = handle.read().decode('utf-8', errors='replace')
        result[job['source']] = public
    return result


def readonly_postprocess_busy():
    data = readonly_snapshot(postprocess.STORE_PATH, {'jobs': []})
    return any(job.get('status') in ('queued', 'running') for job in data.get('jobs', []))

def running():
    if proc and proc.poll() is None:
        return True
    path=(STATE_DIR/'pipeline_state.json').with_suffix('.runner.lock')
    if not path.is_file():
        return False
    with path.open('r+b') as handle:
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(handle,fcntl.LOCK_UN)
        except OSError:
            return True
    return False

def require_idle():
    if running():
        raise ValueError('流水线正在运行，请先停止再修改素材或账号')

@app.errorhandler(ValueError)
def bad_value(exc):
    return jsonify(error=str(exc)), 400

@app.get('/')
def home():
    html = HTML.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="csrf-token" content="{{ csrf_token }}">')
    html = html.replace('<button class="btn red" onclick="stop()">停止</button>',
                        '<button class="btn red" onclick="stop()">停止</button>'
                        '<button class="btn gray" onclick="previewClearHistory()">清除历史记录及对应文件（仅预览）</button>')
    return render_template_string(html, csrf_token=CSRF_TOKEN)


@app.get('/api/csrf')
def csrf_token():
    return jsonify(token=CSRF_TOKEN)

@app.get('/dashboard.js')
def dashboard_js():
    return send_from_directory(ROOT, 'dashboard_ui.js', max_age=0)

@app.get('/api/history/preview')
def history_preview():
    return jsonify(preview_history())

@app.get('/api/state')
def state():
    d = readonly_snapshot(STATE_DIR / 'pipeline_state.json')
    pm = {p.get('keyword',''): p for p in d.get('products', [])}
    log = ''
    log_path = d.get('run_log')
    if log_path:
        p = Path(log_path).resolve()
        if LOGS_DIR.resolve() in p.parents and p.is_file():
            with p.open('rb') as f:
                f.seek(max(0, p.stat().st_size - 20000))
                log = f.read().decode('utf-8', errors='replace')
    return jsonify(server_pid=os.getpid(), project_root=str(ROOT), data_root=str(DATA_ROOT),
                   service_pid=int(os.environ.get('DOUBAO_DASHBOARD_SERVICE_PID','0')) or None,
                   login_windows_open=any(job['thread'].is_alive() for job in pending_accounts.values()),
                   postprocessing=readonly_postprocess_busy(),
                   running=running(),
                    can_resume=has_unfinished_plan(d),resume_script_count=planned_script_count(d),resume_run_id=d.get('run_id',''),
                   returncode=d.get('run_returncode'), log=log, tasks=d.get('tasks', []),
                   rows=[{'record_id':s.get('record_id',''), 'keyword':s.get('keyword',''),
                          'prompt':s.get('prompt_text',''), 'videos':s.get('videos',[]),
                          'images':pm.get(s.get('keyword',''),{}).get('images',[])} for s in d.get('sources',[])])

@app.post('/api/sync')
def sync():
    with lock:
        require_idle()
        try:
            fc=FeishuClient(load_config()); ps=fc.fetch_products(); ss=fc.fetch_sources(); fixed=fc.load_fixed_prompt()
            with store().txn() as d:
                hidden=set(d.get('hidden_source_ids',[]))
                local=[s for s in d.get('sources',[]) if s.get('record_id','').startswith('local_')]
                local_products=[p for p in d.get('products',[]) if p.get('record_id','').startswith('local_')]
                visible=[s for s in ss if s.get('record_id') not in hidden]
                d.update(products=ps+local_products, sources=visible+local, fixed_prompt=fixed, scripts=[], tasks=[])
            return jsonify(ok=True, products=len(ps), sources=len(visible), local_sources=len(local))
        except Exception as e:
            return jsonify(error=str(e)), 500

@app.post('/api/edit')
def edit():
    body=request.get_json(force=True); changes={x['record_id']:x for x in body.get('rows',[])}
    with lock:
        require_idle()
        updated=0
        with store().txn() as d:
            ids={s['record_id'] for s in d.get('sources',[])}
            if set(changes)-ids:
                raise ValueError('素材记录已变化，请刷新后重试')
            for s in d.get('sources',[]):
                if s.get('record_id') not in changes:
                    continue
                change=changes[s['record_id']]
                keyword=change['keyword'].strip(); prompt=change['prompt_text']
                if keyword != s.get('keyword','') or prompt != s.get('prompt_text',''):
                    s.update(keyword=keyword, prompt_text=prompt); updated+=1
            if updated:
                d.update(scripts=[], tasks=[])
    return jsonify(ok=True, updated=updated)

@app.post('/api/rows/delete')
def row_delete():
    rid=request.get_json(force=True).get('record_id')
    with lock:
        require_idle()
        with store().txn() as d:
            if not any(s.get('record_id')==rid for s in d.get('sources',[])):
                abort(404)
            d['sources']=[s for s in d.get('sources',[]) if s.get('record_id')!=rid]
            hidden=d.setdefault('hidden_source_ids',[])
            if rid not in hidden:
                hidden.append(rid)
            d.update(scripts=[],tasks=[])
    return jsonify(ok=True, deleted=rid)

def watch_run(child, run_id, attempt_id=None):
    code=child.wait()
    with lock:
        with store().txn() as d:
            if d.get('run_id')==run_id and d.get('run_attempt_id')==attempt_id:
                d['run_returncode']=code


def has_unfinished_plan(snap):
    tasks=snap.get('tasks',[])
    assemblies=snap.get('assemblies',{})
    if any(task.get('status')!='done' for task in tasks):
        return True
    if any(item.get('status')!='done' for item in assemblies.values()):
        return True
    groups={}
    for task in tasks:
        if task.get('script_id'):
            groups.setdefault(task['script_id'],[]).append(task)
    for script_id,group in groups.items():
        if len(group)<2:
            continue
        assembly=assemblies.get(script_id,{})
        output=Path(assembly.get('output') or VIDEOS_DIR/f'{script_id}_final.mp4')
        if assembly.get('status')!='done' or not output.is_file():
            return True
    return False


def planned_script_count(snap):
    return (len(snap.get('scripts',[]))
            or len({task['script_id'] for task in snap.get('tasks',[]) if task.get('script_id')})
            or len(snap.get('assemblies',{})) or 0)


@app.post('/api/run')
def run():
    global proc
    with lock:
        if running():
            return jsonify(error='流水线已在运行'),409
        if any(job['thread'].is_alive() for job in pending_accounts.values()):
            raise ValueError('账号登录窗口仍在使用，请完成登录或关闭登录窗口后再运行')
        runner=Pipeline.__new__(Pipeline); runner.store=store()
        try:
            with runner._run_lock():
                snap=runner.store.snapshot(); resumed=has_unfinished_plan(snap)
                run_id=snap.get('run_id') if resumed else None
                run_id=run_id or datetime.datetime.now().strftime('r%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:4]
                attempt_id=uuid.uuid4().hex
                LOGS_DIR.mkdir(parents=True,exist_ok=True)
                log_path=Path(snap['run_log']) if resumed and snap.get('run_log') else LOGS_DIR/(run_id+'.log')
                log_path.parent.mkdir(parents=True,exist_ok=True)
                if resumed:
                    script_count=planned_script_count(snap)
                    command=[sys.executable,'-u','run_pipeline.py','run']
                    with runner.store.txn() as d:
                        d.update(run_id=run_id,run_attempt_id=attempt_id,run_log=str(log_path),run_returncode=None)
                else:
                    sources=snap.get('sources',[])
                    if not sources:
                        raise ValueError('请先同步飞书或添加素材')
                    keywords={p.get('keyword') for p in snap.get('products',[]) if p.get('images')}
                    for source in sources:
                        if source.get('keyword') not in keywords:
                            raise ValueError(f"keyword={source.get('keyword')!r} 无对应产品图，请修正后运行")
                    script_count=None
                    command=[sys.executable,'-u','run_pipeline.py','all','--from-saved','--force']
                    with runner.store.txn() as d:
                        d.update(scripts=[],tasks=[],requests={},assemblies={},
                                 run_id=run_id,run_attempt_id=attempt_id,run_log=str(log_path),run_returncode=None)
        except RuntimeError as exc:
            return jsonify(error=str(exc)),409
        # Release the journal's runner lock before the child attempts to acquire it.
        # A racing independent CLI is still serialized by the child's own lock.
        if running():
            return jsonify(error='已有独立流水线接手任务，请查看运行日志'),409
        with log_path.open('a' if resumed else 'w',encoding='utf-8') as log:
            if resumed:
                log.write('\n[dashboard] 继续上次计划，保留已完成视频、原请求和额度记录。\n');log.flush()
            proc=subprocess.Popen(command,
                                  cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
        threading.Thread(target=watch_run,args=(proc,run_id,attempt_id),daemon=True).start()
    message=(f'继续上次计划（{script_count}个脚本），已完成视频不重复生成；脚本数量沿用原计划'
             if resumed else '已启动新任务，脚本数量由提示词决定')
    return jsonify(ok=True,pid=proc.pid,script_count=script_count,log=str(log_path),resumed=resumed,message=message)

@app.post('/api/stop')
def stop():
    with lock:
        if not running():
            return jsonify(ok=True,stopped=False)
        if not proc or proc.poll() is not None:
            return jsonify(error='任务由独立流水线进程运行，请从启动该任务的窗口停止，避免结束其他程序'),409
        if os.name=='nt':
            result=subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
            if result.returncode and running():
                return jsonify(error='停止进程失败'),500
        else:
            proc.terminate()
    return jsonify(ok=True,stopped=True)

def _savecfg(cfg):
    p=DEFAULT_CONFIG_PATH; p.parent.mkdir(parents=True,exist_ok=True); tmp=p.with_suffix('.yaml.tmp')
    tmp.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False),encoding='utf-8'); os.replace(tmp,p)

def account_manager(names=()):
    state=JsonStore(STATE_DIR/'accounts.json')
    with state.txn() as data:
        accounts=data.setdefault('accounts',{})
        for name in names:
            accounts.setdefault(name,dict(status='needs_login',note='尚未验证登录',date=''))
    return AccountManager(state,list(names))


def account_status(name,status,note=''):
    manager=account_manager([name])
    if status=='available':
        manager.mark_available_after_login(name)
        # A verified login must not overwrite exhaustion or an explicit disable.
        with manager.store.txn() as data:
            acc=data.setdefault('accounts',{}).setdefault(name,{})
            if acc.get('status')=='available':
                acc['note']=note
    else:
        manager._set(name,status,note)


def account_quota(manager,name,state=None):
    return dict((state or {}).get('quota') or manager.quota(name),timezone='Asia/Shanghai')

@app.get('/api/accounts')
def account_list():
    with lock:
        items=load_config().get('accounts',{}).get('doubao',[])
        manager=readonly_account_manager([a['name'] for a in items])
        out=[]
        for a in items:
            name=a['name']; state=manager.status(name); job=pending_accounts.get(name,{})
            opened=bool(job.get('thread') and job['thread'].is_alive())
            out.append(dict(name=name,profile=a['profile'],enabled=a.get('enabled',True),saved=True,
                            status=state.get('status','needs_login'),note=state.get('note','尚未验证登录'),
                            quota=account_quota(manager,name,state),login_window_open=opened,
                            login_status=job.get('status','opening') if opened else '',
                            login_note=job.get('note','等待登录验证') if opened else ''))
    return jsonify(accounts=out)

def login_worker(name,profile,job):
    from doubao_automation.browser_pool import BrowserSession
    sess=None
    try:
        use_workspace_temp(); cfg=load_config()
        sess=BrowserSession(name,profile,cfg,start_url='https://www.doubao.com/chat/').start()
        job.update(status='needs_login',note='请在独立浏览器内手动登录；检测成功后自动保存并关闭窗口')
        job['ready'].set()
        deadline=time.monotonic()+7200
        expected=set(cfg.get('login',{}).get('doubao_logged_in_cookies',[]))-{'uid_tt'}
        if not expected:
            job.update(status='needs_login',note='登录检测规则未配置；请手动登录，并在本机配置检测规则后重新检查')
        while time.monotonic()<deadline and not job['stop'].is_set():
            if sess.page.is_closed():
                break
            expected=set(cfg.get('login',{}).get('doubao_logged_in_cookies',[]))-{'uid_tt'}
            valid=sess.has_any_cookie('https://www.doubao.com',expected)
            valid=valid and sess.page.locator('div[contenteditable="true"]').count()>0
            if valid:
                with lock:
                    current=load_config()
                    for a in current['accounts']['doubao']:
                        if a['name']==name:
                            a['enabled']=True
                    _savecfg(current)
                account_status(name,'available','登录已验证，浏览器配置已保存')
                job.update(verified=True,status='verified',note='登录已验证，浏览器配置已保存')
                break
            # 用 Playwright 事件循环等待，保持本线程拥有浏览器生命周期。
            sess.page.wait_for_timeout(1500)
        if not job.get('verified'):
            account_status(name,'needs_login','登录尚未完成，可重新打开登录窗口')
    except Exception as e:
        account_status(name,'error',str(e)[:600]); job.update(error=str(e),status='error',note=str(e)[:600])
    finally:
        if sess:
            sess.stop()
        job['ready'].set(); job['finished'].set()

def start_login(name,profile):
    old=pending_accounts.get(name)
    if old and old['thread'].is_alive():
        return old
    job={'stop':threading.Event(),'ready':threading.Event(),'finished':threading.Event(),
         'status':'opening','note':'正在打开独立浏览器'}
    job['thread']=threading.Thread(target=login_worker,args=(name,profile,job),daemon=True)
    pending_accounts[name]=job; job['thread'].start()
    return job

@app.post('/api/accounts/add')
def account_add():
    with lock:
        require_idle(); cfg=load_config(); items=cfg.setdefault('accounts',{}).setdefault('doubao',[]); i=1
        while any(a['name']==f'doubao{i}' for a in items) or (PROFILES/f'doubao{i}').exists():
            i+=1
        name=f'doubao{i}'; profile=f'profiles/{name}'
        items.append({'name':name,'profile':profile,'enabled':False}); _savecfg(cfg)
        start_login(name,profile)
    return jsonify(ok=True,name=name,profile=profile,saved=True,status='opening')

@app.post('/api/accounts/open')
def account_open():
    name=request.get_json(force=True).get('name')
    with lock:
        require_idle(); a=next((a for a in load_config()['accounts']['doubao'] if a['name']==name),None)
        if not a: abort(404)
        start_login(name,a['profile'])
    return jsonify(ok=True,name=name,status='opening')

@app.post('/api/accounts/confirm')
def confirm():
    name=request.get_json(force=True).get('name')
    manager=account_manager()
    if not manager.store.snapshot().get('accounts',{}).get(name):
        return jsonify(error='尚未检测到登录，请打开登录窗口并手动登录'),400
    status=manager.status(name); quota=account_quota(manager,name,status)
    if status.get('status') in ('available','busy','exhausted'):
        label={'available':'已登录','busy':'账号正在运行','exhausted':'今日额度耗尽'}[status['status']]
        message=label+'，今日剩余 '+str(quota['remaining'])+'/'+str(quota['limit'])
        if not quota['remaining']: message+='，后续任务自动跳过'
        return jsonify(ok=True,saved=True,name=name,status=status['status'],quota=quota,message=message)
    return jsonify(error=status.get('note') or '尚未检测到登录，请打开登录窗口并手动登录'),400

@app.post('/api/accounts/close')
def account_close():
    name=request.get_json(force=True).get('name')
    job=pending_accounts.get(name)
    if job and job['thread'].is_alive():
        job['stop'].set()
        if not job['finished'].wait(15):
            return jsonify(error='登录窗口仍在关闭，请稍后重试'),409
    return jsonify(ok=True,name=name)

@app.post('/api/accounts/delete')
def account_delete():
    name=request.get_json(force=True).get('name')
    with lock:
        require_idle()
        a=next((x for x in load_config()['accounts']['doubao'] if x['name']==name),None)
        if not a: abort(404)
        p=resolve(a['profile']).resolve()
        if PROFILES.resolve() not in p.parents:
            raise ValueError('invalid profile path')
        job=pending_accounts.get(name)
        if job and job['thread'].is_alive():
            job['stop'].set()
            return jsonify(error='正在关闭登录窗口，请稍后重试删除'),409
        if p.exists(): shutil.rmtree(p)
        cfg=load_config();cfg['accounts']['doubao']=[x for x in cfg['accounts']['doubao'] if x['name']!=name]; _savecfg(cfg)
        with JsonStore(STATE_DIR/'accounts.json').txn() as d:
            d.get('accounts',{}).pop(name,None)
        pending_accounts.pop(name,None)
    return jsonify(ok=True,disk_removed=not p.exists())

@app.post('/api/upload')
def upload():
    with lock:
        require_idle()
        kw=request.form.get('keyword','').strip(); rid='local_'+uuid.uuid4().hex[:8]
        if not kw: raise ValueError('keyword required')
        vd=SOURCES_DIR/rid; im=PRODUCTS_DIR/rid; vd.mkdir(parents=True,exist_ok=True); im.mkdir(parents=True,exist_ok=True)
        vs=[]; ims=[]
        for field,base,out in [('videos',vd,vs),('images',im,ims)]:
            for f in request.files.getlist(field):
                p=base/(uuid.uuid4().hex[:6]+'_'+Path(f.filename).name);f.save(p);out.append(str(p))
        with store().txn() as d:
            d.setdefault('sources',[]).append({'record_id':rid,'keyword':kw,'prompt_text':request.form.get('prompt',''),'videos':vs})
            d.setdefault('products',[]).append({'record_id':rid,'name':kw,'keyword':kw,'images':ims})
    return jsonify(ok=True,videos=len(vs),images=len(ims))

@app.get('/api/videos')
def videos():
    out=[]
    processed=readonly_postprocess_snapshots()
    for p in VIDEOS_DIR.glob('*.mp4'):
        try:s=p.stat()
        except FileNotFoundError:continue  # A concurrent delete may remove an enumerated file.
        out.append({'name':p.name,'path':str(p.resolve()),'size':f'{s.st_size/1048576:.1f}MB','modified':datetime.datetime.fromtimestamp(s.st_mtime).isoformat(timespec='seconds'),'postprocess':processed.get(str(p.resolve()))})
    return jsonify(videos=sorted(out,key=lambda x:x['modified'],reverse=True))

@app.post('/api/videos/approve')
def approve_video():
    with lock:
        body=request.get_json(force=True)
        p=video_path(body.get('path',''))
        return jsonify(ok=True,job=postprocess.submit(p))

@app.post('/api/videos/delete')
def delete_video():
    with lock:
        if running():
            return jsonify(error='视频生成流程仍在使用下载文件，请等待运行完成后再删除'),409
        p=video_path(request.get_json(force=True).get('path',''))
        try:
            deleted=postprocess.delete_source(p)
        except postprocess.VideoInUse as exc:
            return jsonify(error=str(exc)),409
        except PermissionError:
            return jsonify(error='视频文件正在被其他程序占用，请关闭播放或编辑窗口后重试'),409
        except OSError as exc:
            return jsonify(error='磁盘文件删除失败：'+str(exc)),500
    return jsonify(ok=True,deleted=deleted)

@app.get('/api/postprocess')
def postprocess_jobs():
    return jsonify(jobs=list(readonly_postprocess_snapshots().values()))

def video_path(value):
    p=Path(value).resolve()
    if VIDEOS_DIR.resolve() not in p.parents or not p.is_file() or p.suffix.lower()!='.mp4':
        abort(404)
    return p

@app.post('/api/open-video')
def openvideo():
    p=video_path(request.get_json(force=True).get('path',''))
    try:
        open_video_folder(p)
    except OSError as exc:
        app.logger.exception('打开视频文件夹失败: %s', p)
        return jsonify(error=f'无法打开文件夹：{exc}',folder=str(p.parent)),500
    message='已请求 Finder 显示视频，请确认窗口' if sys.platform=='darwin' else '已打开视频所在文件夹'
    return jsonify(ok=True,message=message,folder=str(p.parent))

@app.get('/media/<path:name>')
def media(name):
    if Path(name).name!=name: abort(404)
    for base in (PRODUCTS_DIR,SOURCES_DIR):
        for p in base.rglob(name):
            if p.is_file(): return send_from_directory(p.parent,p.name)
    abort(404)

@app.get('/media-video')
def media_video():
    return send_file(video_path(request.args.get('path','')),mimetype='video/mp4',conditional=True)

if __name__=='__main__':
    use_workspace_temp()
    # Resume already queued postprocessing only during service startup, never from a GET.
    if os.environ.get('DOUBAO_CONTAINER_PREVIEW') != '1':
        postprocess.busy()
    # Windows 上 Werkzeug 的地址重用会允许多个仪表盘同时监听相同端口。
    from werkzeug.serving import BaseWSGIServer
    BaseWSGIServer.allow_reuse_address=False
    app.run('127.0.0.1',8782,debug=False,threaded=True)
