"""Gemini 网页版自动化：上传源视频 + 固定指令 + 提示词 → 抓取生成的多个脚本。

内置常用中英文控件定位，config.yaml gemini.selectors 可覆盖默认选择器。
实机校准：python run_pipeline.py inspect-gemini
"""
import sys
import time
import re
import json
import hashlib
import mimetypes
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


class GeminiSubmissionUncertain(GeminiError):
    """A send was attempted; automatic retries could duplicate an accepted request."""


class GeminiResponseRejected(GeminiSubmissionUncertain):
    """Invalid output is not replayable until complete-turn evidence confirms it."""

    def __init__(self,*args):
        super().__init__(*args)
        self.can_retry_same_request=False


class GeminiRefusalResponse(GeminiResponseRejected):
    """An explicit completed refusal; only a specific caller policy may retry it."""


def is_refusal_response(text):
    """Recognize a standalone refusal without treating scripted dialogue as one."""
    from .script_splitter import SCRIPT_HEADER_RE, TS_BLOCK_RE
    text = (text or '').strip()
    if SCRIPT_HEADER_RE.search(text) or TS_BLOCK_RE.search(text):
        return False
    if re.search(
        r'^(?:(?:抱歉|请恕我爱莫能助)[，,。\s]*)?(?:由于[^。！？\n]{0,40}(?:局限|限制)[，,\s]*)?'
        r'我(?:目前|暂时)?(?:没法|无法|不能)(?:办到|完成|帮助|满足|处理|生成|提供)', text):
        return True
    # Match a model describing its own inability, not any occurrence of a
    # negative word in a positive response or a quoted example.
    introduction=re.match(
        r'^(?:(?:很?抱歉|对不起|请恕(?:我)?(?:爱莫能助)?)[，,。！？!\s]*)*'
        r'(?:作为|身为|我(?:只是|是|作为|身为))\s*(?:一(?:个|种)\s*)?'
        r'(?:(?:大(?:型)?)?语言模型|AI\s*(?:助手|模型)?|人工智能(?:助手|模型)?)(?:的我)?',
        text,re.I)
    if not introduction:
        return False
    clause=re.split(r'[。！？!?\n]',text[introduction.end():],1)[0].strip('，,；;：: ')
    if re.match(r'^(?:我\s*)?(?:可以|能够|能|愿意|乐意|会)',clause):
        return False
    if re.search(r'(?:超出|不在|不属于)[^。！？\n]{0,12}(?:设计(?:用途|范围)|能力范围)',clause):
        return True
    refusal=re.search(
        r'(?:^|[，,；;：:])\s*(?:(?:因此|所以|但|不过|很抱歉|抱歉|对不起)\s*)?'
        r'(?:我\s*)?(?:(?:目前|暂时|确实|实在)\s*)?'
        r'(?:(?:不能|无法|没(?:有)?办法|没法)(?P<bridge>[^，,；;。！？\n]{0,16}?)'
        r'(?:提供|帮助|帮上忙|回复|理解|完成|办到)(?!的|时)'
        r'|理解不了|爱莫能助(?:[，,；;\s]*$))',clause)
    if not refusal:
        return False
    # An accuracy disclaimer or an inability to judge someone else's action is
    # not a completed refusal to perform the requested work.
    return not re.search(r'保证|确保|承诺|判断|确定|预测',refusal.group('bridge') or '')


_PROMPT_EDITOR = '[contenteditable="true"][role="textbox"], textarea'
_UPLOAD_OPENERS = [
    'button[aria-label="上传和工具"]',
    'button[aria-label="Upload & tools"]',
    'button[aria-label="Add files"]',
    'button[aria-label="添加文件"]',
]
_SEND_BUTTONS = [
    'button[aria-label="发送"]', 'button[aria-label="Send"]',
    'button[aria-label="发送消息"]', 'button[aria-label="Send message"]',
]


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
        durations=_requested_durations(section)
        if values=={1} and len(durations)==2:return 2  # one of each duration, not one script total
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
        pair=re.search(r'(?:生成|输出|写|制作|创作)[^。；\n]{0,24}?(?:1|一)(?:个|条)\s*(\d+)\s*秒(?:的)?(?:视频|脚本)?\s*(?:和|与|、|以及)\s*(?:1|一)(?:个|条)\s*(\d+)\s*秒',section)
        if pair:
            if requested_count in (None,2):return [int(pair.group(1)),int(pair.group(2))]
            continue
        repeated=re.findall(r'(?:生成|输出|写|制作|创作)\s*(?:再)?\s*(\d{1,3}|[一二两三四五六七八九十])(?:个|条|篇|份)\s*(\d+)\s*秒',section)
        repeated={(int(count) if count.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[count],int(seconds)) for count,seconds in repeated}
        if repeated:
            if len(repeated)==1:
                count,seconds=repeated.pop()
                if 0<count<=100 and requested_count in (None,count):return [seconds]*count
            return []
        # Duration may follow a description of the requested product/script,
        # not just the count: "写一个该产品功能场景应用的15秒...脚本".
        # Keep it in the output-request clause and before its first script
        # noun, so reference footage or a per-segment tool limit is not a total.
        natural = []
        pattern = (r'(?:生成|输出|写|制作|创作)\s*(?:再)?\s*'
                   r'(\d{1,3}|[一二两三四五六七八九十])(?:个|条|篇|份)\s*'
                   r'([^，。；;\n]{0,48}?)(?<![\d.])(\d+)\s*秒'
                   r'([^，。；;\n]{0,48}?脚本)')
        for count, before, seconds, after in re.findall(pattern, section):
            if ('脚本' in before or re.search(r'(?:每段|单次|分段|豆包)', before)
                    or re.search(r'(?:源|原|参考)视频.{0,3}$', before)
                    or re.match(r'\s*(?:的)?(?:源|原|参考)视频', after)):
                continue
            count = int(count) if count.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[count]
            natural.append((count, int(seconds)))
        values = set(natural)
        if values:
            if len(values) == 1:
                count, seconds = values.pop()
                if 0 < count <= 100 and requested_count in (None, count):
                    return [seconds] * count
            return []
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

def format_gemini_request(message):
    from .caption_context import script_instruction

    instruction = script_instruction(message)
    count = _explicit_script_count(instruction)
    targets = _requested_durations(instruction, count)
    constraints = ''
    if count is not None:
        constraints += f'本次最终只输出{count}个脚本。'
    if targets:
        constraints += '本次各脚本总时长按顺序为' + '、'.join(f'{value}秒' for value in targets) + '。'
    full = (message + '\n\n【自动化脚本输出规范】本次附加提示词优先于固定指令；发生冲突时忽略固定指令的旧数量和旧时长。'
            + constraints +
            '以下约束是本次最终要求，禁止额外输出其他数量或时长的版本。'
            '严格保留本次指定的总时长，不因豆包单次15秒限制而压缩。'
            '每个脚本使用独立标题Script 01：标题（总时长N秒），正文单列“总时长：N秒”。'
            '时间轴从00:00开始连续覆盖到总时长，按每5秒一个时间块，最后不足5秒按实际写；'
            '每块分别列摄影与构图、视觉与动作、照明与氛围、音频与口播。'
            '脚本独立完整，禁止省略、同上、合并或缩写为15秒。')
    return full, count, targets


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
        self._submission_attempted = False
        self._external_submission_accepted = False
        self._expected_prompt = ''
        self._expected_attachment_count = 0
        self._last_refusal_context = None

    def _capture_diagnostic(self,stage,response='',error=''):
        """Persist failures locally; never export cookies, storage or authentication query strings."""
        prefix=datetime.now().strftime('gemini_%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]+'_'+stage
        raw=response or self._last_observed_response
        baseline=self._sent_baseline
        safe_baseline=({key:baseline.get(key) for key in ('model_count','user_count','selector')}
                       if isinstance(baseline,dict) else baseline)
        if isinstance(safe_baseline,dict) and isinstance(baseline,dict):
            safe_baseline['user_text_hashes']=[hashlib.sha256(str(text).encode('utf-8')).hexdigest()
                                               for text in baseline.get('user_texts',[]) or []]
        payload=dict(stage=stage,error=str(error),response_chars=len(raw),
                     response_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                     baseline=safe_baseline)
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
        names = get(self.cfg, "login.gemini_logged_in_cookies") or ["__Secure-1PSID", "__Secure-3PSID", "SID"]
        if isinstance(names, str):
            names = [names]
        return self.session.has_any_cookie("https://gemini.google.com", names)

    def _login_pages(self):
        context = getattr(self.session, 'context', None)
        pages = list(context.pages) if context is not None else [self.page]
        return [page for page in pages if not page.is_closed()]

    def login_ready(self) -> bool:
        """A Google cookie alone or the anonymous Gemini editor is insufficient."""
        editor = _combine(self.sel.get('prompt_editor')) or _PROMPT_EDITOR
        for page in self._login_pages():
            try:
                url = urlsplit(page.url)
                if url.scheme not in ('http', 'https') or url.hostname != 'gemini.google.com':
                    continue
                if any(part in url.path.lower() for part in ('signin', 'login')):
                    continue
                if not self.logged_in():
                    continue
                boxes = page.locator(editor)
                if any(boxes.nth(i).is_visible() and boxes.nth(i).is_enabled() for i in range(boxes.count())):
                    self.session.page = page
                    return True
            except Exception:
                # Navigation can replace the document while login is completing.
                continue
        return False

    def wait_logged_in(self):
        """Keep the visible browser usable while the user signs in; never send."""
        headless = bool(get(self.cfg, 'browser.headless', False))
        timeout = (float(get(self.cfg, 'gemini.startup_timeout_seconds', 30)) if headless
                   else float(get(self.cfg, 'login.timeout_minutes', 30)) * 60)
        deadline = time.monotonic() + max(0, timeout)
        announced = False
        while True:
            pages = self._login_pages()
            if not pages:
                raise GeminiNotLoggedIn('Gemini 登录窗口已关闭，尚未发送任何生成请求；请重新开启运行并完成 Google 登录')
            if self.login_ready():
                if announced:
                    print('[scripts] Gemini 登录与输入框已就绪，继续处理素材', flush=True)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeminiNotLoggedIn(f'等待 Gemini 登录或页面就绪超时（{timeout:g} 秒），未发送任何生成请求；请在有界面模式登录 Google 后重试')
            if not announced:
                message = ('无界面模式下等待 Gemini 页面就绪；如未登录，请先用有界面模式完成 Google 登录'
                           if headless else '请在打开的 Gemini 窗口完成 Google 登录；窗口会保留，登录就绪后自动继续。协议或条款请自行确认')
                print(f'[scripts] {message}（最多等待 {timeout:g} 秒）', flush=True)
                announced = True
            # Keep Playwright's event loop alive, including redirects and popups.
            pages[0].wait_for_timeout(min(1, remaining) * 1000)

    def open(self):
        page = self.page
        page.goto(self.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cur = page.url
        if "accounts.google.com" in cur or "signin" in cur.lower():
            raise GeminiNotLoggedIn("Gemini 未登录：请先运行 python run_login.py 在弹出的窗口中登录 Google 账号")
        editor = _combine(self.sel.get("prompt_editor")) or _PROMPT_EDITOR
        if editor:
            try:
                page.locator(editor).first.wait_for(state="visible", timeout=20000)
            except Exception as exc:
                self._capture_diagnostic('editor_unavailable',error=str(exc))
                raise GeminiNotLoggedIn('Gemini输入框不可用，可能需要人工登录或确认新条款；未自动点击同意/继续') from exc

    # ---- 上传 ----
    @staticmethod
    def _input_accepts(loc, paths):
        accept = (loc.get_attribute('accept') or '').lower()
        tokens = {part.strip() for part in accept.split(',') if part.strip()}
        if len(paths) > 1 and loc.get_attribute('multiple') is None:
            return False
        for path in paths:
            mime = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
            extension = Path(path).suffix.lower()
            if tokens and not any(token in ('*/*', mime, extension)
                                  or (token.endswith('/*') and mime.startswith(token[:-1]))
                                  for token in tokens):
                return False
        return True

    def _set_upload_input(self, paths):
        inputs = self.page.locator(_combine(self.sel.get('file_input')) or 'input[type="file"]')
        for index in range(inputs.count()):
            loc = inputs.nth(index)
            if self._input_accepts(loc, paths):
                # Do not try a second input after a partial/ambiguous upload.
                loc.set_input_files([str(path) for path in paths], timeout=30000)
                return True
        return False

    def _wait_uploads(self, paths):
        editor = self.page.locator(_combine(self.sel.get('prompt_editor')) or _PROMPT_EDITOR).filter(visible=True).first
        names = [Path(path).name for path in paths]
        deadline = time.monotonic() + float(self.gcfg.get('upload_timeout_seconds', 300))
        stable_seconds = max(0, float(self.gcfg.get('upload_settle_seconds', 1.5)))
        ready_since = None
        next_status = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._check_for_external_submission():
                return
            kinds = [mimetypes.guess_type(str(path))[0] or 'application/octet-stream' for path in paths]
            try:
                state = editor.evaluate('''(editor, {names, kinds, baseline}) => {
              const root = editor.closest('[data-node-type="input-area"], fieldset, form');
              if (!root) return {ready: false, error: ''};
              const visible = e => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
              const elements = [...root.querySelectorAll('*')].filter(visible);
              // Require one newly rendered preview per selected file. A filename or
              // file-input selection alone does not prove that media finished loading.
              const previews = elements.filter(e => e.matches('uploader-file-preview'));
              const newPreviews = previews.slice(baseline);
              const expectedVideos = kinds.filter(kind => kind.startsWith('video/')).length;
              const expectedImages = kinds.filter(kind => kind.startsWith('image/')).length;
              const loaded = newPreviews.every(e => {
                  const image = e.querySelector('gem-media-attachment img');
                  return image && image.complete && image.naturalWidth > 0;
                });
              const videos = newPreviews.filter(e => e.querySelector('gem-media-attachment .time-overlay')).length;
              const images = newPreviews.length - videos;
              const mediaReady = previews.length - baseline === names.length && loaded &&
                videos === expectedVideos && images === expectedImages;
              const busy = elements.some(e => e.matches('[role="progressbar"], mat-progress-spinner, mat-spinner, [aria-busy="true"], .gem-attachment-content.loading, .loading'));
              const error = elements.filter(e => e.matches('[role="alert"]')).map(e => e.textContent.trim()).join(' ');
              return {ready: mediaReady && !busy && !error, error, previewCount: previews.length,
                      newCount: newPreviews.length, busy};
                }''', dict(names=names,
                       kinds=kinds,
                       baseline=getattr(self, '_upload_preview_baseline', 0)))
            except Exception:
                if self._check_for_external_submission():
                    return
                raise
            if state['error']:
                if self._check_for_external_submission():
                    return
                raise GeminiError('Gemini 附件上传报错：' + state['error'])
            now=time.monotonic()
            if now >= next_status:
                categories={}
                for kind in kinds:
                    category='视频' if kind.startswith('video/') else '图片' if kind.startswith('image/') else '其他'
                    categories[category]=categories.get(category,0)+1
                summary='、'.join(f'{category}{count}个' for category,count in categories.items())
                print(f"[Gemini] 上传仍在等待：{summary}；新预览{state['newCount']}/{len(paths)}，可见预览总数{state['previewCount']}，忙碌={state['busy']}",flush=True)
                next_status=now+10
            if state['ready']:
                if ready_since is None:
                    ready_since = time.monotonic()
                if time.monotonic() - ready_since >= stable_seconds:
                    print(f'[Gemini] 已确认 {len(paths)} 个附件可见且上传进度已结束', flush=True)
                    return
            else:
                ready_since = None
            self.page.wait_for_timeout(500)
        raise GeminiError('Gemini 附件未在时限内确认上传完成；未发送提示词，请检查本地诊断')

    def upload_files(self, paths: list):
        """Wait for hydrated controls, choose a media-compatible input, then prove attachment readiness."""
        if not paths:
            return
        paths = [Path(path) for path in paths]
        if any(not path.is_file() for path in paths):
            raise GeminiError('Gemini 待上传文件不存在，未提交上传或提示词')
        page = self.page
        editor = page.locator(_combine(self.sel.get('prompt_editor')) or _PROMPT_EDITOR).filter(visible=True).first
        self._upload_preview_baseline = editor.evaluate('''editor => {
          const root = editor.closest('[data-node-type="input-area"], fieldset, form');
          return root ? [...root.querySelectorAll('uploader-file-preview')].filter(e => e.getClientRects().length).length : 0;
        }''')
        if self._upload_preview_baseline:
            self._submission_attempted=True
            raise GeminiSubmissionUncertain('Gemini 输入区已有未识别附件；为避免重复提交或误发旧素材，已保留草稿并停止本次处理')
        openers = _selectors(self.sel.get('open_upload')) or _UPLOAD_OPENERS
        deadline = time.monotonic() + float(self.gcfg.get('upload_control_timeout_seconds', 30))
        ok = False
        while time.monotonic() < deadline:
            if self._set_upload_input(paths):
                ok = True
                break
            buttons = page.locator(_combine(openers)).filter(visible=True)
            if buttons.count() and buttons.first.is_enabled():
                chooser = None
                try:
                    with page.expect_file_chooser(timeout=1500) as event:
                        buttons.first.click(timeout=5000)
                    chooser = event.value
                except Exception:
                    # The normal current UI opens a menu, not a system dialog.
                    pass
                if chooser is not None:
                    if not self._input_accepts(chooser.element, paths):
                        raise GeminiError('上传入口不接受当前视频类型，未选择文件')
                    chooser.set_files([str(path) for path in paths])
                    ok = True
                elif self._set_upload_input(paths):
                    ok = True
                else:
                    labels = _selectors(self.sel.get('upload_menu_items')) or ['上传文件', 'Upload files']
                    for label in labels:
                        items = page.get_by_role('menuitem', name=re.compile(r'^' + re.escape(label) + r'(?:[.。]|$)')).filter(visible=True)
                        if not items.count():
                            continue
                        with page.expect_file_chooser(timeout=8000) as event:
                            items.first.click(timeout=5000)
                        chooser = event.value
                        if not self._input_accepts(chooser.element, paths):
                            raise GeminiError('上传菜单不接受当前视频类型，未选择文件')
                        chooser.set_files([str(path) for path in paths])
                        ok = True
                        break
                break
            page.wait_for_timeout(250)
        if not ok:
            raise GeminiError('无法找到可用的 Gemini 视频上传入口，请运行 inspect-gemini 检查页面')
        page.keyboard.press('Escape')
        print(f'[Gemini] 已选择 {len(paths)} 个文件，等待实际附件上传完成…', flush=True)
        self._wait_uploads(paths)

    # ---- 发送与等待 ----
    def type_prompt(self,text):
        editor=_combine(self.sel.get('prompt_editor')) or _PROMPT_EDITOR
        matches=self.page.locator(editor)
        loc=next((matches.nth(i) for i in range(matches.count()) if matches.nth(i).is_visible()), matches.first)
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
        try:
            user_loc=self.page.locator('user-query, [data-message-author-role="user"]')
            users=user_loc.count()
            user_texts=[]
            user_attachment_counts=[]
            for i in range(users):
                turn=user_loc.nth(i)
                user_attachment_counts.append(turn.locator('user-query-file-preview').count())
                lines=turn.locator('.query-text .query-text-line, query-text .query-text-line')
                if lines.count():
                    user_texts.append('\n'.join(lines.nth(j).inner_text(timeout=2000)
                                                 for j in range(lines.count())))
                else:
                    body=turn.locator('.query-text, query-text')
                    user_texts.append(body.first.evaluate('''e => {
                      const copy=e.cloneNode(true);
                      copy.querySelectorAll('.screen-reader-user-query-label, h5, button, [role="button"], mat-card, gem-media-attachment').forEach(n=>n.remove());
                      return (copy.innerText || copy.textContent || '').trim();
                    }''') if body.count() else turn.inner_text(timeout=2000))
        except Exception:
            users=0;user_texts=None;user_attachment_counts=None
        return dict(model_count=len(texts),user_count=users,user_texts=user_texts,
                    user_attachment_counts=user_attachment_counts,selector=selected,
                    texts=texts,response_hashes=[hashlib.sha256(t.encode('utf-8')).hexdigest() for t in texts])

    @staticmethod
    def _normalized_prompt(text):
        return ' '.join((text or '').split())

    def _draft_text(self, *, require_editor=False):
        try:
            selector=_combine(self.sel.get('prompt_editor')) or _PROMPT_EDITOR
            editor=self.page.locator(selector).filter(visible=True).first
            return editor.evaluate("e => e.isContentEditable ? e.innerText : e.value || e.textContent || ''")
        except Exception:
            if require_editor:
                raise GeminiSubmissionUncertain('Gemini 输入区无法校验，已停止原会话重发') from None
            return ''

    def _check_for_external_submission(self):
        """Adopt the one matching user turn; stop if a send may have happened ambiguously."""
        if self._sent_baseline is None or not self._expected_prompt:
            return False
        snapshot=self._response_snapshot()
        if snapshot.get('user_texts') is None:
            self._submission_attempted=True
            raise GeminiSubmissionUncertain('无法读取 Gemini 用户消息以确认提交状态；已停止发送和自动重试')
        delta=max(0,snapshot['user_count']-self._sent_baseline['user_count'])
        if delta:
            baseline_texts=self._sent_baseline.get('user_texts',[])
            current_texts=snapshot.get('user_texts')
            if baseline_texts is None or current_texts is None:
                self._submission_attempted=True
                raise GeminiSubmissionUncertain('无法读取 Gemini 会话中的用户消息以核对提交内容；已停止自动重试')
            new_texts=current_texts[len(baseline_texts):]
            expected=self._normalized_prompt(self._expected_prompt)
            matches=[text for text in new_texts if self._normalized_prompt(text)==expected]
            self._submission_attempted=True
            if delta==1 and len(new_texts)==1 and len(matches)==1:
                if self._expected_attachment_count:
                    counts=snapshot.get('user_attachment_counts')
                    if (counts is None or len(counts)!=snapshot['user_count']
                            or counts[-1]!=self._expected_attachment_count):
                        raise GeminiSubmissionUncertain('已发现匹配的手动消息，但无法确认包含本次全部附件；已停止发送和自动重试')
                self._external_submission_accepted=True
                print('[Gemini] 检测到提示词已由用户提交，接管本次回复并停止重复发送',flush=True)
                return True
            raise GeminiSubmissionUncertain('Gemini 对话出现新的用户消息，但无法唯一匹配当前提示词；已停止发送和自动重试')
        draft=self._normalized_prompt(self._draft_text())
        if draft and draft != self._normalized_prompt(self._expected_prompt):
            self._submission_attempted=True
            raise GeminiSubmissionUncertain('Gemini 草稿在等待期间发生变化，无法确认是否已手动提交；已停止重复发送和自动重试')
        if not draft:
            self._submission_attempted=True
            raise GeminiSubmissionUncertain('Gemini 草稿已消失但对话中尚无可匹配消息；提交状态不确定，已停止自动重试')
        return False

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
        if self._check_for_external_submission():
            return
        if self._sent_baseline is None:
            self._sent_baseline=self._response_snapshot()
        if self._normalized_prompt(self._draft_text()) != self._normalized_prompt(text):
            self.type_prompt(text)
        if self._check_for_external_submission():
            return
        sent=False
        buttons=_selectors(self.sel.get('send_button')) or _SEND_BUTTONS
        ready_deadline=time.monotonic()+float(self.gcfg.get('send_ready_timeout_seconds',180))
        while time.monotonic()<ready_deadline:
            if self._check_for_external_submission():
                return
            for selector in buttons:
                try:
                    matches=self.page.locator(selector)
                    for index in range(matches.count()):
                        button=matches.nth(index)
                        if not button.is_visible():continue
                        if not button.is_enabled():continue
                        if self._check_for_external_submission():
                            return
                        # A click timeout can happen after the request was accepted.
                        # Attempt once, then confirm via the conversation instead of resending.
                        sent=True
                        self._submission_attempted=True
                        try:button.click(timeout=5000)
                        except Exception:pass
                        break
                except Exception:continue
                if sent:break
            if sent:break
            self.page.wait_for_timeout(500)
        else:
            if self._check_for_external_submission():
                return
            self._capture_diagnostic('send_not_ready',error='Send control missing or disabled after upload/draft')
            raise GeminiError('上传或发送按钮迟迟未就绪；未用回车绕过缺失或禁用按钮，已保存诊断')
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
        refusal = is_refusal_response(text)
        if refusal and '我只是一个语言模型，理解不了这个问题，因此没法帮上忙' in text:
            raise GeminiRefusalResponse('Gemini明确回复无法理解本次请求；未发往豆包，已保存拒答诊断')
        if refusal:
            raise GeminiRefusalResponse('Gemini明确拒绝或无法完成本次请求；未发往豆包，已保存拒答诊断')
        for pattern in _selectors(self.gcfg.get('error_patterns')):
            if pattern.lower() in text.lower():raise GeminiError(f'Gemini返回平台错误：{pattern}')

    @staticmethod
    def _input_fingerprint(paths):
        """Bind a refusal retry to the actual files originally selected."""
        identities=[]
        try:
            for value in paths:
                path=Path(value).expanduser().resolve()
                before=path.stat()
                if not path.is_file():
                    return None
                digest=hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda:handle.read(1 << 20),b''):
                        digest.update(chunk)
                after=path.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    return None
                identities.append((str(path),after.st_size,digest.hexdigest()))
        except (OSError,ValueError,TypeError,RuntimeError):
            return None
        return tuple(identities)

    @staticmethod
    def _turn_fingerprint(snapshot):
        """Private in-memory turn identity; raw prompts are not diagnostic fields."""
        try:
            users=snapshot['user_count'];models=snapshot['model_count']
            user_texts=snapshot['user_texts'];texts=snapshot['texts']
            attachments=snapshot['user_attachment_counts']
            if (not isinstance(users,int) or not isinstance(models,int)
                    or users<0 or models<0 or len(user_texts)!=users
                    or len(texts)!=models or len(attachments)!=users):
                return None
            digest=lambda value:hashlib.sha256(str(value).encode('utf-8')).hexdigest()
            return (users,models,tuple(digest(' '.join(text.split())) for text in user_texts),
                    tuple(digest(text) for text in texts),tuple(attachments))
        except (KeyError,TypeError,AttributeError):
            return None

    def _remember_refusal(self,paths,original_message,full_prompt,inputs):
        """Retain only a complete accepted invalid reply, regardless of its wording."""
        self._last_refusal_context=None
        try:
            url=self.page.url;parsed=urlsplit(url)
            snapshot=self._response_snapshot();fingerprint=self._turn_fingerprint(snapshot)
            baseline=self._sent_baseline
            if (inputs is None or not self._submission_attempted or not baseline or not fingerprint
                    or parsed.scheme!='https' or parsed.hostname!='gemini.google.com'
                    or self._stop_visible() or self._draft_text(require_editor=True).strip()
                    or snapshot['user_count']!=baseline['user_count']+1
                    or snapshot['model_count']!=baseline['model_count']+1
                    or not snapshot['user_texts']
                    or self._normalized_prompt(snapshot['user_texts'][-1])!=self._normalized_prompt(full_prompt)
                    or snapshot['user_attachment_counts'][-1]!=len(paths)
                    or self._fresh_response(snapshot,baseline)!=self._last_observed_response
                    or not self._last_observed_response.strip()):
                return
            self._last_refusal_context=dict(page=self.page,url=url,turn=fingerprint,
                paths=tuple(str(path) for path in paths),inputs=inputs,
                original_message=original_message,full_prompt=full_prompt)
        except Exception:
            # Output classification remains truthful even if retry evidence
            # cannot be captured. The retry entry will stop without sending.
            return

    def _verify_refusal_retry(self,paths,message,full_prompt,inputs,context):
        if (tuple(str(path) for path in paths)!=context['paths']
                or message!=context['original_message'] or full_prompt!=context['full_prompt']):
            raise GeminiSubmissionUncertain('原指令或附件路径已变化，已停止原会话重发')
        if inputs is None or inputs!=context['inputs']:
            raise GeminiSubmissionUncertain('原附件内容已变化或无法校验，已停止原会话重发')
        if self.page is not context['page'] or self.page.url!=context['url']:
            raise GeminiSubmissionUncertain('Gemini 会话已变化，已停止原会话重发')
        if self._stop_visible() or self._draft_text(require_editor=True).strip():
            raise GeminiSubmissionUncertain('Gemini 正在回复或已有新草稿，已停止原会话重发')
        snapshot=self._response_snapshot()
        if self._turn_fingerprint(snapshot)!=context['turn']:
            raise GeminiSubmissionUncertain('Gemini 对话已有新消息或发生变化，已停止重复发送')
        return snapshot

    def retry_last_rejected_response(self,video_paths,message_text):
        """Replay identical inputs in the unchanged chat, under a caller's retry budget."""
        context=self._last_refusal_context
        self._last_refusal_context=None  # Each complete invalid reply grants one attempt.
        if context is None:
            raise GeminiSubmissionUncertain('缺少可校验的完整回复记录，不能重发原请求')
        return self.generate(video_paths,message_text,_refusal_context=context)

    def retry_last_refusal(self,video_paths,message_text):
        """Compatibility alias for the former refusal-only entry."""
        return self.retry_last_rejected_response(video_paths,message_text)

    # ---- 组合动作 ----
    def generate(self,video_paths,message_text,*,_refusal_context=None):
        original_message=message_text
        video_paths=tuple(video_paths or ())
        same_inputs=(_refusal_context is None or
                     (tuple(str(path) for path in video_paths)==_refusal_context['paths']
                      and message_text==_refusal_context['original_message']))
        inputs=self._input_fingerprint(video_paths) if same_inputs else None
        self._last_refusal_context=None
        message_text,requested_count,targets=format_gemini_request(message_text)
        self._sent_baseline=None;self._last_observed_response=''
        self._submission_attempted=False
        self._external_submission_accepted=False
        self._expected_prompt=message_text
        self._expected_attachment_count=len(video_paths or [])
        try:
            if _refusal_context is None:
                self.open()
                self._sent_baseline=self._response_snapshot()
            else:
                # Freeze the validated baseline before touching the composer.
                # A human send after this point is still seen by upload/send
                # reconciliation, rather than becoming an invisible baseline.
                self._sent_baseline=self._verify_refusal_retry(
                    video_paths,original_message,message_text,inputs,_refusal_context)
                print('[Gemini] 已核对原会话与原素材，在同一对话重发完全相同指令',flush=True)
            self.type_prompt(message_text)
            print('[Gemini] 提示词草稿已填写，附件就绪前不会发送', flush=True)
            if video_paths:
                try:
                    self.upload_files(video_paths)
                except Exception:
                    # The editor can be detached by a manual submission at the same
                    # moment an upload wait times out. Reconcile the conversation
                    # before letting the caller classify this as retryable.
                    if not self._check_for_external_submission():
                        raise
            if not self._external_submission_accepted:
                self.send_prompt(message_text)
            response=self.wait_response()
            self._last_observed_response=response
            self._check_errors(response)
            from .script_splitter import (SCRIPT_HEADER_RE, TS_BLOCK_RE, parse_scripts,
                                          detect_total_seconds, build_segments, parse_timestamp_blocks)
            scripts=parse_scripts(response)
            if requested_count is not None:
                actual_count=len(scripts)
                if actual_count!=requested_count:
                    raise GeminiResponseRejected(f'Gemini脚本数量不符：提示词要求{requested_count}个，实际{actual_count}个，未发往豆包')
            if not SCRIPT_HEADER_RE.search(response) or not scripts:
                raise GeminiResponseRejected('Gemini回复缺少独立脚本标题或正文；未发往豆包，已保存原始回复')
            try:
                actual=[detect_total_seconds(script) for script in scripts]
                if targets and actual!=targets:
                    raise GeminiResponseRejected(f'Gemini脚本总时长不符：期望{targets}秒，实际{actual}秒，未发往豆包')
                for script, total in zip(scripts, actual):
                    if total is None or total <= 0:
                        raise ValueError('缺少有效总时长')
                    blocks=parse_timestamp_blocks(script)
                    if not blocks:
                        raise ValueError('缺少覆盖完整时长的时间戳')
                    for block in blocks:
                        content=[]
                        for line in block['lines']:
                            timestamp=TS_BLOCK_RE.match(line)
                            line=timestamp.group(5).strip() if timestamp else line.strip()
                            if not line:
                                continue
                            # Labels without any value do not make a script.
                            value=line.split('：',1)[-1] if '：' in line else line.split(':',1)[-1]
                            if re.search(r'[\w\u3400-\u9fff]', value):
                                content.append(value)
                        if not content:
                            raise ValueError('时间块缺少画面或音频正文')
                    build_segments(script,self.cfg)
            except ValueError as exc:
                raise GeminiResponseRejected('Gemini时间轴不完整：'+str(exc)+'；未发往豆包，已保存原始回复') from exc
            self._capture_diagnostic('completed',response=response)
            return response
        except Exception as exc:
            self._capture_diagnostic('failed',response=self._last_observed_response,error=str(exc))
            if isinstance(exc, GeminiResponseRejected):
                self._remember_refusal(video_paths,original_message,message_text,inputs)
                exc.can_retry_same_request=self._last_refusal_context is not None
                raise
            if self._submission_attempted:
                raise GeminiSubmissionUncertain(f'{exc}；已尝试发送，本次停止自动重试，请先核对 Gemini 对话与本地诊断') from exc
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
