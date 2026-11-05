"""豆包对话框执行代理：新建对话 → 上传图片 → 输入脚本提示词 → 等待生成 → 下载视频。

异常语义：
  NeedsLogin      登录失效 → 账号标记为需重新登录，任务回队
  QuotaExhausted  免费额度耗尽 → 账号标记为耗尽，任务回队并自动切换账号
  GenerationFailed/Timeout → 任务重试（有上限）

选择器全部来自 config.yaml doubao.selectors；实机校准：
  python run_pipeline.py inspect-doubao --account doubao1
"""
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import get
from .doubao_batch import BatchGenerationMixin, quota_exhaustion_matches


class NeedsLogin(Exception):
    pass


class QuotaExhausted(Exception):
    pass


class GenerationFailed(Exception):
    pass


class GenerationTimeout(GenerationFailed):
    pass


class NeedsUserAction(GenerationFailed):
    pass


def _combine(selectors) -> str:
    if isinstance(selectors, str):
        return selectors
    return ", ".join(s for s in (selectors or []) if s)


# Conservative language templates, not a claim about current platform wording.
# Only explicit daily video-generation exhaustion qualifies; ambiguous failures do not.
DEFAULT_QUOTA_EXHAUSTED_PATTERNS = [
    r'(?:今日|今天|当日)(?:的)?(?:免费)?视频(?:生成)?(?:次数|额度)(?:已|已经)(?:用完|耗尽)',
    r'(?:daily|today\x27s)\s+(?:free\s+)?video\s+generation\s+(?:quota|limit)\s+(?:has\s+been\s+)?(?:exhausted|reached)',
    r'no\s+(?:daily\s+)?(?:free\s+)?video\s+generations\s+(?:are\s+)?remaining',
]

DEFAULT_FAILURE_PATTERNS = ["视频生成失败", "生成失败", "内容不符合", "无法生成", "出错了"]


class DoubaoAgent(BatchGenerationMixin):
    def __init__(self, session, cfg: dict, account_name: str):
        self.session = session
        self.cfg = cfg
        self.name = account_name
        self.dcfg = cfg.get("doubao", {}) or {}
        self.sel = self.dcfg.get("selectors", {}) or {}
        self.chat_url = self.dcfg.get("chat_url", "https://www.doubao.com/chat/")
        self.quota_patterns = [re.compile(p, re.IGNORECASE) for p in (self.dcfg.get("quota_exhausted_patterns", DEFAULT_QUOTA_EXHAUSTED_PATTERNS) or [])]
        self.login_patterns = [re.compile(p) for p in (self.dcfg.get("login_required_patterns") or [])]
        self.failure_patterns = [re.compile(p) for p in (self.dcfg.get("failure_patterns") or DEFAULT_FAILURE_PATTERNS)]
        self.confirm_patterns = [re.compile(p) for p in (self.dcfg.get("confirm_patterns") or [])]
        self.confirm_reply = self.dcfg.get("confirm_reply", "确认，请直接开始生成视频。")

    @property
    def page(self):
        return self.session.page

    # ---- 模态对话框处理 ----
    def _handle_dialogs(self):
        """处理模态对话框：新协议交给用户、保留生成进度，其余关闭/Escape。
        每次处理都打印对话框文本，便于后续针对性优化。"""
        page = self.page
        for _ in range(4):
            try:
                dlg = page.locator('div[role="dialog"][data-state="open"]')
                if dlg.count() == 0:
                    return
                txt = " ".join(dlg.first.inner_text(timeout=2000).split())[:300]
            except Exception:
                return
            print(f"[{self.name}] 检测到模态对话框: {txt!r}")
            acted = False
            # 新法律条款需要用户在浏览器中自行确认。
            if any(k in txt for k in ("协议", "条款", "阅读并同意")):
                raise NeedsUserAction('豆包出现新的协议或条款，需要用户在窗口中确认后继续')
            # ② 生成进度类 → 不关闭（关了可能取消生成）
            if not acted and any(k in txt for k in ("生成中", "排队", "进度", "正在生成", "预计")):
                print(f"[{self.name}] 对话框疑似生成进度，保持打开")
                return
            # ③ 关闭/取消类按钮
            if not acted:
                for sel in ('button[aria-label*="关闭"]', 'button[aria-label*="Close"]'):
                    try:
                        b = dlg.first.locator(sel).first
                        if b.count() > 0 and b.is_visible():
                            b.click(timeout=2000)
                            acted = True
                            break
                    except Exception:
                        continue
            if not acted:
                for label in ("我知道了", "知道了", "取消", "关闭", "暂不", "以后再说"):
                    try:
                        b = dlg.first.get_by_role("button", name=label).first
                        if b.count() > 0 and b.is_visible():
                            b.click(timeout=2000)
                            acted = True
                            break
                    except Exception:
                        continue
            if not acted:
                try:
                    page.keyboard.press("Escape")
                    acted = True
                except Exception:
                    return
            page.wait_for_timeout(1000)

    # ---- 打开与登录检测 ----
    def open_chat(self):
        page = self.page
        page.goto(self.chat_url, wait_until="domcontentloaded")
        self._handle_dialogs()
        self._assert_logged_in()
        self._wait_chat_ready(fresh=False)

    def _chat_ready(self, fresh=False):
        """A root URL alone can precede hydration of the previous conversation."""
        url = urlsplit(self.page.url)
        if url.scheme != 'https' or url.hostname not in ('www.doubao.com', 'doubao.com'):
            return False
        if fresh and url.path.rstrip('/') != '/chat':
            return False
        boxes = self.page.locator(_combine(self.sel.get('input_box')) or
                                  '[contenteditable="true"], textarea, [role="textbox"]')
        if not any(boxes.nth(i).is_visible() and boxes.nth(i).is_enabled()
                   for i in range(boxes.count())):
            return False
        if fresh:
            history = self.page.locator(
                '[data-testid="receive_message"], [data-testid="send_message"], '
                '[data-message-role="user"], [data-message-role="assistant"], '
                '[data-role="user"], [data-role="assistant"], '
                '[class*="block-video-"], [class*="fulfill-video-error-box-"]')
            if history.count():
                return False
        return True

    def _wait_chat_ready(self, fresh=False, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 20
        ready_since = None
        while time.monotonic() < deadline:
            self._assert_logged_in()
            try:
                ready = self._chat_ready(fresh=fresh)
            except Exception:
                ready = False  # SPA navigation may replace the document during a poll.
            if ready:
                ready_since = time.monotonic() if ready_since is None else ready_since
                if not fresh or time.monotonic() - ready_since >= 1:
                    return
            else:
                ready_since = None
            self.page.wait_for_timeout(100)
        detail = ('未能确认新对话为空且已就绪，已在上传和发送前停止，避免误领旧视频'
                  if fresh else '找不到可用对话输入框，请运行 inspect-doubao 校准 input_box 选择器')
        raise GenerationFailed(f'[{self.name}] {detail}')

    def _assert_logged_in(self):
        url = (self.page.url or "").lower()
        if "login" in url or "passport" in url or "sso" in url:
            raise NeedsLogin(f"[{self.name}] 被重定向到登录页: {self.page.url}")
        cookies = self.session.cookies("https://www.doubao.com")
        names = {c.get("name") for c in cookies}
        expected = set(get(self.cfg, "login.doubao_logged_in_cookies", []) or [])
        if expected and not (names & expected):
            # 没有会话 cookie 且页面出现登录字样 → 需要登录
            try:
                body_text = self.page.locator("body").inner_text(timeout=3000)[:500]
            except Exception:
                body_text = ""
            if any(p.search(body_text) for p in self.login_patterns) or "登录" in body_text[:200]:
                raise NeedsLogin(f"[{self.name}] 页面提示需要登录")

    # ---- 新对话 ----
    def new_chat(self):
        """Click the real new-chat control and prove an empty hydrated composer.

        Navigating to /chat/ can restore the previous conversation asynchronously;
        it is only a way to locate the control, never evidence of a new chat.
        """
        page = self.page
        configured = self.sel.get('new_chat') or []
        configured = [configured] if isinstance(configured, str) else configured
        deadline = time.monotonic() + float(self.dcfg.get('new_chat_timeout_seconds', 30))
        navigated = False
        while time.monotonic() < deadline:
            self._assert_logged_in()
            candidates = [page.locator(selector) for selector in configured]
            candidates.append(page.get_by_role(
                'button', name=re.compile(r'^(?:新对话|New chat)(?:$|[\s⇧⌘+])', re.I)))
            for matches in candidates:
                for i in range(matches.count()):
                    button = matches.nth(i)
                    if not button.is_visible() or not button.is_enabled():
                        continue
                    button.click(timeout=4000)
                    self._wait_chat_ready(fresh=True, deadline=deadline)
                    return
            if not navigated:
                page.goto(self.chat_url, wait_until='domcontentloaded')
                navigated = True
            page.wait_for_timeout(250)
        raise GenerationFailed(f'[{self.name}] 找不到可用的新对话按钮，已在上传和发送前停止；仅访问 /chat/ 不代表创建成功')

    # ---- 上传图片 ----
    def upload_images(self, image_paths: list):
        if not image_paths:
            return
        page = self.page
        files = [str(p) for p in image_paths]
        file_input = self.sel.get("file_input", 'input[type="file"]')
        ok = False
        # A) 直接向常驻隐藏 file input 注入（豆包页面有两个 input[type=file]）
        try:
            loc = page.locator(file_input)
            if loc.count() > 0:
                loc.first.set_input_files(files, timeout=6000)
                ok = True
        except Exception:
            pass
        # B) 点上传按钮：捕获系统文件框，或点开后注入新出现的 input
        if not ok:
            for opener in (self.sel.get("open_upload") or []):
                if ok:
                    break
                try:
                    btn = page.locator(opener).first
                    if btn.count() == 0 or not btn.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=6000) as fc:
                            btn.click(timeout=4000)
                        fc.value.set_files(files)
                        ok = True
                        break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                    loc = page.locator(file_input)
                    if loc.count() > 0:
                        loc.first.set_input_files(files, timeout=4000)
                        ok = True
                except Exception:
                    continue
        if not ok:
            raise GenerationFailed(f"[{self.name}] 无法定位图片上传入口，请运行 dryrun-doubao 校准 file_input/open_upload")
        settle = float(self.dcfg.get("upload_settle_seconds", 5)) * max(len(image_paths), 1)
        page.wait_for_timeout(int(settle * 1000))
        print(f"[{self.name}] 已上传 {len(image_paths)} 张图片")

    # ---- 输入与发送 ----
    def type_text(self, text: str):
        """只输入不发送（供 dry-run 校准用）。"""
        page = self.page
        self._handle_dialogs()
        box = _combine(self.sel.get("input_box"))
        loc = page.locator(box).first
        loc.click(timeout=8000)
        page.wait_for_timeout(200)
        page.keyboard.insert_text(text)
        page.wait_for_timeout(600)

    def send(self) -> bool:
        """点击发送按钮；找不到按钮时按 Enter 兜底。返回是否用按钮发送。"""
        page = self.page
        for sel in (self.sel.get("send_button") or []):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=4000)
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        return False

    def type_and_send(self, text: str):
        self.type_text(text)
        by_button = self.send()
        print(f"[{self.name}] 提示词已发送（{len(text)} 字，按钮发送={by_button}），等待视频生成…")

    # ---- 结果监控 ----
    def _recent_text(self) -> str:
        """取页面上最后几条消息的文本（用于匹配额度/登录/失败提示）。"""
        page = self.page
        sel = _combine(self.sel.get("message_item"))
        try:
            if sel:
                loc = page.locator(sel)
                n = loc.count()
                if n > 0:
                    parts = []
                    for i in range(max(0, n - 3), n):
                        try:
                            parts.append(loc.nth(i).inner_text(timeout=2000))
                        except Exception:
                            pass
                    if parts:
                        return "\n".join(parts)
            return page.locator("body").inner_text(timeout=4000)[-2000:]
        except Exception:
            return ""

    def _video_count(self) -> int:
        sel = self.sel.get("video_card", "video")
        try:
            return self.page.locator(sel).count()
        except Exception:
            return 0

    def _try_save_video(self, dest: Path) -> bool:
        """两种方式取回成片：① 点下载按钮触发浏览器下载；② 直接抓取 video src。"""
        page = self.session.page
        dest.parent.mkdir(parents=True, exist_ok=True)
        # ① 下载按钮
        for sel in (self.sel.get("download_button") or []):
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                # 悬停最后一个视频卡片让下载按钮浮现
                try:
                    v = page.locator(self.sel.get("video_card", "video")).last
                    if v.count() > 0:
                        v.hover(timeout=2000)
                        page.wait_for_timeout(500)
                except Exception:
                    pass
                btn = loc.last
                if not btn.is_visible():
                    continue
                with page.expect_download(timeout=30000) as dl_info:
                    btn.click(timeout=5000)
                dl = dl_info.value
                dl.save_as(str(dest))
                if dest.exists() and dest.stat().st_size > 0:
                    return True
            except Exception:
                continue
        # ② video src 直连下载（blob: 无法用此方式）
        try:
            src = page.evaluate(
                """() => {
                  const vs = [...document.querySelectorAll('video')];
                  if (!vs.length) return '';
                  const v = vs[vs.length - 1];
                  return v.currentSrc || v.src || (v.querySelector('source') ? v.querySelector('source').src : '') || '';
                }"""
            )
            if src and src.startswith("http"):
                resp = self.session.context.request.get(src, timeout=300000)
                if resp.ok:
                    dest.write_bytes(resp.body())
                    return dest.exists() and dest.stat().st_size > 0
        except Exception:
            pass
        return False

    def wait_result(self, dest: Path) -> Path:
        page = self.page
        timeout_s = float(self.dcfg.get("generation_timeout_minutes", 15)) * 60
        poll_s = float(self.dcfg.get("poll_interval_seconds", 5))
        deadline = time.time() + timeout_s
        baseline_videos = self._video_count()
        quota_hinted = False
        last_log = 0.0
        last_confirm_text = ""
        last_agree_at = 0.0
        agree_texts = list(self.dcfg.get("agree_button_texts") or ["确认生成"])
        no_resp_s = float(self.dcfg.get("no_response_minutes", 8)) * 60
        last_text_change = time.time()
        last_text_seen = ""
        ACTIVE_KEYWORDS = ("生成中", "排队", "预计", "正在生成", "准备中")

        while time.time() < deadline:
            page.wait_for_timeout(int(poll_s * 1000))
            # 登录失效
            try:
                self._assert_logged_in()
            except NeedsLogin:
                raise
            except Exception:
                pass
            text = self._recent_text()
            now = time.time()
            if text != last_text_seen:
                last_text_seen = text
                last_text_change = now
            # 及时处理新弹出的模态对话框（如首次视频生成的协议同意）
            self._handle_dialogs()
            # 条款/确认消息里的内联可点击元素（确认生成/继续生成）→ 点击开始生成（60s 冷却防重复点）
            if now - last_agree_at > 60 and self._video_count() == baseline_videos:
                for label in agree_texts:
                    try:
                        b = page.get_by_text(label, exact=True).last
                        if b.count() > 0 and b.is_visible():
                            b.click(timeout=4000)
                            last_agree_at = now
                            print(f"[{self.name}] 已点击内联按钮「{label}」，等待视频生成…")
                            break
                    except Exception:
                        continue
            # 豆包要求确认/继续 → 自动回复（按文本去重，每个新问句只回一次）
            if self._video_count() == baseline_videos and text != last_confirm_text:
                for p in self.confirm_patterns:
                    if p.search(text):
                        if page.locator('div[role="dialog"][data-state="open"]').count() > 0:
                            print(f"[{self.name}] 命中确认提示但对话框仍打开（疑似生成进度），暂不回确认")
                            last_confirm_text = text
                            break
                        print(f"[{self.name}] 豆包要求确认（命中 {p.pattern}），自动回复确认生成…")
                        self.type_and_send(self.confirm_reply)
                        last_confirm_text = text
                        break
            # 对话长文有时不进入特定 message_item；用全页文本判断真实进度/产物。
            try:
                page_text = page.locator("body").inner_text(timeout=3000)
            except Exception:
                page_text = text
            text = text or page_text[-6000:]
            # 长时间无任何新内容且未进入生成流程 → 快速失败重试
            if (self._video_count() == baseline_videos
                    and not any(k in text for k in ACTIVE_KEYWORDS)
                    and "你的视频生成好了" not in text
                    and "预计等待" not in text
                    and (now - last_text_change) > no_resp_s):
                raise GenerationFailed(f"[{self.name}] 发送后 {no_resp_s / 60:.0f} 分钟页面无任何响应")
            if now - last_log >= 60:
                last_log = now
                snippet = " ".join(text.split())[-60:]
                print(f"[{self.name}] 生成中… 已等待 {now - (deadline - timeout_s):.0f}s | 页面最新: {snippet!r}")
            # Only a new assistant response can prove exhaustion; page history
            # and user prompts are not authoritative platform refusals.
            if getattr(self, '_quota_assistant_before', None) is not None:
                quota_reply = self._assistant_text()
                if (self._last_reply_source == 'latest_assistant'
                        and quota_reply != self._quota_assistant_before
                        and not any(k in quota_reply for k in ACTIVE_KEYWORDS)):
                    if quota_exhaustion_matches(quota_reply, self.quota_patterns):
                        raise QuotaExhausted(f"[{self.name}] 命中额度提示")
            # 生成失败
            for p in self.failure_patterns:
                if p.search(text):
                    raise GenerationFailed(f"[{self.name}] 豆包报告生成失败: {p.pattern}")
            # 出现新视频 → 尝试下载
            if self._video_count() > baseline_videos:
                # 等播放器稳定
                page.wait_for_timeout(4000)
                if self._try_save_video(Path(dest)):
                    print(f"[{self.name}] 视频已下载: {dest}")
                    return Path(dest)
                if not quota_hinted:
                    print(f"[{self.name}] 检测到视频但下载未成功，继续重试…")
                    quota_hinted = True
        raise GenerationTimeout(f"[{self.name}] 等待视频生成超过 {timeout_s / 60:.0f} 分钟")

    # ---- 组合动作 ----
    def run_task(self, prompt_text: str, image_paths: list, dest: Path) -> Path:
        self.open_chat()
        self.new_chat()
        if get(self.cfg, "doubao.use_video_skill", False):
            try:
                skill_name = get(self.cfg, "doubao.video_skill_name", "视频生成")
                btn = self.page.get_by_role("button", name=skill_name).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=4000)
                    self.page.wait_for_timeout(1200)
                    print(f"[{self.name}] 已点击「{skill_name}」技能按钮")
            except Exception as e:
                print(f"[{self.name}] 点击视频技能失败（忽略，走自动路由）: {e}")
        self.upload_images(image_paths)
        try:
            self._quota_assistant_before = self._assistant_text()
        except Exception:
            self._quota_assistant_before = None
        self.type_and_send(prompt_text)
        return self.wait_result(Path(dest))

    # ---- 实机勘察 ----
    INSPECT_JS = """() => {
      const r = {fileInputs: [], editables: [], textareas: [], buttons: [], videos: 0};
      document.querySelectorAll('input[type=file]').forEach(e => r.fileInputs.push(
        {accept: e.accept, multiple: e.multiple, cls: (e.className||'').slice(0,120), id: e.id,
         aria: e.getAttribute('aria-label'), testid: e.getAttribute('data-testid')}));
      document.querySelectorAll('[contenteditable="true"], [role=textbox]').forEach(e => r.editables.push(
        {tag: e.tagName, id: e.id, cls: (e.className||'').toString().slice(0,140),
         role: e.getAttribute('role'), aria: e.getAttribute('aria-label'),
         testid: e.getAttribute('data-testid'),
         placeholder: e.getAttribute('placeholder') || e.getAttribute('data-placeholder')}));
      document.querySelectorAll('textarea').forEach(e => r.textareas.push(
        {id: e.id, cls: (e.className||'').toString().slice(0,140), aria: e.getAttribute('aria-label'),
         testid: e.getAttribute('data-testid'), placeholder: e.getAttribute('placeholder')}));
      document.querySelectorAll('button, [role=button], [data-testid]').forEach(e => {
        const a = e.getAttribute('aria-label') || e.getAttribute('data-testid') || (e.textContent||'').trim().slice(0,30);
        if (a) r.buttons.push({label: a.slice(0,80), tag: e.tagName,
                               cls: (e.className||'').toString().slice(0,90),
                               testid: e.getAttribute('data-testid')});
      });
      r.videos = document.querySelectorAll('video').length;
      r.parameters = [...document.querySelectorAll('[role=slider],input[type=range],[class*=slider],[class*=Slider]')].map(e=>e.outerHTML.slice(0,4000));
      r.durationControl = [...document.querySelectorAll('div,span,label')].filter(e=>e.children.length===0 && e.textContent.trim()==='时长').map(e=>e.parentElement.parentElement.outerHTML.slice(0,12000));
      return r;
    }"""

    def inspect_dump(self, out_dir):
        import json
        import os

        page = self.page
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(5000)  # 等 SPA 水合
        page.screenshot(path=str(out_dir / f"{self.name}_page.png"))
        messages=page.locator('[data-testid="receive_message"]')
        if messages.count():
            (out_dir/f'{self.name}_received_dom.html').write_text(messages.last.evaluate('(e)=>e.outerHTML'),encoding='utf-8')
            (out_dir/f'{self.name}_received_text.txt').write_text(messages.last.inner_text(),encoding='utf-8')
        user_messages = page.evaluate('''() => [...document.querySelectorAll(
          '[data-testid="send_message"], [data-testid="user_message"], [data-message-role="user"], [data-role="user"]'
        )].map(node => ({
          data_testid: node.getAttribute('data-testid'),
          data_message_role: node.getAttribute('data-message-role'),
          id: node.id,
          closest_message_id: node.closest('[data-message-id]')?.getAttribute('data-message-id') || '',
          innerText: node.innerText,
          textContent: node.textContent,
          message_text_contents: [...node.querySelectorAll('[data-testid="message_text_content"]')]
            .map(child => ({innerText: child.innerText, textContent: child.textContent}))
        }))''')
        user_path = out_dir / f'{self.name}_user_messages.json'
        fd = os.open(user_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(user_messages, handle, ensure_ascii=False, indent=2)
        print(f'[{self.name}] 用户消息诊断已保存: {user_path}（{len(user_messages)} 条）')
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
            for x in info["textareas"][:20]:
                lines.append(str(x))
            lines.append(f"== video 元素数量: {info['videos']} ==")
            lines.append('== parameter DOM ==')
            lines.extend(info.get('parameters',[]));lines.extend(info.get('durationControl',[]))
            lines.append("== buttons / data-testid (前120个) ==")
            for x in info["buttons"][:120]:
                lines.append(str(x))
        (out_dir / f"{self.name}_dom.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"[{self.name}] 勘察结果已保存: {out_dir / (self.name + '_dom.txt')} 与截图 {self.name}_page.png")
