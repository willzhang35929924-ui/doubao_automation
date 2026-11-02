"""Bounded two-video requests and recoverable downloads, without resubmitting unknown work."""
import hashlib
import re
import time
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


class BatchGenerationMixin:
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
        observed=self.page.evaluate('''() => {
          const cards=[];
          const selector='[class*="block-video-"],[class*="fulfill-video-error-box-"]';
          for(const [receive_index,message] of [...document.querySelectorAll('[data-testid="receive_message"]')].entries()) {
            const nodes=[...message.querySelectorAll(selector)];
            for(const [card_ordinal,node] of nodes.entries()) {
              const message_id=node.closest('[data-message-id]')?.getAttribute('data-message-id')||'';
              const status=String(node.className).includes('fulfill-video-error-box-')?'failed':'ready';
              const video=node.querySelector('video');
              const url=video?.currentSrc||video?.src||'';
              const stable='card:'+message_id+':'+card_ordinal;
              cards.push({kind:'card',status,stable,url,poster:node.querySelector('img')?.src||'',
                message_id,card_message_id:message_id,card_ordinal,receive_index,card_position:cards.length,
                duration:Number.isFinite(video?.duration)?video.duration:0});
            }
          }
          if(cards.length) return {cards,assets:cards.filter(c=>c.status==='ready')};
          const seen=new Set(), result=[];
          for(const [index,v] of [...document.querySelectorAll('video')].entries()) {
            const url=v.currentSrc||v.src||v.querySelector('source')?.src||'';
            if(!url || !Number.isFinite(v.duration) || v.duration<=0) continue;
            const stable=url.split('?')[0]; if(seen.has(stable)) continue; seen.add(stable);
            const message=v.closest('[data-message-id],[data-testid*="message"]');
            const siblings=message?[...message.querySelectorAll('video')]:[];
            result.push({url,stable,video_index:index,message_ordinal:siblings.indexOf(v),
              message_id:message?.getAttribute('data-message-id')||message?.id||'',
              duration:Number.isFinite(v.duration)?v.duration:0});
          }
          return {cards:[],assets:result};
        }''')
        self._batch_cards_snapshot=observed['cards']
        return observed['assets']

    def _key_asset(self, asset):
        value = asset.get('stable') or asset.get('url', '').split('?')[0]
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _assistant_text(self):
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
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix('.downloading.mp4')
        src = asset.get('url', '')
        if asset.get('kind')=='card' and not src:
            selector='[class*="block-video-"],[class*="fulfill-video-error-box-"]'
            try:
                message=self.page.locator('[data-testid="receive_message"]').nth(asset['receive_index'])
                card=message.locator(selector).nth(asset['card_ordinal'])
                if card.get_attribute('class').find('fulfill-video-error-box-')>=0:
                    return False
                previous=self.page.evaluate("()=>[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src||'')")
                card.click(timeout=4000)
                self.page.wait_for_function("()=>[...document.querySelectorAll('video')].some(v=>v.currentSrc||v.src)",timeout=20000)
                candidates=self.page.evaluate("()=>[...document.querySelectorAll('video')].map((v,index)=>({url:v.currentSrc||v.src||'',video_index:index,duration:Number.isFinite(v.duration)?v.duration:0})).filter(v=>v.url)")
                chosen=next((v for v in candidates if v['url'] not in previous),candidates[-1] if candidates else None)
                if chosen:
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
        videos = self.page.locator(self.sel.get('video_card', 'video'))
        index = asset.get('video_index', -1)
        if not 0 <= index < videos.count():
            return False
        video = videos.nth(index)
        try:
            video.hover(timeout=2000)
            card = video
            for _ in range(7):
                card = card.locator('..')
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
        known = {tid: ref for tid, ref in result['remote_refs'].items()
                 if isinstance(ref, dict) and ref.get('key') and (ref.get('url') or ref.get('card_message_id'))}
        result['remote_refs'] = dict(known)
        accounted = set()
        one_at_time, single_followups, control_phases = False, set(), set()
        remote_started = False
        last_reply, last_url = '', result['chat_url']
        try:
            if resume:
                target = context.get('chat_url', '')
                parsed = urlsplit(target)
                if parsed.hostname not in ('www.doubao.com', 'doubao.com') or parsed.path.rstrip('/') in ('', '/chat'):
                    result.update(error_kind='needs_user', error='原请求缺少可恢复的对话地址，禁止重复发送')
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
                balance_selector = self.sel.get('quota_balance')
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
                            'card_count':len(getattr(self,'_batch_cards_snapshot',[]))}
                baseline['assistant_before']=self._assistant_text()
                prompt = batch_prompt(tasks, context)
                self._batch_sent_prompt = prompt
                # Persist uncertainty BEFORE the potentially successful click.
                on_submitted(self.page.url, baseline)
                result.update(submitted=True, chat_url=self.page.url)
                self.type_and_send(prompt)
                self._save_batch_debug(context)
            self._batch_body_before = baseline.get('body', '')
            baseline_keys = set(baseline.get('keys', []))
            timeout = float(self.dcfg.get('generation_timeout_minutes', 25)) * 60
            deadline, last_log = time.monotonic() + timeout, 0
            while time.monotonic() < deadline:
                self._assert_logged_in()
                self._handle_dialogs()
                if self.page.url != last_url:
                    last_url = self.page.url
                    on_submitted(last_url, baseline)
                    result['chat_url'] = last_url
                assets = self._batch_assets()
                cards=getattr(self,'_batch_cards_snapshot',[])
                original_ids=context.get('original_task_ids') or context.get('task_ids') or [t['id'] for t in tasks]
                card_offset=int(baseline.get('card_count',0))
                failed_ids=[original_ids[c['card_position']-card_offset] for c in cards
                            if c['status']=='failed' and 0<=c['card_position']-card_offset<len(original_ids)]
                failed_ids=[tid for tid in failed_ids if any(t['id']==tid for t in tasks)]
                if resume and not assets and not cards:
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
                        matches=[a for a in assets if ref.get('message_id') and a.get('message_id')==ref['message_id']
                                 and a.get('message_ordinal')==ref.get('message_ordinal')]
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
                    if destination.is_file() and destination.stat().st_size > 1000:
                        result['videos'][tid] = str(destination)
                    elif self._download_batch_asset(ref, destination):
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
                if not resume and reply and reply != last_reply:
                    missing_tasks = [t for t in tasks if t['id'] not in known]
                    phase = 'initial' if not known else ','.join(t['id'] for t in missing_tasks)
                    explicit_remaining = bool(known) and bool(re.search(r'(第二|第2|下一|另一)[^。\n]{0,24}(?:是否|需要|确认|继续)|(?:是否|确认|继续)[^。\n]{0,24}(第二|第2|下一|另一)',reply))
                    if (action in ('split','confirm','single') and missing_tasks and not active
                            and (not remote_started or explicit_remaining) and phase not in control_phases):
                        control_phases.add(phase)
                        one_at_time |= action=='single' and not remote_started
                        follow_tasks = missing_tasks[:1] if one_at_time else missing_tasks
                        windows='、'.join(f'第{t["seg_index"]+1}段（{t.get("start_seconds",0):g}–{t.get("end_seconds",t["est_seconds"]):g}秒）' for t in follow_tasks)
                        clarification=f'选择分段分别生成的方案，不压缩整片。确认按上面的原始剧本，仅开始{windows}，各自独立提供视频。其他窗口不要生成。'
                        baseline['controls'].append({'phase':phase,'task_ids':[t['id'] for t in follow_tasks]})
                        if known and one_at_time:
                            single_followups.update(t['id'] for t in follow_tasks)
                        on_submitted(self.page.url,baseline)
                        last_reply = reply
                        self.type_and_send(clarification)
                        self._batch_sent_prompt = clarification
                if (not resume and one_at_time and result['videos'] and len(known) < len(tasks) and not active):
                    next_task = next(t for t in tasks if t['id'] not in known)
                    if next_task['id'] not in single_followups:
                        single_followups.add(next_task['id'])
                        baseline['controls'].append({'phase':'single_next','task_ids':[next_task['id']]})
                        on_submitted(self.page.url,baseline)
                        self.type_and_send('前一条已经完成。现在仅生成下一条独立视频：\n' + batch_prompt([next_task], context))
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
