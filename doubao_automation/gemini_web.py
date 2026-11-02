"""Gemini 网页版自动化：上传源视频 + 固定指令 + 提示词 → 抓取生成的多个脚本。

选择器全部来自 config.yaml gemini.selectors，页面改版只需改配置。
实机校准：python run_pipeline.py inspect-gemini
"""
import sys
import time
import re
import json
import hashlib
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit

from .config import get
from .paths import DATA_ROOT, ROOT, WORKSPACE, resolve


class GeminiError(Exception):
    pass


class GeminiNotLoggedIn(GeminiError):
    pass


def _combine(selectors) -> str:
    if isinstance(selectors, str):
        return selectors
    return ", ".join(s for s in (selectors or []) if s)


def _selectors(value):
    return [value] if isinstance(value,str) else list(value or [])


def _explicit_script_count(message):
    """Recognize unambiguous requests for scripts; never infer a default count."""
    # Per-source supplemental instructions override the common prompt.
    parts=message.rsplit('【本次附加提示词】',1)
    sections=list(reversed(parts)) if len(parts)>1 else parts
    words={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    def number(value):
        if value.isdigit():return int(value)
        if value=='十':return 10
        if '十' in value:
            left,right=value.split('十',1)
            return (words.get(left,1) if left else 1)*10+(words.get(right,0) if right else 0)
        return words.get(value,0)
    pattern=r'(?:生成|输出|写|创作|提供|撰写|制作|需要|要求|共计|总共|共)\s*(?:再|共|总共|额外|另外)?\s*(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:个|条|篇|份)\s*[^，。；;\n]{0,25}?脚本'
    for index,section in enumerate(sections):
        values=[number(value) for value in re.findall(pattern,section)]
        values += [int(value) for value in re.findall(r'\b(?:write|generate|create|produce)\s+(\d{1,3})\s+(?:video\s+)?scripts\b',section,re.I)]
        values={value for value in values if 0<value<=100}
        if len(values)==1:return values.pop()
        if values:return None  # conflicting instructions must not become a guessed hard limit
        if len(sections)>1 and index==0:
            durations=_requested_durations(section)
            if durations:return len(durations)
    return None


def _requested_durations(message, requested_count=None):
    parts=message.rsplit('【本次附加提示词】',1)
    sections=list(reversed(parts)) if len(parts)>1 else parts
    for section in sections:
        values=[int(value) for value in re.findall(r'Script\s*\d+\s*[:=：]\s*(\d+)\s*秒',section,re.I)]
        if values:return values
        # A common natural request is "生成1个50秒和一个70秒的视频".
        # Only infer the pair when it doesn't conflict with an explicit script count.
        if requested_count not in (None,2):continue
        pair=re.search(r'(?:生成|输出|写|制作|创作)[^。；\n]{0,24}?(?:1|一)(?:个|条)\s*(\d+)\s*秒(?:的)?(?:视频|脚本)?\s*(?:和|与|、|以及)\s*(?:1|一)(?:个|条)\s*(\d+)\s*秒',section)
        if pair:return [int(pair.group(1)),int(pair.group(2))]
    return []


def _resolve_audit_dir(value):
    """Keep macOS diagnostics out of the source tree, including configured paths."""
    if not value:
        return WORKSPACE / 'audit' / datetime.now().strftime('%Y%m%d') / 'long_video_run'
    if sys.platform != 'darwin':
        return Path(resolve(value))
    text = str(value)
    if PureWindowsPath(text).is_absolute():
        raise ValueError('macOS 诊断目录不能使用 Windows 绝对路径')
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (DATA_ROOT / path).resolve()
        if path != DATA_ROOT.resolve() and DATA_ROOT.resolve() not in path.parents:
            raise ValueError('相对诊断目录不能逃逸用户数据目录')
    else:
        path = path.resolve()
    project = ROOT.resolve()
    if path == project or project in path.parents:
        raise ValueError('macOS 诊断目录不能位于源码目录')
    return path

class GeminiWeb:
    def __init__(self, session, cfg: dict):
        self.session = session
        self.cfg = cfg
        self.gcfg = cfg.get("gemini", {}) or {}
        self.sel = self.gcfg.get("selectors", {}) or {}
        self.url = get(cfg, "login.gemini_url", "https://gemini.google.com/app")
        self.audit_dir = _resolve_audit_dir(self.gcfg.get('audit_dir'))
        self._sent_baseline = None
        self._last_observed_response = ''

    def _capture_diagnostic(self,stage,response='',error=''):
        """Persist failures locally; never export cookies, storage or authentication query strings."""
        prefix=datetime.now().strftime('gemini_%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]+'_'+stage
        raw=response or self._last_observed_response
        payload=dict(stage=stage,error=str(error),response_chars=len(raw),
                     response_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                     baseline=self._sent_baseline)
        try:self.audit_dir.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            print(f'[Gemini] 本地诊断目录不可写：{type(exc).__name__}',flush=True)
            return prefix
        try:(self.audit_dir/(prefix+'.response.txt')).write_text(raw,encoding='utf-8')
        except OSError as exc:payload['response_write_error']=type(exc).__name__
        try:
            url=urlsplit(self.page.url)
            payload['url']=urlunsplit((url.scheme,url.netloc,url.path,'',''))
        except Exception as exc:payload['url_capture_error']=type(exc).__name__
        try:
            (self.audit_dir/(prefix+'.page.txt')).write_text(self.page.locator('body').inner_text(timeout=5000),encoding='utf-8')
        except Exception as exc:payload['page_capture_error']=type(exc).__name__
        try:
            self.page.screenshot(path=str(self.audit_dir/(prefix+'.png')),full_page=True,timeout=10000)
        except Exception as exc:payload['screenshot_capture_error']=type(exc).__name__
        try:(self.audit_dir/(prefix+'.json')).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError as exc:print(f'[Gemini] 本地诊断索引写入失败：{type(exc).__name__}',flush=True)
        print(f'[Gemini] 诊断已保存：{prefix}（回复{len(raw)}字）',flush=True)
        return prefix

    # ---- 基础 ----
    @property
    def page(self):
        return self.session.page

    def logged_in(self) -> bool:
        return self.session.has_any_cookie(
            "https://gemini.google.com",
            get(self.cfg, "login.gemini_logged_in_cookies", ["__Secure-1PSID"]),
        )

    def open(self):
        page = self.page
        page.goto(self.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cur = page.url
        if "accounts.google.com" in cur or "signin" in cur.lower():
            raise GeminiNotLoggedIn("Gemini 未登录：请先运行 python run_login.py 在弹出的窗口中登录 Google 账号")
        editor = _combine(self.sel.get("prompt_editor")) or 'rich-textarea [contenteditable="true"], [contenteditable="true"][role="textbox"]'
        if editor:
            try:
                page.locator(editor).first.wait_for(state="visible", timeout=20000)
            except Exception as exc:
                self._capture_diagnostic('editor_unavailable',error=str(exc))
                raise GeminiNotLoggedIn('Gemini输入框不可用，可能需要人工登录或确认新条款；未自动点击同意/继续') from exc

    # ---- 上传 ----
    def upload_files(self, paths: list):
        """三层策略：A 常驻 input 直接注入 → B 点入口捕获系统文件选择框 → C 点菜单项捕获。"""
        page = self.page
        files = [str(p) for p in paths]
        file_input = _combine(self.sel.get("file_input")) or 'input[type="file"]'
        menu_items = _selectors(self.sel.get("upload_menu_items"))
        ok = False
        # A) 页面已有 file input
        try:
            loc = page.locator(file_input)
            if loc.count() > 0:
                loc.first.set_input_files(files, timeout=8000)
                ok = True
        except Exception:
            pass
        # B/C) 点 "Upload & tools" 等入口
        if not ok:
            for opener in _selectors(self.sel.get("open_upload")):
                if ok:
                    break
                try:
                    btn = page.locator(opener).first
                    if btn.count() == 0 or not btn.is_visible():
                        continue
                except Exception:
                    continue
                # B) 点击直接弹系统文件框
                try:
                    with page.expect_file_chooser(timeout=6000) as fc:
                        btn.click(timeout=4000)
                    fc.value.set_files(files)
                    ok = True
                    break
                except Exception:
                    pass
                # C) 打开了菜单 → 逐个菜单项尝试
                page.wait_for_timeout(1200)
                for label in menu_items:
                    if ok:
                        break
                    item = None
                    for finder in (lambda: page.get_by_role("menuitem", name=label),
                                   lambda: page.get_by_text(label, exact=False)):
                        try:
                            cand = finder()
                            if cand.count() > 0 and cand.first.is_visible():
                                item = cand.first
                                break
                        except Exception:
                            continue
                    if item is None:
                        continue
                    try:
                        with page.expect_file_chooser(timeout=8000) as fc2:
                            item.click(timeout=4000)
                        fc2.value.set_files(files)
                        ok = True
                    except Exception:
                        try:
                            loc = page.locator(file_input)
                            if loc.count() > 0:
                                loc.first.set_input_files(files, timeout=4000)
                                ok = True
                        except Exception:
                            pass
                if not ok:  # 菜单点开后可能出现了常驻 input
                    try:
                        loc = page.locator(file_input)
                        if loc.count() > 0:
                            loc.first.set_input_files(files, timeout=4000)
                            ok = True
                    except Exception:
                        pass
                if not ok:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
        if not ok:
            raise GeminiError("无法完成 Gemini 文件上传，请运行 dryrun-gemini / inspect-gemini 校准选择器")
        settle = float(self.gcfg.get("upload_settle_seconds", 8)) * max(len(paths), 1)
        print(f"[Gemini] 已提交 {len(paths)} 个文件，等待上传完成（约 {settle:.0f}s）…")
        page.wait_for_timeout(int(settle * 1000))

    # ---- 发送与等待 ----
    def type_prompt(self,text):
        editor=_combine(self.sel.get('prompt_editor')) or 'rich-textarea [contenteditable="true"]'
        loc=self.page.locator(editor).first
        loc.click(timeout=10000)
        try:loc.fill(text,timeout=10000)
        except Exception:
            self.page.keyboard.press('Meta+A' if sys.platform == 'darwin' else 'Control+A')
            self.page.keyboard.insert_text(text)
        self.page.wait_for_timeout(300)

    @staticmethod
    def _clean_response(text):
        text=(text or '').strip()
        return re.sub(r'^\s*(?:Gemini said|Gemini 回答|Gemini 说)\s*[:：]?\s*\n+', '',text,flags=re.I).strip()

    def _response_snapshot(self):
        # Prefer the whole model turn: its last markdown child can be only a footer/code block.
        selectors=['model-response','[data-message-author-role="model"]','.model-response-text']
        for configured in _selectors(self.sel.get('response_text')):
            if re.search(r'model|assistant|response',configured,re.I) and configured not in selectors:
                selectors.append(configured)
        texts=[];selected=''
        for selector in selectors:
            try:
                loc=self.page.locator(selector)
                if loc.count():
                    texts=[self._clean_response(loc.nth(i).inner_text(timeout=3000)) for i in range(loc.count())]
                    selected=selector;break
            except Exception:continue
        try:users=self.page.locator('user-query, [data-message-author-role="user"]').count()
        except Exception:users=0
        return dict(model_count=len(texts),user_count=users,selector=selected,
                    texts=texts,response_hashes=[hashlib.sha256(t.encode('utf-8')).hexdigest() for t in texts])

    def _last_response_text(self):
        snapshot=self._response_snapshot()
        return snapshot['texts'][-1] if snapshot['texts'] else ''

    def _stop_button(self):
        selector=_combine(self.sel.get('stop_button'))
        return self.page.locator(selector).first if selector else None

    def _stop_visible(self):
        selectors=_selectors(self.sel.get('stop_button'))
        selectors+=['button[aria-label*="Stop"]','button[aria-label*="停止"]','button.stop-button']
        for selector in selectors:
            try:
                loc=self.page.locator(selector)
                if any(loc.nth(i).is_visible() for i in range(loc.count())):return True
            except Exception:continue
        return False

    @staticmethod
    def _fresh_response(snapshot,baseline):
        text=snapshot['texts'][-1] if snapshot['texts'] else ''
        if not text:return ''
        if snapshot['model_count']>baseline['model_count']:return text
        fingerprint=hashlib.sha256(text.encode('utf-8')).hexdigest()
        if (snapshot['user_count']>baseline['user_count']
                and fingerprint not in baseline['response_hashes']):return text
        return ''

    def send_prompt(self,text):
        self._sent_baseline=self._response_snapshot()
        self.type_prompt(text)
        sent=False
        buttons=_selectors(self.sel.get('send_button')) or ['button[aria-label="Send message"]','button[aria-label="发送消息"]']
        ready_deadline=time.monotonic()+float(self.gcfg.get('send_ready_timeout_seconds',180))
        while time.monotonic()<ready_deadline:
            visible=False
            for selector in buttons:
                try:
                    matches=self.page.locator(selector)
                    for index in range(matches.count()):
                        button=matches.nth(index)
                        if not button.is_visible():continue
                        visible=True
                        if not button.is_enabled():continue
                        # A click timeout can happen after the request was accepted.
                        # Attempt once, then confirm via the conversation instead of resending.
                        sent=True
                        try:button.click(timeout=5000)
                        except Exception:pass
                        break
                except Exception:continue
                if sent:break
            if sent or not visible:break
            self.page.wait_for_timeout(500)
        else:
            self._capture_diagnostic('send_not_ready',error='Send control stayed disabled after upload/draft')
            raise GeminiError('上传或发送按钮迟迟未就绪；未用回车绕过禁用按钮，已保存诊断')
        if not sent:self.page.keyboard.press('Enter')
        # A successful click is not proof that Gemini accepted the request.
        deadline=time.monotonic()+float(self.gcfg.get('send_ack_timeout_seconds',20))
        while time.monotonic()<deadline:
            snapshot=self._response_snapshot()
            if (snapshot['user_count']>self._sent_baseline['user_count']
                    or snapshot['model_count']>self._sent_baseline['model_count'] or self._stop_visible()):
                print('[Gemini] 已确认本次消息进入对话，等待新回复…',flush=True)
                return
            self.page.wait_for_timeout(500)
        self._capture_diagnostic('send_unacknowledged',error='No new user/model turn after send')
        raise GeminiError('点击后未确认新消息进入对话；已保存截图，不会把旧回复当作本次结果')

    def wait_response(self):
        if self._sent_baseline is None:raise GeminiError('缺少发送前回复基线，不能确认本次新回复')
        start=time.monotonic()
        start_deadline=start+float(self.gcfg.get('response_start_timeout_s',90))
        deadline=start+float(self.gcfg.get('response_timeout_minutes',20))*60
        stable_seconds=float(self.gcfg.get('response_stable_seconds',8))
        previous='';stable_since=start;seen_start=False
        while time.monotonic()<deadline:
            snapshot=self._response_snapshot()
            text=self._fresh_response(snapshot,self._sent_baseline)
            busy=self._stop_visible()
            seen_start=(seen_start or busy or bool(text)
                        or snapshot['user_count']>self._sent_baseline['user_count']
                        or snapshot['model_count']>self._sent_baseline['model_count'])
            now=time.monotonic()
            if text:
                self._last_observed_response=text
                if text!=previous:previous=text;stable_since=now
                elif not busy and now-stable_since>=stable_seconds:
                    self._check_errors(text)
                    return text
            if not seen_start and now>=start_deadline:
                self._capture_diagnostic('response_not_started',error='No new response or stop control')
                raise GeminiError('没有观察到本次新的Gemini回复；已保存诊断，拒绝使用旧文本')
            self.page.wait_for_timeout(1000)
        self._capture_diagnostic('response_timeout',response=self._last_observed_response,error='Incomplete response')
        raise GeminiError('Gemini回复未在时限内完成；已保存部分文本，未将其视为完整脚本')

    def _check_errors(self,text):
        chrome_markers=('New chat\nSearch chats','Search chats\nImages','Where should we start?')
        if any(marker in text for marker in chrome_markers):
            raise GeminiError('抓取到Gemini页面导航而非模型回复，已拒绝作为脚本')
        if '我只是一个语言模型，理解不了这个问题，因此没法帮上忙' in text:
            raise GeminiError('Gemini明确回复无法理解本次请求；已保存原始平台回复')
        for pattern in _selectors(self.gcfg.get('error_patterns')):
            if pattern.lower() in text.lower():raise GeminiError(f'Gemini返回平台错误：{pattern}')

    # ---- 组合动作 ----
    def generate(self,video_paths,message_text):
        requested_count=_explicit_script_count(message_text)
        targets=_requested_durations(message_text,requested_count)
        message_text+=('\n\n【自动化脚本输出规范】用户本次附加提示词中的数量和总时长优先。'
                       '完整保留指定的50秒、70秒等总时长，不因豆包单次15秒限制而压缩。'
                       '每个脚本使用独立标题Script 01：标题（总时长N秒），正文单列“总时长：N秒”。'
                       '时间轴从00:00开始连续覆盖到总时长，按每5秒一个时间块，最后不足5秒按实际写；'
                       '每块分别列摄影与构图、视觉与动作、照明与氛围、音频与口播。'
                       '脚本独立完整，禁止省略、同上、合并或缩写为15秒。')
        self._sent_baseline=None;self._last_observed_response=''
        try:
            self.open()
            if video_paths:self.upload_files(video_paths)
            self.send_prompt(message_text)
            response=self.wait_response()
            self._last_observed_response=response
            if requested_count is not None:
                from .script_splitter import parse_scripts
                actual_count=len(parse_scripts(response))
                if actual_count!=requested_count:
                    raise GeminiError(f'Gemini脚本数量不符：提示词要求{requested_count}个，实际{actual_count}个，未发往豆包')
            if targets:
                from .script_splitter import parse_scripts,detect_total_seconds,build_segments,parse_timestamp_blocks
                try:
                    scripts=parse_scripts(response)
                    actual=[detect_total_seconds(script) for script in scripts]
                    if actual!=targets:
                        raise GeminiError(f'Gemini脚本总时长不符：期望{targets}秒，实际{actual}秒，未发往豆包')
                    for script in scripts:
                        if not parse_timestamp_blocks(script):raise ValueError('缺少覆盖完整时长的时间戳')
                        build_segments(script,self.cfg)
                except ValueError as exc:raise GeminiError('Gemini时间轴不完整：'+str(exc)) from exc
            self._capture_diagnostic('completed',response=response)
            return response
        except Exception as exc:
            self._capture_diagnostic('failed',response=self._last_observed_response,error=str(exc))
            raise

    # ---- 实机勘察 ----
    INSPECT_JS = """() => {
      const r = {fileInputs: [], editables: [], textareas: [], buttons: []};
      document.querySelectorAll('input[type=file]').forEach(e => r.fileInputs.push(
        {accept: e.accept, multiple: e.multiple, cls: e.className, id: e.id, aria: e.getAttribute('aria-label')}));
      document.querySelectorAll('[contenteditable="true"], [role=textbox]').forEach(e => r.editables.push(
        {tag: e.tagName, cls: (e.className||'').toString().slice(0,120), role: e.getAttribute('role'),
         aria: e.getAttribute('aria-label'),
         placeholder: e.getAttribute('placeholder') || e.getAttribute('data-placeholder')}));
      document.querySelectorAll('textarea').forEach(e => r.textareas.push(
        {id: e.id, cls: (e.className||'').toString().slice(0,120), aria: e.getAttribute('aria-label'),
         placeholder: e.getAttribute('placeholder')}));
      document.querySelectorAll('button, [role=button]').forEach(e => {
        const a = e.getAttribute('aria-label') || (e.textContent||'').trim().slice(0,40);
        if (a) r.buttons.push({label: a.slice(0,80), cls: (e.className||'').toString().slice(0,100)});
      });
      return r;
    }"""

    def inspect_dump(self, out_dir):
        page = self.page
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(6000)  # Gemini SPA 渲染慢，多等一会
        page.screenshot(path=str(out_dir / "gemini_page.png"), full_page=False)
        lines = [f"URL: {page.url}"]
        for idx, frame in enumerate(page.frames):
            try:
                info = frame.evaluate(self.INSPECT_JS)
            except Exception as e:
                lines.append(f"\n#### frame[{idx}] {frame.url[:120]} → 读取失败: {e}")
                continue
            lines.append(f"\n#### frame[{idx}] {frame.url[:120]}")
            lines.append("== file inputs ==")
            for x in info["fileInputs"]:
                lines.append(str(x))
            lines.append("== contenteditable / role=textbox ==")
            for x in info["editables"][:20]:
                lines.append(str(x))
            lines.append("== textarea ==")
            for x in info["textareas"][:10]:
                lines.append(str(x))
            lines.append("== buttons (前80个) ==")
            for x in info["buttons"][:80]:
                lines.append(str(x))
        (out_dir / "gemini_dom.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"[Gemini] 勘察结果已保存: {out_dir / 'gemini_dom.txt'} 与截图 gemini_page.png")
