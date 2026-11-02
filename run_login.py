"""里程碑1：弹出浏览器窗口登录 3 个豆包账号（外加 1 个 Gemini 登录窗口）。

用法:
    python run_login.py                 # 豆包×3 + Gemini
    python run_login.py --only doubao   # 只弹豆包
    python run_login.py --only gemini   # 只弹 Gemini
    python run_login.py --keep-open     # 登录成功后窗口不自动关闭

登录态保存在 profiles/ 下各自的浏览器配置目录里，之后自动化直接复用，
无需重复登录（除非账号过期）。
"""
import argparse
import sys
if sys.platform == 'darwin':
    sys.dont_write_bytecode = True
import threading
import time

try:  # Windows 控制台 UTF-8 + 行缓冲（后台运行时日志实时可见）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

from doubao_automation.browser_pool import BrowserSession
from doubao_automation.config import get, load_config
from doubao_automation.paths import ensure_dirs, use_workspace_temp

# 登录提示关键词：页面上出现任何一个都说明还没登录
_LOGIN_HINTS = ("登录", "扫码", "验证码", "手机号", "注册")


def _doubao_probe(page) -> bool:
    """DOM 后备判定：存在聊天输入框，且页面上没有任何登录提示 → 已登录。
    （宁可漏判——漏判时仍靠 cookie 判定；不能误判——误判会导致未登录却继续。）"""
    try:
        url = (page.url or "").lower()
        if "login" in url or "passport" in url or "sso" in url:
            return False
        if page.locator('div[contenteditable="true"]').count() == 0:
            return False
        body = page.locator("body").inner_text(timeout=2000)[:600]
        return not any(k in body for k in _LOGIN_HINTS)
    except Exception:
        return False


def _gemini_probe(page) -> bool:
    try:
        if "accounts.google.com" in (page.url or ""):
            return False
        return page.locator(
            'div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]'
        ).count() > 0
    except Exception:
        return False


def _worker(acct: dict, kind: str, cfg: dict, result: dict, stop_event: threading.Event):
    name = acct["name"]
    if kind == "doubao":
        url = get(cfg, "login.doubao_url", "https://www.doubao.com/chat/")
        cookies = get(cfg, "login.doubao_logged_in_cookies", [])
    else:
        url = get(cfg, "login.gemini_url", "https://gemini.google.com/app")
        cookies = get(cfg, "login.gemini_logged_in_cookies", [])

    session = BrowserSession(name, acct["profile"], cfg, start_url=url)
    try:
        session.start()
    except Exception as e:
        print(f"[login] ✗ {name} 浏览器启动失败: {e}")
        result[name] = False
        return

    if session.has_any_cookie(url, cookies):
        print(f"[login] ✓ {name} 已有登录态（复用之前保存的会话）")
        result[name] = True
    else:
        print(f"[login] ⏳ {name}: 请在弹出的浏览器窗口中完成登录（扫码/验证码均可）…")

        def on_status(ok):
            if ok:
                print(f"[login] ✓ {name} 登录成功！")

        ok = session.wait_logged_in(
            url, cookies,
            poll_s=float(get(cfg, "login.poll_interval_seconds", 5)),
            timeout_s=float(get(cfg, "login.timeout_minutes", 30)) * 60,
            on_status=on_status,
            dom_probe=_doubao_probe if kind == "doubao" else _gemini_probe,
        )
        result[name] = ok
        if not ok:
            print(f"[login] ✗ {name} 等待登录超时")

    # 登录成功后保持窗口打开，直到主线程通知收尾
    while not stop_event.is_set():
        stop_event.wait(1)
    session.stop()


def main():
    ap = argparse.ArgumentParser(description="弹出浏览器窗口完成豆包/Gemini 登录")
    ap.add_argument("--only", choices=["doubao", "gemini", "all"], default="all")
    ap.add_argument("--keep-open", action="store_true", help="全部登录后不自动关闭窗口")
    ap.add_argument("--account", default="", help="只处理指定账号名（如 doubao2）")
    args = ap.parse_args()

    cfg = load_config()
    ensure_dirs()
    use_workspace_temp()

    targets = []
    if args.only in ("doubao", "all"):
        for a in (get(cfg, "accounts.doubao") or []):
            targets.append((a, "doubao"))
    if args.only in ("gemini", "all"):
        g = get(cfg, "accounts.gemini")
        if g:
            targets.append((g, "gemini"))
    if args.account:
        targets = [(a, k) for a, k in targets if a["name"] == args.account]
    if not targets:
        print("没有匹配的账号，请检查 config.yaml accounts 配置")
        return 2

    result, stop_event = {}, threading.Event()
    threads = []
    print(f"[login] 即将弹出 {len(targets)} 个浏览器窗口: {', '.join(a['name'] for a, _ in targets)}")
    for acct, kind in targets:
        t = threading.Thread(target=_worker, args=(acct, kind, cfg, result, stop_event),
                             name=f"login-{acct['name']}", daemon=True)
        t.start()
        threads.append(t)
        time.sleep(2)  # 错开启动，避免 Chrome 抢锁

    print("[login] 请在各窗口分别登录对应账号（每个窗口登录一个不同的豆包账号）。")
    try:
        while True:
            time.sleep(3)
            if all(result.get(a["name"]) is True for a, _ in targets):
                print("\n[login] ✓✓✓ 全部账号登录成功，登录态已持久化到当前平台的用户数据目录。")
                if args.keep_open:
                    print("[login] --keep-open：窗口保持打开，按 Ctrl+C 结束。")
                    while True:
                        time.sleep(5)
                break
            if all(result.get(a["name"]) is not None for a, _ in targets) and \
               any(result.get(a["name"]) is False for a, _ in targets):
                failed = [a["name"] for a, _ in targets if result.get(a["name"]) is False]
                print(f"\n[login] 以下账号未完成登录: {', '.join(failed)}（可稍后重跑本脚本）")
                break
    except KeyboardInterrupt:
        print("\n[login] 手动中断")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=15)
    return 0 if all(result.get(a["name"]) for a, _ in targets) else 1


if __name__ == "__main__":
    sys.exit(main())
