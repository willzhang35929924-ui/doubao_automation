"""Shared Doubao login detection; never treat visitor cookies as authentication."""
import re
import time
from urllib.parse import urlsplit

from .config import get

_SESSION_COOKIES = frozenset(('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard',
                              'sid_ucp_v1', 'ssid_ucp_v1'))
_VISITOR_COOKIES = frozenset(('uid_tt', 'uid_tt_ss', 'ttwid', 'tt_webid', 'tt_webid_v2',
                              'odin_tt', 'passport_csrf_token', 'passport_csrf_token_default'))
_INPUTS = '[contenteditable="true"], textarea, [role="textbox"]'
_LOGIN_BUTTON = re.compile(r'^(登录|立即登录|登录[/／注册 ]*|扫码登录|Log in|Sign in)$', re.I)


def session_pages(session):
    """Read pages only from the owning Playwright thread, including login popups."""
    pages = list(session.context.pages) if session.context is not None else []
    if session.page is not None and session.page not in pages:
        pages.insert(0, session.page)
    return [page for page in pages if not page.is_closed()]


def _visible(locator):
    return any(locator.nth(i).is_visible() for i in range(locator.count()))


def probe_doubao_login(session, cfg):
    """Require an authenticated session AND usable chat UI on a trusted page.

    Empty/legacy cookie configuration falls back to built-in session names. A
    guest editor alone, a hidden editor, or a cached cookie on a login page is
    not sufficient. No cookie values or conversation text leave this function.
    """
    configured = get(cfg, 'login.doubao_logged_in_cookies', []) or []
    if isinstance(configured, str):
        configured = [configured]
    expected = (_SESSION_COOKIES | set(configured)) - _VISITOR_COOKIES
    selectors = get(cfg, 'doubao.selectors.input_box', []) or []
    if isinstance(selectors, str):
        selectors = [selectors]
    inputs = ', '.join([_INPUTS, *selectors])
    note = '等待豆包聊天页面加载或完成登录'
    for page in session_pages(session):
        try:
            url = urlsplit(page.url)
            if url.scheme not in ('http', 'https') or url.hostname not in ('www.doubao.com', 'doubao.com'):
                continue
            if any(part in url.path.lower() for part in ('login', 'passport', 'sso')):
                continue
            if _visible(page.get_by_role('button', name=_LOGIN_BUTTON)):
                note = '页面仍显示登录入口，请完成登录后等待自动验证'
                continue
            if _visible(page.locator('input[type="tel"], input[autocomplete="one-time-code"]')):
                note = '页面仍显示登录表单，请完成登录后等待自动验证'
                continue
            cookies = session.cookies(page.url)
            authenticated = any(c.get('name') in expected and c.get('value')
                                and (c.get('expires', -1) <= 0 or c['expires'] > time.time())
                                for c in cookies)
            if not authenticated:
                note = '尚未检测到有效会话，请在此账号的独立浏览器窗口中登录'
                continue
            if not _visible(page.locator(inputs)):
                note = '已检测到会话，等待聊天输入框显示'
                continue
            session.page = page
            return dict(logged_in=True, note='登录已验证，浏览器配置已保存')
        except Exception:
            # Redirects/navigation can replace the document during a poll.
            note = '页面正在跳转或加载，稍后自动重试登录验证'
    return dict(logged_in=False, note=note)
