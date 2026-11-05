"""Native macOS first-run setup. No Docker, account import or system changes."""
import hashlib
from contextlib import contextmanager
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
MODULE_CHECK = 'import flask, requests, yaml; import playwright.sync_api'


def configure_data_environment(root=ROOT, environ=None, home=None):
    """Choose roots before importing the application's cached path constants."""
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    data_override = env.get('DOUBAO_DATA_DIR', '').strip()
    config_override = env.get('DOUBAO_CONFIG_PATH', '').strip()
    if data_override:
        data = Path(data_override).expanduser()
    elif not config_override and (root / 'config/config.yaml').is_file():
        data = root
    else:
        data = home / 'Library/Application Support/DoubaoAutomation'
    config = Path(config_override).expanduser() if config_override else data / 'config/config.yaml'
    if not data.is_absolute() or not config.is_absolute():
        raise ValueError('DOUBAO_DATA_DIR 和 DOUBAO_CONFIG_PATH 必须为绝对路径')
    env['DOUBAO_DATA_DIR'] = str(data)
    env['DOUBAO_CONFIG_PATH'] = str(config)
    env.pop('DOUBAO_CONTAINER_PREVIEW', None)
    return data, config


def startup_log(data):
    path = data / 'logs/macos_startup.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    return path


@contextmanager
def setup_lock(data, timeout=900):
    """Two Finder clicks must not install into the same venv concurrently."""
    import fcntl
    path = data / '.tmp/macos_bootstrap.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('另一个启动器仍在准备环境，等待超时；请查看启动日志。')
                if not announced:
                    print('另一个启动器正在准备环境，等待其完成后复用。', flush=True)
                    announced = True
                time.sleep(1)
        yield
    finally:
        os.close(fd)


def run_logged(command, log, timeout):
    """Keep installer output local and bound every external setup command."""
    with log.open('a', encoding='utf-8') as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                       check=True, timeout=timeout)


def check_ffmpeg():
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    if missing:
        raise RuntimeError('缺少 ' + '、'.join(missing) + '。请先安装完整 FFmpeg，例如 brew install ffmpeg；不会自动修改系统。')


def ensure_virtualenv(root, log):
    python = root / '.venv/bin/python'
    if python.is_file() and os.access(python, os.X_OK):
        return python
    if (root / '.venv').exists():
        raise RuntimeError('现有 .venv 缺少可执行 Python；已保留原目录，请备份后按文档重建。')
    print('首次启动：正在创建项目虚拟环境。', flush=True)
    run_logged([sys.executable, '-m', 'venv', str(root / '.venv')], log, 120)
    if not python.is_file():
        raise RuntimeError('虚拟环境创建后仍未找到 Python，请查看启动日志。')
    return python


def ensure_dependencies(root, python, log):
    requirements = root / 'requirements.txt'
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = root / '.venv/.doubao-requirements.sha256'
    try:
        current = marker.read_text(encoding='ascii').strip() == fingerprint
    except OSError:
        current = False
    modules_ok = subprocess.run([str(python), '-c', MODULE_CHECK],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30).returncode == 0
    if current and modules_ok:
        return
    print('正在检查并安装 requirements.txt 依赖；详细输出写入启动日志。', flush=True)
    run_logged([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                '--retries', '2', '--timeout', '30', '-r', str(requirements)], log, 300)
    run_logged([str(python), '-c', MODULE_CHECK], log, 30)
    marker.write_text(fingerprint + '\n', encoding='ascii')


def ensure_private_config(root, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise RuntimeError('配置路径已存在但不是文件：' + str(path))
        return False
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write((root / 'config/config.example.yaml').read_bytes())
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)  # Only this call's newly-created template.
        raise
    print('已创建无密钥配置模板：' + str(path) + '；请填写自己的设置并人工登录。', flush=True)
    return True


def ensure_browser(cfg, log):
    from doubao_automation.browser_runtime import inspect_browser_runtime
    runtime = inspect_browser_runtime(cfg)
    if not runtime['available'] and runtime['install_managed']:
        print('正在安装与当前 Playwright 匹配的 Chromium；保留现有账号资料。', flush=True)
        run_logged([sys.executable, '-m', 'playwright', 'install', 'chromium'], log, 300)
        runtime = inspect_browser_runtime(cfg)
    if not runtime['available']:
        raise RuntimeError(runtime['error'])


def verify_dashboard(launcher):
    # State identity and page contents are separate requirements.
    if not launcher.checked_state():
        raise RuntimeError('工作台 HTTP 归属检查失败')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(launcher.URL, timeout=5) as response:
        if response.status != 200 or '豆包 Studio' not in response.read().decode('utf-8'):
            raise RuntimeError('工作台页面检查失败，未打开浏览器')


def start_dashboard(check_requirements):
    if sys.version_info < (3, 10):
        raise RuntimeError('需要 Python 3.10 或更新版本')
    data, config = configure_data_environment()
    log = startup_log(data)
    try:
        ensure_private_config(ROOT, config)
        print('本机配置：' + str(config), flush=True)
        check_requirements()
        from doubao_automation.config import load_config
        ensure_browser(load_config(), log)
        import launch_dashboard
        launch_dashboard.main(open_page=False)
        verify_dashboard(launch_dashboard)
        run_logged(['/usr/bin/open', launch_dashboard.URL], log, 15)
        print('页面和项目归属检查通过，已请求系统打开工作台：' + launch_dashboard.URL, flush=True)
    except Exception:
        with log.open('a', encoding='utf-8') as output:
            traceback.print_exc(file=output)
        print('启动失败；日志：' + str(log), file=sys.stderr, flush=True)
        raise


def main():
    if sys.platform != 'darwin':
        raise RuntimeError('此启动入口仅用于 macOS')
    if sys.version_info < (3, 10):
        raise RuntimeError('需要 Python 3.10 或更新版本')
    data, config = configure_data_environment()
    log = startup_log(data)
    print('启动日志：' + str(log), flush=True)
    try:
        with setup_lock(data):
            check_ffmpeg()
            ensure_private_config(ROOT, config)
            python = ensure_virtualenv(ROOT, log)
            ensure_dependencies(ROOT, python, log)
            # The control command imports all runtime dependencies in the venv.
            completed = subprocess.run([str(python), '-u', str(ROOT / 'scripts/app_control.py'), 'start'],
                                       cwd=ROOT, timeout=420)
            return completed.returncode
    except Exception:
        with log.open('a', encoding='utf-8') as output:
            traceback.print_exc(file=output)
        print('启动失败；日志：' + str(log), file=sys.stderr, flush=True)
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('操作失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
