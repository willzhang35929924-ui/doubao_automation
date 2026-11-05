"""Native launcher shared by the Windows and macOS entry scripts.

Shutdown intentionally fails closed until a cooperative, atomic shutdown
protocol can prevent new work while the owned worker exits.
"""
import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def check_start_requirements():
    missing = []
    for command in ('ffmpeg', 'ffprobe'):
        if not shutil.which(command):
            missing.append(command)
    for module in ('flask', 'requests', 'yaml', 'playwright.sync_api'):
        try:
            found = importlib.util.find_spec(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            found = None
        if found is None:
            missing.append(module)
    if missing:
        raise RuntimeError('缺少依赖：' + '、'.join(missing) + '。请安装 requirements.txt 中的依赖和 PATH 中的 FFmpeg/FFprobe。')
    from doubao_automation.config import DEFAULT_CONFIG_PATH
    if not DEFAULT_CONFIG_PATH.is_file():
        raise RuntimeError('缺少本机配置，请参考 README 和平台文档，从配置示例创建配置文件。')


def query_state():
    # The existing launcher checks both project and data roots before reuse.
    import launch_dashboard
    return launch_dashboard.checked_state()


def report_state(state):
    if state is None:
        print('未检测到可验证的工作台服务；不能据此确认管理进程已经退出。')
        return
    for name, label in (('running', '流水线'), ('login_windows_open', '账号登录窗口'),
                        ('postprocessing', '视频后处理')):
        print(label + ('：运行中' if state.get(name) else '：未检测到活动'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Doubao Automation 本地控制')
    parser.add_argument('action', choices=('start', 'stop', 'status', 'preview-clear', 'clear-history'))
    args = parser.parse_args(argv)
    if sys.platform == 'darwin':
        # Status/preview must select the same data directory as the starter.
        from scripts.macos_bootstrap import configure_data_environment
        configure_data_environment()
    if args.action in ('preview-clear', 'clear-history'):
        script = ROOT / 'scripts' / 'clear_history.py'
        if not script.is_file():
            raise RuntimeError('历史清理组件尚未安装')
        action = 'preview' if args.action == 'preview-clear' else 'clear'
        return subprocess.run([sys.executable, str(script), action], cwd=ROOT).returncode
    if args.action == 'start':
        if sys.platform == 'darwin':
            from scripts.macos_bootstrap import start_dashboard
            start_dashboard(check_start_requirements)
            return 0
        check_start_requirements()
        import launch_dashboard
        launch_dashboard.main()
        return 0
    state = query_state()
    report_state(state)
    if args.action == 'status':
        return 0 if state is not None else 2
    # A status check alone cannot prevent a task starting immediately after it.
    # Never infer process ownership from a stale PID or terminate a process tree.
    print('安全关闭尚未支持：当前缺少阻止新任务并确认进程归属的原子关停协议；未终止任何进程。')
    return 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('操作失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
