"""Bounded two-video requests and recoverable downloads, without resubmitting unknown work."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def quota_exhaustion_matches(text, patterns):
    # Ambiguous login, verification, network or system failures require review.
    if re.search(r'网络|连接|超时|系统错误|验证码|验证|登录|未登录|network|connection|timed?\s*out|system\s+error|captcha|verif(?:y|ication)|log\s*in|sign\s*in', text or '', re.IGNORECASE):
        return False
    return any(pattern.search(text or '') for pattern in patterns)


def quota_remaining(text):
    """Only explicit video/free generation balances count as a platform observation."""
    patterns = (
        r'(?:今日|今天)[^\n。]{0,12}?(?:视频|免费)[^\n。]{0,16}?(?:剩余|还可|可用|可生成)\s*[:：]?\s*(\d+)\s*(?:次|条|个)',
        r'(?:视频生成|生成视频)[^\n。]{0,16}?(?:剩余|还可)\s*[:：]?\s*(\d+)\s*(?:次|条|个)',
        r'(?:今日)?免费(?:生成)?次数\s*(?:剩余|[:：])\s*(\d+)',
    )
    values = [int(m.group(1)) for pattern in patterns for m in re.finditer(pattern, text or '')]
    return min(values) if values else None


def batch_prompt(tasks, context):
    if not 1 <= len(tasks) <= 2:
        raise ValueError('一批只能请求1或2条视频')
    for task in tasks:
        duration = float(task.get('target_seconds') or task.get('est_seconds') or 0)
        if not 0 < duration <= 15:
            raise ValueError('每条视频目标时长必须在0到15秒之间')
    total = context.get('total_seconds') or max(t.get('end_seconds', 0) for t in tasks)
    lines = [
        '【本次执行边界，请严格遵守】',
        f'完整影片总时长为{total:g}秒。本条命令只请求下面{len(tasks)}条独立视频，每条最长15秒，分别生成并分别提供下载。',
        '不要把完整长片或本批全部剧情压缩、缩写、加速成一条15秒视频。其余时间窗口会由我的下一次命令单独执行，本次不要生成剩余窗口。',
        '同批视频必须按剧情顺序衔接，人物、产品、包装、场景、光线保持一致。上传的尾帧参考图用于本批开头。',
    ]
    for task in tasks:
        duration = float(task.get('target_seconds') or task['est_seconds'])
        start, end = task.get('start_seconds', 0), task.get('end_seconds', duration)
        lines.extend([
            f'\n【独立视频{task["seg_index"]+1}/{task["seg_total"]}：原片{start:g}–{end:g}秒，本条内容长{duration:g}秒】',
            task['prompt'],
        ])
        if duration < 15:
            lines.append(f'本条全部动作和口播须在前{duration:g}秒完成；如工具只能输出15秒，剩余时间只保持结尾画面，不添加新内容。')
    script = context.get('script_text', '')
    if script:
        lines.extend(['\n【完整剧本，只用于人物和剧情连续性参考，不是本次全部生成范围】', script])
    lines.append(f'\n请直接依次生成上述{len(tasks)}条独立视频，不要合并成一条，也不要再次询问是否压缩。')
    return '\n'.join(lines)


def choose_reply(text):
    text = text or ''
    if re.search(r'(验证码|安全验证|人机验证|完成验证)', text):
        return 'needs_user'
    if (re.search(r'(压缩|缩短|精简)', text)
            and re.search(r'(分成|拆成|分段|两段|2段|两条|2条)', text)
            and re.search(r'(方案|选择|你希望|请选择|也可以|还是)', text)):
        return 'split'
    if re.search(r'(?:每次|一次|单次)[^。\n]{0,12}(?:只能|仅能|最多|只支持)[^。\n]{0,8}(?:1|一)(?:个|条|段)', text):
        return 'single'
    if re.search(r'(确认后|确认开始|是否开始|是否继续|继续生成吗|我就开始生成|开始生成视频吗|请确认|要我.{0,12}生成)', text):
        return 'confirm'
    return ''


def verified_download_matches(path, remote_ref):
    """Read-only shared raw-file validation for recovery and generation paths."""
    verifier = BatchGenerationMixin()
    return verifier._bound_download_matches(path, remote_ref, remote_ref)


class BatchGenerationMixin:
    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def _asset_identity(self, asset):
        if not isinstance(asset, dict) or asset.get('ownership_verified') is not True:
            return None
        message_id = asset.get('card_message_id') or asset.get('message_id')
        if not message_id:
            return None
        return {'key': asset.get('key') or self._key_asset(asset), 'message_id': str(message_id),
                'ordinal': asset.get('card_ordinal') if asset.get('kind') == 'card' else asset.get('message_ordinal'),
                'owner_user_message_id': asset.get('owner_user_message_id', '')}

    def _bound_download_matches(self, path, asset, original_ref):
        path = Path(path)
        identity = self._asset_identity(asset)
        if not identity or identity != self._asset_identity(original_ref or {}):
            return False
        try:
            binding = json.loads(path.with_suffix(path.suffix + '.asset.json').read_text(encoding='utf-8'))
            return (isinstance(binding, dict) and path.stat().st_size > 1000 and binding.get('identity') == identity
                    and binding.get('size') == path.stat().st_size
                    and binding.get('sha256') == self._file_hash(path))
        except (OSError, ValueError, TypeError):
            return False

    def _preserve_unbound_download(self, path):
        path = Path(path)
        if not path.exists():
            return
        archive = path.parent / '.download-backups'
        archive.mkdir(exist_ok=True, mode=0o700)
        backup = archive / (path.name + '.' + self._file_hash(path) + '.unverified')
        if not backup.exists():
            shutil.copy2(path, backup)
            backup.chmod(0o600)
            sidecar = path.with_suffix(path.suffix + '.asset.json')
            if sidecar.exists():
                shutil.copy2(sidecar, backup.with_suffix(backup.suffix + '.asset.json'))

    def _bind_download(self, path, asset):
        path = Path(path)
        identity = self._asset_identity(asset)
        if not identity:
            raise ValueError('下载文件缺少本请求的产物身份，不能登记为已完成')
        binding = {'version': 1, 'identity': identity, 'size': path.stat().st_size,
                   'sha256': self._file_hash(path)}
        fd, filename = tempfile.mkstemp(prefix='.asset-', suffix='.json.part', dir=path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(binding, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path.with_suffix(path.suffix + '.asset.json'))
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _original_anchor_proof(ownership, chat_path):
        """Record only after the complete original user turn was verified."""
        if not re.fullmatch(r'/chat/\d+/?', str(chat_path or '')) or not ownership.get('prompt'):
            raise ValueError('原消息证明必须绑定完整提示词和正式数字ID对话')
        digest = lambda text: hashlib.sha256(re.sub(r'\s+', '', str(text or '')).encode('utf-8')).hexdigest()
        return {'version': 1, 'chat_path': chat_path,
                'prompt_sha256': digest(ownership['prompt']), 'marker_sha256': digest(ownership.get('marker'))}

    @staticmethod
    def _ownership_spec(tasks, context, baseline, resume):
        """Bind outputs to a particular submitted user turn, never a page-wide index."""
        def complete(spec):
            spec['task_ids'] = list(context.get('original_task_ids') or [task['id'] for task in tasks])
            try:
                proof_valid = spec.get('original_anchor_proof') == BatchGenerationMixin._original_anchor_proof(spec, spec.get('chat_path'))
            except ValueError:
                proof_valid = False
            # A collapsed original turn cannot establish the slot ordering of
            # a multi-output request. Keep that ambiguous case fail-closed.
            spec['allow_verified_control_anchor'] = bool(proof_valid and len(spec['task_ids']) == 1)
            return spec
        saved = baseline.get('ownership')
        if isinstance(saved, dict) and any(saved.get(k) for k in ('marker', 'prompt', 'user_message_id')):
            return complete(dict(saved))
        spec = {'prompt': batch_prompt(tasks, context), 'control_texts': []}
        if not resume:
            request_id = str(context.get('request_id') or uuid.uuid4().hex)
            spec['marker'] = '【自动化任务标记：' + request_id + '】'
        else:
            # Old journals predate markers. Only an exact normalized original
            # prompt (or an explicitly reconciled message ID) can recover them.
            for control in baseline.get('controls', []):
                selected = [t for t in tasks if t['id'] in control.get('task_ids', [])]
                if selected and len(selected) == len(control.get('task_ids', [])):
                    windows = '、'.join(f'第{t["seg_index"]+1}段（{t.get("start_seconds",0):g}–{t.get("end_seconds",t["est_seconds"]):g}秒）' for t in selected)
                    spec['control_texts'].append(f'选择分段分别生成的方案，不压缩整片。确认按上面的原始剧本，仅开始{windows}，各自独立提供视频。其他窗口不要生成。')
        return complete(spec)

    def _save_batch_debug(self, context, reply=''):
        if not context.get('request_id'):
            return
        try:
            import json
            from .paths import WORKSPACE,safe_filename
            directory=WORKSPACE/'generation_requests'/safe_filename(context['request_id'])
            directory.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(directory/'latest.png'))
            (directory/'latest_reply.txt').write_text(reply,encoding='utf-8')
            metadata=self.page.evaluate('''() => [...document.querySelectorAll('[data-testid*="message"],[data-message-role],[data-role]')].map(e=>({tag:e.tagName,testid:e.getAttribute('data-testid'),role:e.getAttribute('data-message-role')||e.getAttribute('data-role'),id:e.id,cls:String(e.className).slice(0,180)}))''')
            (directory/'message_structure.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception:
            pass

    def configure_video_mode(self, inspect_menu=False):
        """Select the existing video skill and its 15-second control when exposed."""
        skill = self.dcfg.get('video_skill_name', '视频生成')
        selector=re.compile(r'(?:^|[·\s])(?:[4-9]|1[0-5])\s*(?:s|秒)$',re.I)
        # Existing chats render their composer after DOMContentLoaded.  Looking
        # up buttons immediately after navigation mistakes that loading state
        # for a missing control, especially on the second batch in one chat.
        self.page.wait_for_function('''skill => [...document.querySelectorAll('button')].some(b => {
          const text=(b.innerText||b.getAttribute('aria-label')||'').trim();
          return b.getClientRects().length && (text===skill || /(?:^|[·\\s])(?:[4-9]|1[0-5])\\s*(?:s|秒)$/i.test(text));
        })''',arg=skill,timeout=30000)
        button = self.page.get_by_role('button', name=skill, exact=True)
        controls=self.page.get_by_role('button',name=selector)
        if (self.dcfg.get('use_video_skill', True) and not controls.count()
                and button.count() and button.first.is_visible()):
            button.first.click(timeout=5000)
            self.page.get_by_role('button',name=selector).last.wait_for(state='visible',timeout=15000)
        self._handle_dialogs()
        controls=self.page.get_by_role('button',name=selector)
        if not controls.count() or not controls.last.is_visible():
            raise RuntimeError('找不到视频时长控件，发送前已停止，避免默认10秒浪费次数')
        control=controls.last
        duration_pattern=re.compile(r'(?:^|[·\s])15\s*(?:s|秒)$',re.I)
        desired_ratio=self.cfg.get('video',{}).get('aspect_ratio','9:16')
        ratio_pattern=re.compile(r'(?:^|[·\s])'+re.escape(desired_ratio)+r'(?:$|[·\s])')
        label=control.inner_text()
        duration_ready=bool(duration_pattern.search(label))
        ratio_ready=bool(ratio_pattern.search(label))
        if duration_ready and ratio_ready:
            return
        control.click(timeout=4000)
        self.page.wait_for_timeout(600)
        if inspect_menu:
            return
        selected=duration_ready
        # The current Doubao UI exposes a 4–15 second ARIA slider, not a menu item.
        sliders=self.page.get_by_role('slider')
        for i in range(sliders.count()):
            if selected:break
            slider=sliders.nth(i)
            maximum=slider.get_attribute('aria-valuemax') or slider.get_attribute('max')
            section=slider.locator('xpath=ancestor::section[1]')
            labelled_duration=(section.count() and '时长' in section.inner_text()
                               and re.search(r'15\s*s',section.inner_text(),re.I))
            # Current slider stores indices 0..11 for the labelled 4s..15s values.
            if maximum and float(maximum) in (11,15) and labelled_duration and slider.is_visible():
                slider.press('End',timeout=4000)
                # React may publish aria-valuenow and the saved button label later.
                # This records an attempted selection; the actual button is verified below.
                selected=True
                break
        for desired in ('15s','15秒','15 秒'):
            if selected:break
            for role in ('button','option','radio','menuitemradio'):
                option=self.page.get_by_role(role,name=desired,exact=True)
                if option.count() and option.last.is_visible():
                    option.last.click(timeout=4000);selected=True;break
        if selected:
            ratio=self.page.get_by_role('button',name=desired_ratio,exact=True)
            if not ratio_ready and ratio.count() and ratio.last.is_visible():ratio.last.click(timeout=3000)
            for label in ('确定','应用','完成'):
                apply=self.page.get_by_role('button',name=label,exact=True)
                if apply.count() and apply.last.is_visible():
                    apply.last.click(timeout=3000);break
            check=(self.page.get_by_role('button',name=selector)
                   .filter(has_text=duration_pattern).filter(has_text=ratio_pattern))
            try:
                check.last.wait_for(state='visible',timeout=5000)
                self.page.keyboard.press('Escape')
                return
            except Exception:pass
        raise RuntimeError(f'未能确认视频时长已选择15秒且比例为{desired_ratio}，发送前已停止')

    def _batch_assets(self):
        observed=self.page.evaluate('''ownership => {
          const norm = text => String(text || '').replace(/\\s+/g, '');
          const canonical = [...document.querySelectorAll('[data-testid="send_message"], [data-testid="receive_message"]')];
          const messages = canonical.length ? canonical :
            [...document.querySelectorAll('[data-message-role="user"], [data-message-role="assistant"], [data-role="user"], [data-role="assistant"]')]
              .filter((node, index, all) => !all.some((other, j) => j !== index && other.contains(node)));
          const user = node => node.matches('[data-testid="send_message"], [data-message-role="user"], [data-role="user"]');
          const id = node => node.closest('[data-message-id]')?.getAttribute('data-message-id') || node.id || '';
          const owns = node => {
            if (!user(node)) return false;
            if (ownership.user_message_id) return id(node) === ownership.user_message_id;
            const text = norm(node.querySelector('[data-testid="message_text_content"]')?.innerText || node.innerText);
            return ownership.marker ? text.includes(norm(ownership.marker)) &&
              (!ownership.prompt || text.includes(norm(ownership.prompt))) :
              Boolean(ownership.prompt && text.includes(norm(ownership.prompt)));
          };
          let anchors = messages.filter(owns), anchorKind='original';
          if (!anchors.length && ownership.user_message_id && ownership.chat_path === location.pathname && ownership.prompt) {
            // A reload can replace a DOM-only node ID. The complete original
            // prompt must still match uniquely in the same locked chat.
            anchors = messages.filter(node => user(node) &&
              norm(node.querySelector('[data-testid="message_text_content"]')?.innerText || node.innerText).includes(norm(ownership.prompt)) &&
              (!ownership.marker || norm(node.innerText).includes(norm(ownership.marker))));
          }
          // No authenticated ownership proof means no asset, including when
          // old history hydrates after an apparently empty composer.
          const empty = {cards:[], assets:[], owner_found:false, route_ready:false, latest_reply:''};
          if (!anchors.length && ownership.allow_verified_control_anchor === true &&
              ownership.chat_path === location.pathname && /^\\/chat\\/\\d+\\/?$/.test(location.pathname)) {
            // Virtual lists can unmount the original prompt. Only a prior
            // complete-prompt proof in this exact chat permits the latest
            // registered control turn to stand in for it. Marker-only and
            // partial control text never establish ownership.
            const control = norm((ownership.control_texts || []).at(-1));
            if (control) {
              anchors = messages.filter(node => user(node) &&
                norm(node.querySelector('[data-testid="message_text_content"]')?.innerText || node.innerText) === control);
              anchorKind='verified_control';
            }
          }
          if (anchors.length !== 1) return empty;
          const anchor = anchors[0], anchorIndex = messages.indexOf(anchor);
          const originalUserID = anchorKind === 'original' ? id(anchor) : (ownership.user_message_id || '');
          const routeReady = /^\\/chat\\/\\d+\\/?$/.test(location.pathname) &&
            (!ownership.chat_path || ownership.chat_path === location.pathname);
          const metadata = {owner_found:true, route_ready:routeReady, user_message_id:originalUserID,
                            anchor_kind:anchorKind, control_message_id:anchorKind === 'verified_control' ? id(anchor) : '',
                            chat_path:location.pathname, latest_reply:''};
          if (!routeReady) return {...empty, ...metadata};
          const allowed = (ownership.control_texts || []).map(norm);
          const received=[]; let latestOwned=null;
          for (const node of messages.slice(anchorIndex + 1)) {
            if (user(node)) {
              const text=norm(node.querySelector('[data-testid="message_text_content"]')?.innerText || node.innerText);
              if (!allowed.some(value => value && text === value)) break;
            } else received.push(node);
            latestOwned=node;
          }
          // A confirmation user turn can be mounted before its answer. Do
          // not act again on the assistant confirmation that preceded it.
          metadata.latest_reply = latestOwned && !user(latestOwned) ? latestOwned.innerText : '';
          metadata.latest_reply_message_id = latestOwned && !user(latestOwned) ? id(latestOwned) : '';
          const cards=[];
          const selector='[class*="block-video-"],[class*="fulfill-video-error-box-"]';
          for(const message of received) {
            const nodes=[...message.querySelectorAll(selector)];
            for(const [card_ordinal,node] of nodes.entries()) {
              const message_id=id(node);
              if (!message_id) continue;
              const status=String(node.className).includes('fulfill-video-error-box-')?'failed':'ready';
              const video=node.querySelector('video');
              const url=video?.currentSrc||video?.src||'';
              const stable='card:'+location.pathname+':'+message_id+':'+card_ordinal;
              cards.push({kind:'card',status,stable,url,poster:node.querySelector('img')?.src||'',
                message_id,card_message_id:message_id,card_ordinal,card_position:cards.length,
                ownership_verified:true,owner_user_message_id:originalUserID,
                duration:Number.isFinite(video?.duration)?video.duration:0});
            }
          }
          if(cards.length) return {...metadata,cards,assets:cards.filter(c=>c.status==='ready')};
          const seen=new Set(), result=[];
          for(const message of received) for(const [ordinal,v] of [...message.querySelectorAll('video')].entries()) {
            const url=v.currentSrc||v.src||v.querySelector('source')?.src||'';
            if(!url || !Number.isFinite(v.duration) || v.duration<=0) continue;
            const stable=url.split('?')[0]; if(seen.has(stable)) continue; seen.add(stable);
            const message_id=id(v); if (!message_id) continue;
            result.push({url,stable,message_ordinal:ordinal,message_id,
              ownership_verified:true,owner_user_message_id:originalUserID,
              duration:Number.isFinite(v.duration)?v.duration:0});
          }
          return {...metadata,cards:[],assets:result};
        }''', getattr(self, '_batch_ownership', {}))
        self._batch_scope_state=observed
        self._batch_cards_snapshot=observed['cards']
        return observed['assets']

    def _key_asset(self, asset):
        value = asset.get('stable') or asset.get('url', '').split('?')[0]
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _assistant_text(self):
        scope = getattr(self, '_batch_scope_state', None)
        if scope is not None:
            self._last_reply_source='latest_assistant'
            return scope.get('latest_reply', '') if scope.get('owner_found') and scope.get('route_ready') else ''
        selectors = ('[data-testid="receive_message"]', '[data-message-role="assistant"]',
                     '[data-role="assistant"]', '[data-testid*="assistant"]', '[class*="message-receive"]')
        for selector in selectors:
            loc = self.page.locator(selector)
            if loc.count():
                try:
                    self._last_reply_source='latest_assistant'
                    return loc.last.inner_text(timeout=3000)
                except Exception:
                    pass
        text = self.page.locator('body').inner_text(timeout=4000)
        self._last_reply_source='body_delta'
        text = text.replace(getattr(self, '_batch_sent_prompt', ''), '') if getattr(self, '_batch_sent_prompt', '') else text
        before = getattr(self, '_batch_body_before', '')
        common = 0
        for a, b in zip(text, before):
            if a != b:
                break
            common += 1
        return text[common:][-7000:]

    def _download_batch_asset(self, asset, dest):
        # Old global video indexes and unverified saved references are not
        # sufficient evidence that a file belongs to this request.
        if asset.get('ownership_verified') is not True:
            return False
        message_id = asset.get('card_message_id') or asset.get('message_id')
        if not message_id:
            return False
        message = self.page.locator('[data-message-id=' + json.dumps(str(message_id)) + ']')
        if message.count() != 1:
            return False
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix('.downloading.mp4')
        src = asset.get('url', '')
        if asset.get('kind')=='card' and not src:
            selector='[class*="block-video-"],[class*="fulfill-video-error-box-"]'
            try:
                card=message.locator(selector).nth(asset['card_ordinal'])
                if card.count() != 1:
                    return False
                if card.get_attribute('class').find('fulfill-video-error-box-')>=0:
                    return False
                previous=self.page.evaluate("()=>[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src||'')")
                card.click(timeout=4000)
                self.page.wait_for_function("()=>[...document.querySelectorAll('video')].some(v=>v.currentSrc||v.src)",timeout=20000)
                candidates=self.page.evaluate("()=>[...document.querySelectorAll('video')].map((v,index)=>({url:v.currentSrc||v.src||'',video_index:index,duration:Number.isFinite(v.duration)?v.duration:0})).filter(v=>v.url)")
                fresh=[v for v in candidates if v['url'] not in previous]
                if len(fresh) != 1:
                    return False
                chosen=fresh[0]
                asset.update(chosen);src=chosen['url']
            except Exception:
                return False
        if src.startswith(('https://', 'http://')):
            try:
                response = self.session.context.request.get(src, timeout=90000)
                if response.ok and len(response.body()) > 1000:
                    temporary.write_bytes(response.body())
                    temporary.replace(dest)
                    return True
            except Exception:
                pass
        videos = self.page.locator('video')
        matching=[i for i in range(videos.count()) if videos.nth(i).evaluate("v=>v.currentSrc||v.src||''") == src]
        if len(matching) != 1:
            return False
        video = videos.nth(matching[0])
        try:
            video.hover(timeout=2000)
            card = video
            for _ in range(3):
                card = card.locator('..')
                if card.evaluate("e=>['BODY','HTML'].includes(e.tagName)"):
                    break
                if card.locator('video').count() > 1:
                    break
                for selector in self.sel.get('download_button', []):
                    buttons = card.locator(selector)
                    for j in range(buttons.count()):
                        button = buttons.nth(j)
                        if not button.is_visible():
                            continue
                        with self.page.expect_download(timeout=30000) as download:
                            button.click(timeout=4000)
                        download.value.save_as(str(temporary))
                        if temporary.is_file() and temporary.stat().st_size > 1000:
                            temporary.replace(dest)
                            return True
        except Exception:
            pass
        return False

    def run_batch(self, tasks, image_paths, dests, context, on_submitted, on_generated):
        """One request, up to two separately billed assets; resume_only never sends text."""
        result = {'videos': {}, 'remote_refs': dict(context.get('remote_refs') or {}),
                  'unresolved': [t['id'] for t in tasks], 'submitted': False,
                  'chat_url': context.get('chat_url', ''), 'error_kind': '', 'error': '',
                  'continuation_safe': False, 'quota_remaining': None}
        resume = bool(context.get('resume_only'))
        baseline = dict(context.get('baseline') or {})
        self._batch_scope_state = None
        self._batch_ownership = self._ownership_spec(tasks, context, baseline, resume)
        baseline['ownership'] = self._batch_ownership
        known = {tid: ref for tid, ref in result['remote_refs'].items()
                 if isinstance(ref, dict) and ref.get('key') and (ref.get('url') or ref.get('card_message_id'))}
        result['remote_refs'] = dict(known)
        download_bindings = dict(context.get('remote_refs') or {})
        accounted = set()
        one_at_time, single_followups, control_phases = False, set(), set()
        remote_started = False
        last_url, last_reply_identity = result['chat_url'], ''
        try:
            if resume:
                target = context.get('chat_url', '')
                parsed = urlsplit(target)
                result['submitted'] = True
                if (parsed.hostname not in ('www.doubao.com', 'doubao.com')
                        or not re.fullmatch(r'/chat/\d+/?', parsed.path)):
                    result.update(error_kind='needs_user', error='原请求缺少正式数字ID的豆包对话地址（临时 local_ 路由不可恢复）；请核对原会话正式地址，已保留请求，禁止重复发送')
                    return result
                self.page.goto(target, wait_until='domcontentloaded')
                self.page.wait_for_timeout(3000)
                self._assert_logged_in()
                result['submitted'] = True
            else:
                self.open_chat()
                target = context.get('chat_url', '')
                if target and urlsplit(target).hostname in ('www.doubao.com', 'doubao.com'):
                    self.page.goto(target, wait_until='domcontentloaded')
                else:
                    self.new_chat()
                self.configure_video_mode()
                # A whole-page search could match a user's prompt or old chat.
                # Only inspect an explicitly configured visible balance element.
                observed = None
                balance_selector = getattr(self, 'sel', {}).get('quota_balance')
                if balance_selector:
                    selector = balance_selector if isinstance(balance_selector, str) else ', '.join(balance_selector)
                    try:
                        element = self.page.locator(selector).first
                        if element.count() and element.is_visible():
                            observed = quota_remaining(element.inner_text(timeout=4000))
                    except Exception:
                        pass
                if observed is not None and context.get('on_quota_remaining'):
                    context['on_quota_remaining'](observed)
                self.upload_images(image_paths)
                self._batch_body_before = self.page.locator('body').inner_text(timeout=4000)
                assets = self._batch_assets()
                baseline = {'keys': [self._key_asset(a) for a in assets], 'assets': assets,
                            'body': self._batch_body_before, 'controls': [],
                            'card_count':len(getattr(self,'_batch_cards_snapshot',[])),
                            'ownership':self._batch_ownership, 'scope_version':1}
                baseline['assistant_before']=self._assistant_text()
                prompt = batch_prompt(tasks, context)
                prompt += '\n' + self._batch_ownership['marker']
                prompt += '\n此标记仅用于自动化定位，不得出现在生成内容、视频画面、字幕或口播中。'
                self._batch_sent_prompt = prompt
                # Persist uncertainty BEFORE the potentially successful click.
                # The old page URL can still identify the previous chat during
                # SPA navigation. Save it only after this exact user turn exists.
                on_submitted('', baseline)
                result.update(submitted=True, chat_url='')
                last_url = ''
                self.type_and_send(prompt)
                self._save_batch_debug(context)
            self._batch_body_before = baseline.get('body', '')
            baseline_keys = set(baseline.get('keys', []))
            timeout = float(self.dcfg.get('generation_timeout_minutes', 25)) * 60
            ack_timeout = max(0, float(self.dcfg.get('ownership_ack_timeout_seconds', 180)))
            ownership_wait_started = None
            deadline, last_log = time.monotonic() + timeout, 0
            while time.monotonic() < deadline:
                self._assert_logged_in()
                self._handle_dialogs()
                assets = self._batch_assets()
                scope = getattr(self, '_batch_scope_state', None)
                ownership_changed = False
                if scope is not None:
                    if not scope.get('owner_found') or not scope.get('route_ready'):
                        now = time.monotonic()
                        if ownership_wait_started is None:
                            ownership_wait_started = now
                        if now - ownership_wait_started >= ack_timeout:
                            detail = '本次用户消息定位' if not scope.get('owner_found') else '正式数字ID对话路由确认'
                            error = f'{detail}超过{ack_timeout:g}秒仍未就绪；已停止等待并保留原请求，请核对原会话后恢复，禁止盲目重发'
                            self._save_batch_debug(context,error)
                            result.update(error_kind='needs_user', error=error, continuation_safe=False)
                            return result
                        remaining = ack_timeout - (now - ownership_wait_started)
                        self.page.wait_for_timeout(max(1, int(min(float(self.dcfg.get('poll_interval_seconds',5)),remaining)*1000)))
                        continue
                    ownership_wait_started = None
                    if baseline.get('scope_version') != 1:
                        baseline['unscoped_history'] = {k:baseline.get(k) for k in ('keys','assets','card_count')}
                        baseline.update(keys=[], assets=[], card_count=0, scope_version=1)
                        baseline_keys = set()
                    self._batch_ownership.update(chat_path=scope['chat_path'])
                    if scope.get('anchor_kind') == 'original':
                        proof = self._original_anchor_proof(self._batch_ownership, scope['chat_path'])
                        ownership_changed |= self._batch_ownership.get('original_anchor_proof') != proof
                        self._batch_ownership['original_anchor_proof'] = proof
                        self._batch_ownership['allow_verified_control_anchor'] = len(self._batch_ownership.get('task_ids', [])) == 1
                    if scope.get('user_message_id') and scope.get('anchor_kind') == 'original':
                        ownership_changed |= self._batch_ownership.get('user_message_id') != scope['user_message_id']
                        self._batch_ownership['user_message_id'] = scope['user_message_id']
                    ownership_changed |= self._batch_ownership.get('last_anchor_kind') != scope.get('anchor_kind')
                    self._batch_ownership['last_anchor_kind'] = scope.get('anchor_kind')
                if self.page.url != last_url or ownership_changed or (scope is not None and baseline.get('ownership_acknowledged') is not True):
                    last_url = self.page.url
                    if scope is not None:
                        baseline['ownership_acknowledged'] = True
                    on_submitted(last_url, baseline)
                    result['chat_url'] = last_url
                cards=getattr(self,'_batch_cards_snapshot',[])
                original_ids=context.get('original_task_ids') or context.get('task_ids') or [t['id'] for t in tasks]
                card_offset=int(baseline.get('card_count',0))
                failed_ids=[original_ids[c['card_position']-card_offset] for c in cards
                            if c['status']=='failed' and 0<=c['card_position']-card_offset<len(original_ids)]
                failed_ids=[tid for tid in failed_ids if any(t['id']==tid for t in tasks)]
                if resume and not assets and not cards and scope is None:
                    self.page.wait_for_timeout(int(float(self.dcfg.get('poll_interval_seconds',5))*1000))
                    continue
                by_key = {self._key_asset(a): a for a in assets}
                if resume and baseline_keys:
                    before_assets = baseline.get('assets') or []
                    n = len(baseline_keys)
                    if len(assets)<n:
                        # Chat history can mount one lazy card at a time.  An
                        # incomplete prefix is not evidence of changed assets.
                        self.page.wait_for_timeout(int(float(self.dcfg.get('poll_interval_seconds',5))*1000))
                        continue
                    prefix = assets[:n]
                    keys_match = len(prefix)==n and {self._key_asset(a) for a in prefix}==baseline_keys
                    ids_match = (len(before_assets)==len(prefix)==n
                                 and all(a.get('message_id') and a['message_id']==b.get('message_id')
                                         for a,b in zip(before_assets,prefix)))
                    if not (keys_match or ids_match):
                        result.update(error_kind='needs_user',error='旧视频标识已变化，无法可靠区分原请求产物；已阻止误领与重发')
                        return result
                    baseline_keys.update(self._key_asset(a) for a in prefix)
                for task in tasks:
                    ref = known.get(task['id'])
                    if ref and ref.get('key') in by_key:
                        known[task['id']] = dict(by_key[ref['key']], key=ref['key'])
                    elif resume and ref:
                        ordinal = 'card_ordinal' if ref.get('kind') == 'card' else 'message_ordinal'
                        matches=[a for a in assets if ref.get('message_id') and a.get('message_id')==ref['message_id']
                                 and a.get(ordinal)==ref.get(ordinal)]
                        if len(matches)==1:
                            known[task['id']]=dict(matches[0],key=self._key_asset(matches[0]))
                        else:
                            result.update(error_kind='needs_user',error='原产物标识已变化，无法安全映射剩余视频；禁止重复生成')
                            return result
                assigned = {r.get('key') for r in known.values()}
                for asset in assets:
                    key = self._key_asset(asset)
                    if key in baseline_keys or key in assigned:
                        continue
                    if asset.get('kind')=='card':
                        slot=asset['card_position']-card_offset
                        tid=original_ids[slot] if 0<=slot<len(original_ids) else None
                        missing=next((t for t in tasks if t['id']==tid and tid not in known),None)
                        if not missing:continue
                    else:
                        missing = next((t for t in tasks if t['id'] not in known), None)
                    if not missing:
                        break
                    ref = dict(asset, key=key)
                    known[missing['id']] = ref
                    assigned.add(key)
                for task in tasks:
                    tid, destination = task['id'], Path(dests[task['id']])
                    ref = known.get(tid)
                    if not ref:
                        continue
                    if tid not in accounted:
                        on_generated(tid, ref)
                        accounted.add(tid)
                    result['remote_refs'][tid] = ref
                    if self._bound_download_matches(destination, ref, download_bindings.get(tid)):
                        result['videos'][tid] = str(destination)
                    else:
                        self._preserve_unbound_download(destination)
                        if self._download_batch_asset(ref, destination):
                            self._bind_download(destination, ref)
                            download_bindings[tid] = dict(ref)
                            result['videos'][tid] = str(destination)
                            print(f'[{self.name}] 分段已下载 {task["seg_index"]+1}/{task["seg_total"]}: {destination.name}', flush=True)
                    if ref.get('kind')=='card' and ref.get('url'):
                        try:self.page.keyboard.press('Escape')
                        except Exception:pass
                result['unresolved'] = [t['id'] for t in tasks if t['id'] not in result['videos']]
                if not result['unresolved']:
                    result['continuation_safe'] = True
                    return result
                reply = self._assistant_text()
                if reply==baseline.get('assistant_before'):
                    reply=''
                action = choose_reply(reply)
                active = any(k in reply for k in ('正在生成', '生成中', '排队中', '预计等待', '生成任务已提交'))
                remote_started |= active or bool(known)
                if action == 'needs_user':
                    result.update(error_kind='needs_user', error='豆包需要人工完成验证，已保存当前请求')
                    return result
                if (failed_ids and not active and getattr(self,'_last_reply_source','')=='latest_assistant'
                        and re.search(r'失败[^。\n]{0,35}(?:额度|次数)[^。\n]{0,12}(?:未扣|不扣|未消耗)|未扣[^。\n]{0,25}失败',reply)):
                    result.update(error_kind='partial_failed_refunded',error='平台明确报告部分视频失败且失败额度未扣除',
                                  rejected_task_ids=failed_ids,continuation_safe=True,
                                  rejection_evidence=dict(kind='generation_failed_refunded',definitive=True,
                                    active=False,source='latest_assistant',quota_not_charged=True,failed_task_ids=failed_ids))
                    self._save_batch_debug(context,reply)
                    return result
                reply_identity = (scope or {}).get('latest_reply_message_id') or reply
                if reply and reply_identity != last_reply_identity:
                    missing_tasks = [t for t in tasks if t['id'] not in known]
                    phase = 'initial' if not known else ','.join(t['id'] for t in missing_tasks)
                    explicit_remaining = bool(known) and bool(re.search(r'(第二|第2|下一|另一)[^。\n]{0,24}(?:是否|需要|确认|继续)|(?:是否|确认|继续)[^。\n]{0,24}(第二|第2|下一|另一)',reply))
                    if (action in ('split','confirm','single') and missing_tasks and not active
                            and (resume or phase in control_phases or not remote_started or explicit_remaining)):
                        if resume or phase in control_phases:
                            self._save_batch_debug(context,reply)
                            result.update(error_kind='needs_user', continuation_safe=False,
                                          error=('原请求仍等待平台确认；恢复模式不会发送消息，请在原会话明确确认开始后再继续'
                                                 if resume else '已发送一次明确开始生成指令，平台仍要求确认；已停止自动确认，请在原会话人工核对，尚未领取任何未确认产物'))
                            return result
                        control_phases.add(phase)
                        one_at_time |= action=='single' and not remote_started
                        follow_tasks = missing_tasks[:1] if one_at_time else missing_tasks
                        windows='、'.join(f'第{t["seg_index"]+1}段（{t.get("start_seconds",0):g}–{t.get("end_seconds",t["est_seconds"]):g}秒）' for t in follow_tasks)
                        clarification=f'确认，立即开始生成上述{windows}的独立视频，严格沿用上面的原始剧本和已选择的视频参数。'
                        if action == 'split':
                            clarification += '已选择分段分别生成，不压缩整片。'
                        clarification += f'本次仅生成这{len(follow_tasks)}条，各自独立提供视频；其他窗口不要生成，不再重复列参数或询问确认。'
                        if self._batch_ownership.get('marker'):
                            clarification += '\n' + self._batch_ownership['marker']
                            clarification += '\n此标记仅用于自动化定位，不得出现在生成内容、视频画面、字幕或口播中。'
                        self._batch_ownership.setdefault('control_texts', []).append(clarification)
                        baseline['controls'].append({'phase':phase,'task_ids':[t['id'] for t in follow_tasks]})
                        if known and one_at_time:
                            single_followups.update(t['id'] for t in follow_tasks)
                        on_submitted(self.page.url,baseline)
                        last_reply_identity = reply_identity
                        self.type_and_send(clarification)
                        self._batch_sent_prompt = clarification
                if (not resume and one_at_time and result['videos'] and len(known) < len(tasks) and not active):
                    next_task = next(t for t in tasks if t['id'] not in known)
                    if next_task['id'] not in single_followups:
                        single_followups.add(next_task['id'])
                        baseline['controls'].append({'phase':'single_next','task_ids':[next_task['id']]})
                        followup = '前一条已经完成。现在仅生成下一条独立视频：\n' + batch_prompt([next_task], context)
                        if self._batch_ownership.get('marker'):
                            followup += '\n' + self._batch_ownership['marker']
                            followup += '\n此标记仅用于自动化定位，不得出现在生成内容、视频画面、字幕或口播中。'
                        self._batch_ownership.setdefault('control_texts', []).append(followup)
                        on_submitted(self.page.url,baseline)
                        self.type_and_send(followup)
                        deadline = time.monotonic() + timeout
                if not any(k in reply for k in ('正在生成', '生成中', '排队', '预计等待')):
                    if (not action and not active and getattr(self,'_last_reply_source','')=='latest_assistant'
                            and quota_exhaustion_matches(reply, self.quota_patterns)):
                        self._save_batch_debug(context,reply)
                        result.update(error_kind='quota', error='豆包提示视频生成次数已用完',
                                      continuation_safe=not remote_started)
                        return result
                    if any(p.search(reply) for p in self.failure_patterns) and not action:
                        self._save_batch_debug(context,reply)
                        result.update(error_kind='failed', error='豆包明确报告本批生成失败', continuation_safe=True)
                        return result
                if time.monotonic() - last_log >= 60:
                    last_log = time.monotonic()
                    print(f'[{self.name}] 本批已取回{len(result["videos"])}/{len(tasks)}条，继续等待未完成分段', flush=True)
                    self._save_batch_debug(context,reply)
                self.page.wait_for_timeout(int(float(self.dcfg.get('poll_interval_seconds', 5)) * 1000))
            result.update(error_kind='download' if known else 'timeout', error='请求结果尚未完全取回，已保存原对话，禁止盲目重发')
            return result
        except Exception as exc:
            self._save_batch_debug(context,str(exc))
            kind = {'NeedsLogin': 'login', 'QuotaExhausted': 'quota', 'NeedsUserAction': 'needs_user'}.get(type(exc).__name__, 'failed')
            result.update(error_kind=kind, error=str(exc), continuation_safe=not result['submitted'])
            return result
