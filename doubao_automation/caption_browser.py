"""Read a public TikTok product with a fresh, network-restricted browser."""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "doubao_automation"

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .browser_runtime import resolve_browser_runtime
from .caption_page import CaptionPageError, ProductPage, is_tiktok_product_url, normalize_page_url
from .paths import TEMP_DIR


_TIMEOUT = 55
_PROCESS_TIMEOUT = 70
_MAX_RESULT_BYTES = 48_000
_MAX_REQUESTS = 180
_MAX_TEXT = 6400
_BLOCKED_MEDIA = frozenset(("media", "image", "font", "manifest"))
_VERIFICATION = re.compile(
    r"^(?:security check|robot check|verify you are human|attention required|"
    r"just a moment|sign in|log in|安全验证|人机验证|请先登录)", re.I,
)
_SNAPSHOT = """() => {
    const heading = document.querySelector('h1')?.innerText?.trim() || '';
    const product = document.getElementById('pdp-about-this-product-section');
    const section = product || document.querySelector('main,article') || document.body;
    const visible = element => {
        const box = element.getBoundingClientRect(), style = getComputedStyle(element);
        return box.width > 0 && box.height > 0 && style.visibility !== 'hidden'
            && style.display !== 'none' && style.opacity !== '0';
    };
    const gates = document.querySelectorAll(
        'iframe[src*="captcha"], [id*="captcha"], [class*="captcha"], [id="verify-bar-code"]');
    return {
        url: location.href,
        title: document.title.slice(0, 240),
        heading: heading.slice(0, 240),
        text: (section?.innerText || '').slice(0, 6400),
        product_section: Boolean(product),
        verification: Array.from(gates).some(visible)
    };
}"""


def _product_identity(url):
    value = normalize_page_url(url)
    if not is_tiktok_product_url(value) or not value.startswith("https://"):
        raise CaptionPageError("浏览器识别仅支持公开的 HTTPS TikTok Shop 商品详情链接。")
    path = urlsplit(value).path.strip("/").split("/")
    return path[0], path[-1]


def _timeout_ms(deadline, maximum=25_000):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise CaptionPageError("浏览器读取商品页面超时，请稍后重试。")
    return min(remaining, maximum)


def _rendered_product(snapshot, expected):
    if not isinstance(snapshot, dict) or _product_identity(snapshot.get("url")) != expected:
        raise CaptionPageError("商品页面跳转到了其他地址，已停止识别。")
    title = snapshot.get("title", "")
    heading = snapshot.get("heading", "")
    text = snapshot.get("text", "")
    if (not all(isinstance(value, str) for value in (title, heading, text))
            or snapshot.get("verification") or _VERIFICATION.match(title.strip())
            or _VERIFICATION.match(heading.strip())):
        raise CaptionPageError("商品页面需要登录或安全验证，已停止自动识别。")
    if (not snapshot.get("product_section") or not heading.strip()
            or heading.strip().lower() in ("tiktok shop", "shop") or len(text.strip()) < 40):
        return None
    evidence = f"商品名称：{heading.strip()}\n页面可见商品资料：\n{text.strip()}"
    return ProductPage(normalize_page_url(snapshot["url"]), heading.strip()[:240], evidence[:_MAX_TEXT])


def _close_page(page):
    with contextlib.suppress(PlaywrightError):
        page.close()


def _record_browser_group(browser, path):
    session = browser.new_browser_cdp_session()
    try:
        processes = session.send("SystemInfo.getProcessInfo")["processInfo"]
        pid = next(int(item["id"]) for item in processes if item["type"] == "browser")
        if pid <= 1 or (os.name != "nt" and os.getpgid(pid) != pid):
            raise ValueError
        _write_private_json(path, {"browser_pid": pid, "worker_pid": os.getpid()})
    except (OSError, KeyError, TypeError, ValueError, StopIteration):
        raise CaptionPageError("无法确认商品读取浏览器的进程归属，已停止读取。") from None
    finally:
        session.detach()


def _read_with_browser(playwright, url, proxy, deadline, manifest_path=None):
    runtime = resolve_browser_runtime({}, playwright.chromium.executable_path)
    if not runtime["available"]:
        raise CaptionPageError("没有可用的商品读取浏览器，请安装项目对应的 Chromium 后重试。")
    launch = dict(headless=False, chromium_sandbox=True, proxy=proxy,
                  timeout=_timeout_ms(deadline, 20_000), args=[
                      "--proxy-bypass-list=<-loopback>", "--disable-quic",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                  ])
    if runtime["channel"]:
        launch["channel"] = runtime["channel"]
    browser = playwright.chromium.launch(**launch)
    try:
        if manifest_path is not None:
            _record_browser_group(browser, manifest_path)
        context = browser.new_context(accept_downloads=False, service_workers="block", locale="en-US")
        try:
            context.set_default_timeout(3000)
            if not hasattr(context, "route_web_socket"):
                raise CaptionPageError("浏览器组件版本过低，请更新项目依赖后重试。")
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            expected = _product_identity(url)
            requests = 0
            forbidden_navigation = False

            def route_request(route):
                nonlocal requests, forbidden_navigation
                requests += 1
                request = route.request
                navigation = request.is_navigation_request()
                main_navigation = navigation and request.frame == page.main_frame
                if navigation and not main_navigation:
                    route.abort()
                    return
                try:
                    target = normalize_page_url(request.url)
                    allowed = target.startswith("https://")
                    if main_navigation:
                        allowed = allowed and _product_identity(target) == expected
                        forbidden_navigation = forbidden_navigation or not allowed
                except (CaptionPageError, ValueError):
                    allowed = False
                    forbidden_navigation = forbidden_navigation or main_navigation
                if not allowed or requests > _MAX_REQUESTS or request.resource_type in _BLOCKED_MEDIA:
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", route_request)
            context.on("page", _close_page)
            page.on("download", lambda download: download.cancel())
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_timeout_ms(deadline))
            except PlaywrightTimeoutError:
                pass  # A slow subresource need not prevent reading an already rendered product.
            while True:
                _timeout_ms(deadline)
                if forbidden_navigation:
                    raise CaptionPageError("商品页面跳转到了其他地址，已停止识别。")
                if requests > _MAX_REQUESTS:
                    raise CaptionPageError("商品页面请求过多，已停止读取。")
                snapshot = page.evaluate(_SNAPSHOT)
                product = _rendered_product(snapshot, expected)
                if product is not None:
                    return product
                page.wait_for_timeout(min(400, _timeout_ms(deadline)))
        finally:
            with contextlib.suppress(PlaywrightError):
                context.close()
    finally:
        with contextlib.suppress(PlaywrightError):
            browser.close()


def _read_browser_product_page(url, manifest_path):
    from .caption_browser_proxy import public_browser_proxy

    deadline = time.monotonic() + _TIMEOUT
    try:
        with public_browser_proxy(timeout=_TIMEOUT + 5) as proxy:
            with sync_playwright() as playwright:
                return _read_with_browser(playwright, url, proxy, deadline, manifest_path)
    except CaptionPageError:
        raise
    except (PlaywrightError, OSError, RuntimeError):
        raise CaptionPageError("浏览器无法读取商品页面，请检查浏览器和本机网络后重试。") from None


def _write_private_json(path, value):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False)


def _recorded_browser_pid(process, manifest):
    try:
        with manifest.open("rb") as stream:
            data = json.loads(stream.read(512))
        reported = data["browser_pid"]
        if (data["worker_pid"] == process.pid and type(reported) is int and reported > 1
                and reported not in (process.pid, os.getpid())
                and (os.name == "nt" or os.getpgid(reported) == reported)):
            return reported
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _stop_recorded_browser(process, manifest):
    browser_pid = _recorded_browser_pid(process, manifest)
    if browser_pid is None:
        return None
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(browser_pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(browser_pid, signal.SIGKILL)
    return browser_pid


def _stop_worker(process, manifest):
    browser_pid = _stop_recorded_browser(process, manifest)
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    else:
        if browser_pid is None:
            # Before CDP can record Chromium, let the owned driver close a
            # browser that may still be starting in its separate process group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            time.sleep(0.25)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def read_browser_product_page(url: str) -> ProductPage:
    """Read in an owned process so a hung page cannot block the workbench."""
    current = normalize_page_url(url)
    _product_identity(current)
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="caption-browser-", dir=TEMP_DIR) as directory:
            temp = Path(directory)
            request, result, manifest = (temp / name for name in ("request.json", "result.json", "browser.json"))
            _write_private_json(request, {"url": current})
            allowed_env = ("HOME", "USERPROFILE", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR",
                           "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
                           "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            env = {name: os.environ[name] for name in allowed_env if name in os.environ}
            env.update(PATH=os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                       LANG="en_US.UTF-8", TMPDIR=str(temp), PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker",
                       str(request), str(result), str(manifest)]
            process = subprocess.Popen(command, cwd=temp, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=os.name != "nt",
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                process.wait(timeout=_PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise CaptionPageError("浏览器读取超过时限，已结束本次商品识别。") from None
            finally:
                if process.poll() is None:
                    _stop_worker(process, manifest)
                else:
                    _stop_recorded_browser(process, manifest)
            if process.returncode != 0 or not result.is_file() or result.stat().st_size > _MAX_RESULT_BYTES:
                raise CaptionPageError("商品读取浏览器异常退出，请稍后重试。")
            data = json.loads(result.read_bytes())
            if not isinstance(data, dict):
                raise ValueError
            if data.get("error"):
                raise CaptionPageError(data["error"][:512])
            product = ProductPage(**data)
            if (_product_identity(product.url) != _product_identity(current)
                    or not isinstance(product.title, str) or len(product.title) > 240
                    or not isinstance(product.text, str) or not 0 < len(product.text) <= _MAX_TEXT):
                raise ValueError
            return product
    except CaptionPageError:
        raise
    except (OSError, ValueError, TypeError):
        raise CaptionPageError("无法启动或读取商品浏览器结果，请检查项目环境后重试。") from None


def _worker_main():
    request_path, result_path, manifest_path = map(Path, sys.argv[2:])
    try:
        request = json.loads(request_path.read_bytes())
        current = normalize_page_url(request["url"])
        _product_identity(current)
        product = _read_browser_product_page(current, manifest_path)
        response = dict(url=product.url, title=product.title, text=product.text)
    except CaptionPageError as exc:
        response = {"error": str(exc)[:512]}
    except Exception:
        response = {"error": "商品读取浏览器运行失败，请检查浏览器和本机网络。"}
    _write_private_json(result_path, response)


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--worker":
    _worker_main()
