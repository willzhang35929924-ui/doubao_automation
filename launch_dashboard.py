"""启动或复用本地仪表盘，避免重复服务和无限等待。"""
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
if sys.platform == 'darwin':
    sys.dont_write_bytecode = True
from doubao_automation.paths import ROOT, DATA_ROOT, LOGS_DIR
URL = 'http://127.0.0.1:8782/'
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_port_in_use():
    """Check the local listener without sending signals to its owner."""
    try:
        with socket.create_connection(('127.0.0.1', 8782), timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise RuntimeError('无法确认本地端口状态，已取消启动') from exc


def record(message):
    logs = LOGS_DIR
    logs.mkdir(parents=True, exist_ok=True)
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message
    with (logs / 'dashboard_launcher.log').open('a', encoding='utf-8') as log:
        log.write(line + '\n')
    if sys.stdout is not None:
        print(message, flush=True)


def open_browser():
    try:
        if webbrowser.open(URL):
            return True
        record('默认浏览器未确认打开，请稍后手动打开地址。')
    except Exception as exc:
        record(f'默认浏览器启动异常：{exc}')
    if os.name == 'nt':
        try:
            os.startfile(URL)
            return True
        except OSError as exc:
            record(f'Windows 网址关联启动失败：{exc}')
    return False


def belongs_to_project(state):
    if not state or not state.get('server_pid') or not state.get('project_root'):
        return False
    try:
        if Path(state['project_root']).resolve() != ROOT.resolve():
            return False
        data_root = state.get('data_root')
        if not data_root:
            # Older Windows builds used the source tree as their only data root.
            return os.name == 'nt' and DATA_ROOT.resolve() == ROOT.resolve()
        return Path(data_root).resolve() == DATA_ROOT.resolve()
    except (OSError, TypeError, ValueError):
        return False


def checked_state():
    state = server_state()
    if state and not belongs_to_project(state):
        raise RuntimeError('端口已被其他项目或其他数据目录的仪表盘占用；请使用正确的服务，不会自动关闭它')
    return state


def healthy():
    return bool(checked_state())


def server_state():
    try:
        with LOCAL_HTTP.open(URL + 'api/state', timeout=2) as r:
            data = json.load(r)
            return data if data.get('server_pid') else None
    except Exception:
        return None


def migrate_legacy_server(state):
    if not belongs_to_project(state) or state.get('service_pid'):
        return
    # An HTTP response and PID do not prove process ownership after PID reuse.
    # Reuse the verified project/data-root service and never forcibly kill it.
    record('检测到本项目旧后台，继续复用；不会自动强制终止进程。')


def main():
    logs = LOGS_DIR
    logs.mkdir(parents=True, exist_ok=True)
    migrate_legacy_server(checked_state())
    if not healthy():
        if local_port_in_use():
            raise RuntimeError('本机 8782 端口已被无法确认身份的服务占用；不会关闭或替换它')
        record('正在启动后台服务...')
        python = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        with (logs / 'dashboard_service.log').open('a', encoding='utf-8') as log:
            child = subprocess.Popen([str(python), '-u', str(ROOT / 'dashboard_service.py')], cwd=ROOT,
                                     env=env, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if healthy():
                break
            if child.poll() is not None:
                # 另一个启动器可能刚刚抢先启动了服务。
                if not healthy():
                    raise RuntimeError('仪表盘启动失败，请查看 logs/dashboard.log')
                break
            time.sleep(0.5)
        else:
            raise TimeoutError('等待仪表盘超过 30 秒，请查看 logs/dashboard.log')
    else:
        record('检测到已运行的仪表盘，复用后台服务。')
    record(f'服务已就绪：{URL}')
    if open_browser():
        record('已向系统发送打开浏览器请求。若没有弹出，请手动打开以上地址。')
    else:
        record('浏览器未能自动打开，请复制以上地址到浏览器。后台服务仍正常运行。')
    (logs / 'dashboard_start_error.log').unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / 'dashboard_start_error.log').write_text(traceback.format_exc(), encoding='utf-8')
        record(f'启动失败：{exc}')
        raise
