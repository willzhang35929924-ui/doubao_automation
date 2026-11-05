"""浏览器池：每个账号一个独立的 Chrome 用户配置目录（登录态持久化）。

Playwright 同步 API 是线程绑定的：每个 BrowserSession 必须在自己的线程里
start()/stop()。编排器为每个豆包账号开一个工作线程，各自持有一个会话。
"""
import sys
import threading
import time
from pathlib import Path

from .config import get
from .browser_runtime import resolve_browser_runtime
from .paths import PROFILES, resolve


class BrowserSession:
    """一个持久化 Chrome 上下文（独立 profile = 独立登录态）。"""

    def __init__(self, name: str, profile_dir, cfg: dict, start_url: str = ""):
        self.name = name
        self.profile_dir = Path(resolve(profile_dir))
        if sys.platform == "darwin":
            # Keep all macOS sessions inside the selected user-data root.
            profile = self.profile_dir.resolve()
            if PROFILES.resolve() not in profile.parents:
                raise ValueError("macOS 浏览器配置必须位于用户数据 profiles 目录")
            self.profile_dir = profile
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.start_url = start_url
        self._pw = None
        self.context = None
        self.page = None
        self._lock = threading.RLock()

    # ---- 生命周期 ----
    def start(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        with self._lock:
            if self.context is not None:
                return self
            self._pw = sync_playwright().start()
            try:
                runtime = resolve_browser_runtime(self.cfg, self._pw.chromium.executable_path)
                if not runtime['available']:
                    raise RuntimeError(runtime['error'])
                kwargs = dict(
                    user_data_dir=str(self.profile_dir),
                    headless=bool(get(self.cfg, "browser.headless", False)),
                    args=self._chrome_args(),
                    no_viewport=True,
                    accept_downloads=True,
                    locale=get(self.cfg, "browser.locale", "zh-CN"),
                    timeout=60000,
                )
                timeout_ms = int(get(self.cfg, "browser.default_timeout_ms", 30000))
                channel = runtime['channel']
                if channel:
                    kwargs['channel'] = channel
                try:
                    self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if any(token in exc_str for token in ('processsingleton', 'already in use', 'currently in use')):
                        raise RuntimeError(f'[{self.name}] 账号浏览器配置正在使用，请先关闭该账号的已有窗口再重试') from exc
                    # Keep the Windows default-channel fallback, but do not
                    # silently override an explicitly configured browser.
                    if (channel and not runtime['explicit'] and runtime['managed_available']
                            and 'executable' in exc_str and 'exist' in exc_str):
                        kwargs.pop('channel', None)
                        self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                    else:
                        raise RuntimeError(f'[{self.name}] 浏览器启动失败：{exc}') from exc
                self.context.set_default_timeout(timeout_ms)
                self.context.set_default_navigation_timeout(max(timeout_ms, 60000))
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                if not kwargs['headless']:
                    self.page.bring_to_front()
            except Exception:
                # Also release the driver/context on invalid settings and
                # post-launch initialization errors, not only launch failures.
                self.stop()
                raise
            if self.start_url:
                try:
                    self.page.goto(self.start_url, wait_until="domcontentloaded")
                except Exception as exc:
                    print(f"[{self.name}] 打开 {self.start_url} 失败: {exc}")
            return self

    def _chrome_args(self) -> list:
        args = [
            "--disable-blink-features=AutomationControlled",  # 降低被识别为自动化的概率
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if get(self.cfg, "browser.start_maximized", True):
            args.append("--start-maximized")
        return args

    def stop(self) -> None:
        with self._lock:
            try:
                if self.context is not None:
                    self.context.close()
            except Exception:
                pass
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:
                pass
            self.context = None
            self.page = None
            self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ---- 登录态检测 ----
    def cookies(self, url: str = "") -> list:
        if self.context is None:
            return []
        try:
            return self.context.cookies(url) if url else self.context.cookies()
        except Exception:
            return []

    def has_any_cookie(self, url: str, names) -> bool:
        names = set(names or [])
        return any(c.get("name") in names for c in self.cookies(url))

    def wait_logged_in(self, url: str, cookie_names, poll_s: float = 5.0,
                       timeout_s: float = 1800.0, on_status=None, dom_probe=None,
                       login_probe=None) -> bool:
        """轮询等待登录成功；返回是否成功。on_status(logged_in: bool) 可回调。

        login_probe(session) 可提供统一判定；指定后不再被宽松的 cookie/DOM
        后备规则短路。其他站点仍使用 cookie 命中或 dom_probe(page)。
        """
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            if login_probe is not None:
                ok = bool(login_probe(self))
            else:
                ok = self.has_any_cookie(url, cookie_names)
                if not ok and dom_probe is not None and self.page is not None:
                    try:
                        ok = bool(dom_probe(self.page))
                    except Exception:
                        ok = False
            if ok != last:
                last = ok
                if on_status:
                    on_status(ok)
            if ok:
                return True
            if self.page is not None and not self.page.is_closed():
                self.page.wait_for_timeout(poll_s * 1000)
            else:
                time.sleep(poll_s)
        return False
