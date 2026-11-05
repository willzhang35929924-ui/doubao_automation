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
import copy
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
from doubao_automation.config import get, load_config, save_config
from doubao_automation.doubao_login import probe_doubao_login
from doubao_automation.gemini_web import GeminiWeb
from doubao_automation.account_manager import AccountManager
from doubao_automation.paths import STATE_DIR, ensure_dirs, use_workspace_temp
from doubao_automation.state import JsonStore

_LOGIN_SAVE_LOCK = threading.Lock()


def _save_doubao_login(acct):
    # Concurrent login workers must merge into the latest settings, not the
    # startup snapshot. Login verification never resets the quota ledger.
    with _LOGIN_SAVE_LOCK:
        cfg = load_config()
        current = next((a for a in get(cfg, 'accounts.doubao', [])
                        if a['name'] == acct['name'] and a['profile'] == acct['profile']), None)
        if current is None:
            raise ValueError('账号配置已改变，请重新打开登录窗口')
        current['enabled'] = True
        save_config(cfg)
        manager = AccountManager(JsonStore(STATE_DIR / 'accounts.json'), [acct['name']])
        manager.mark_available_after_login(acct['name'])


def _worker(acct: dict, kind: str, cfg: dict, result: dict, stop_event: threading.Event):
    cfg = copy.deepcopy(cfg)
    cfg['browser'] = dict(cfg.get('browser') or {}, headless=False)
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
        if kind == 'doubao':
            login_probe = lambda sess: probe_doubao_login(sess, cfg)['logged_in']
        else:
            gemini = GeminiWeb(session, cfg)
            login_probe = lambda sess: gemini.login_ready()
        ok = login_probe(session)
        if ok:
            print(f"[login] ✓ {name} 已有登录态（复用之前保存的会话）")
        else:
            method = '登录 Google 账号' if kind == 'gemini' else '完成豆包登录（扫码/验证码均可）'
            print(f"[login] ⏳ {name}: 请在弹出的浏览器窗口中{method}…")
            ok = session.wait_logged_in(
                url, cookies,
                poll_s=float(get(cfg, "login.poll_interval_seconds", 5)),
                timeout_s=float(get(cfg, "login.timeout_minutes", 30)) * 60,
                login_probe=login_probe,
            )
        if ok and kind == 'doubao':
            _save_doubao_login(acct)
        result[name] = ok
        print(f"[login] {'✓ 登录成功，状态已保存' if ok else '✗ 等待登录超时'}: {name}")
        # Keep the window on its owning Playwright thread until main finishes.
        while not stop_event.is_set() and not session.page.is_closed():
            session.page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[login] ✗ {name} 登录验证失败: {e}")
        result[name] = False
    finally:
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
        g = {'name':'gemini', 'profile':'profiles/gemini', **(get(cfg, "accounts.gemini") or {})}
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

    print("[login] 请在各窗口登录对应账号：Gemini 使用 Google 账号，豆包使用各自的独立账号。")
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
