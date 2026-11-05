"""Resolve installed browser executables without opening a browser or profile."""
import shlex
import sys
from pathlib import Path


MAC_CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


def resolve_browser_runtime(cfg, managed_executable, *, platform=None):
    """Return the launch selection, respecting an explicitly configured channel.

    Auto mode on macOS prefers the Chromium matching the current Playwright
    version. If that version is missing, installed Chrome can use the same
    separate project profile instead. No browser is downloaded here.
    """
    platform = platform or sys.platform
    browser = cfg.get('browser') or {}
    configured = browser.get('channel')
    explicit = configured is not None
    managed = Path(managed_executable)
    managed_available = managed.is_file()
    result = dict(available=False, channel=None, executable=str(managed),
                  managed_executable=str(managed), managed_available=managed_available,
                  explicit=explicit, source='managed', error='', install_managed=False)
    command = shlex.quote(sys.executable) + ' -m playwright install chromium'
    if explicit and not isinstance(configured, str):
        result['error'] = 'browser.channel 必须为浏览器名称；使用 chromium 或 chrome，或移除此配置以自动选择。'
        return result
    channel = configured.strip().lower() if explicit else ('chrome' if platform == 'win32' else None)
    if channel in ('', 'chromium'):
        channel = None
    if channel:
        result.update(channel=channel, source='configured-channel' if explicit else 'default-channel',
                      executable=None)
        # Other platforms/channels let Playwright resolve its supported channel
        # paths. Only Chrome's known macOS executable is inspected here.
        if platform == 'darwin' and channel == 'chrome':
            result.update(executable=str(MAC_CHROME), available=MAC_CHROME.is_file())
            if not result['available']:
                result['error'] = ('配置要求 Chrome，但未找到 /Applications/Google Chrome.app。'
                                   '请安装 Chrome，或将 browser.channel 改为 chromium 后运行：' + command)
        else:
            result['available'] = True
        return result
    if managed_available:
        result['available'] = True
        return result
    if platform == 'darwin' and not explicit and MAC_CHROME.is_file():
        result.update(available=True, channel='chrome', executable=str(MAC_CHROME), source='system-chrome')
        return result
    result.update(install_managed=True,
                  error=('未找到当前 Playwright 版本匹配的 Chromium；请重新运行项目的一键启动入口自动检查，'
                         '或执行：' + command))
    return result


def inspect_browser_runtime(cfg):
    """Return a plain status dict; starts only the short-lived Playwright driver."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return resolve_browser_runtime(cfg, playwright.chromium.executable_path)
