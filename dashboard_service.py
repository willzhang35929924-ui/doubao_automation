"""仪表盘管理进程：单实例、监测代码更新并安全报告待重载。"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
if sys.platform == 'darwin':
    sys.dont_write_bytecode = True
from doubao_automation.paths import ROOT, DATA_ROOT, LOGS_DIR, TEMP_DIR

STATUS = TEMP_DIR / 'dashboard_service.json'
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_FAST_FAILURES = 3
STABLE_WORKER_SECONDS = 30


def local_port_in_use():
    """Do not start a competing worker when a local listener already exists."""
    try:
        with socket.create_connection(('127.0.0.1', 8782), timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise RuntimeError('无法确认本地端口状态，已取消启动') from exc


def source_files():
    return [ROOT/'dashboard.py', ROOT/'run_pipeline.py', *sorted((ROOT/'doubao_automation').glob('*.py'))]


def revision():
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in source_files())


def validate_sources():
    for path in source_files():
        compile(path.read_bytes(), str(path), 'exec')


def worker_state():
    try:
        with LOCAL_HTTP.open('http://127.0.0.1:8782/api/state',timeout=2) as r:
            state=json.load(r)
            if (not state.get('project_root') or not state.get('data_root')
                    or Path(state['project_root']).resolve()!=ROOT.resolve()
                    or Path(state['data_root']).resolve()!=DATA_ROOT.resolve()):
                return None
            return state
    except Exception:
        return None


def publish(worker, **fields):
    data={'service_pid':os.getpid(), 'worker_pid':worker.pid if worker else None,
          'updated':time.time(), **fields}
    tmp=STATUS.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    os.replace(tmp,STATUS)


def start_worker():
    env=os.environ.copy()
    env['DOUBAO_DASHBOARD_SERVICE_PID']=str(os.getpid())
    env['PYTHONIOENCODING']='utf-8'
    env['PYTHONDONTWRITEBYTECODE']='1'
    with (LOGS_DIR/'dashboard.log').open('a',encoding='utf-8') as log:
        return subprocess.Popen([sys.executable,'-u',str(ROOT/'dashboard.py')],cwd=ROOT,
                                env=env,stdout=log,stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def main():
    TEMP_DIR.mkdir(parents=True,exist_ok=True)
    LOGS_DIR.mkdir(parents=True,exist_ok=True)
    with (TEMP_DIR/'dashboard_service.lock').open('a+b') as singleton:
        singleton.seek(0,os.SEEK_END)
        if not singleton.tell():singleton.write(b'0');singleton.flush()
        singleton.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(singleton.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(singleton.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            print('仪表盘管理进程已经运行，复用现有服务。',flush=True)
            return
        worker=None
        try:
            validate_sources()
            current=revision()
            if local_port_in_use():
                print('本机 8782 端口已有服务；为避免冲突，管理进程不启动或关闭任何工作进程。',flush=True)
                return
            worker=start_worker()
            worker_started_at=time.monotonic()
            fast_failures=0
            pending_revision=None
            publish(worker,reload_pending=False,error=None)
            while True:
                time.sleep(1)
                if worker.poll() is not None:
                    if time.monotonic()-worker_started_at < STABLE_WORKER_SECONDS:
                        fast_failures+=1
                    else:
                        fast_failures=0
                    if fast_failures >= MAX_FAST_FAILURES:
                        message='工作进程连续快速退出，已停止重启；请检查端口和仪表盘日志'
                        publish(worker,reload_pending=False,error=message)
                        print(message,flush=True)
                        break
                    publish(worker,reload_pending=True,error=f'工作进程退出：{worker.returncode}，正在恢复')
                    time.sleep(1)
                    try:
                        validate_sources()
                        if local_port_in_use():
                            message='本机 8782 端口已被占用，已停止重启；不会关闭该服务'
                            publish(worker,reload_pending=False,error=message)
                            print(message,flush=True)
                            break
                        worker=start_worker();worker_started_at=time.monotonic();current=revision();pending_revision=None
                        publish(worker,reload_pending=False,error=None)
                    except (SyntaxError,OSError,RuntimeError) as exc:
                        publish(worker,reload_pending=False,error=str(exc))
                        print('无法安全确认端口或恢复工作进程，已停止重启：'+str(exc),flush=True)
                        break
                    continue
                try:latest=revision()
                except OSError:continue
                if latest==current:
                    if pending_revision is not None:
                        pending_revision=None
                        publish(worker,reload_pending=False,error=None)
                    continue
                if latest==pending_revision:continue
                pending_revision=latest
                try:
                    validate_sources()
                except (SyntaxError,OSError) as exc:
                    publish(worker,reload_pending=True,error=str(exc))
                    continue
                message='检测到代码更新；缺少安全关停协议，未自动重载或终止工作进程'
                publish(worker,reload_pending=True,error=message)
                print(message,flush=True)
        finally:
            if worker and worker.poll() is None:
                print('管理进程退出；工作进程可能仍在运行，未强制终止。',flush=True)
            singleton.seek(0)
            if os.name == 'nt':
                msvcrt.locking(singleton.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(singleton.fileno(),fcntl.LOCK_UN)


if __name__=='__main__':main()
