"""脚本切分与首尾帧衔接。

核心逻辑：
1. parse_scripts: 把 Gemini 一次回复中的多个脚本拆出来
   （支持 "### 脚本1" / "**脚本1**" / "脚本1：" / 裸 "脚本1" / "Script 01：" 等格式）；
2. build_segments:
   - 有明确总时长或时间戳：使用连续的 15 秒窗口，最后一段保留余数；
   - 无明确时长：按中文口播语速（默认 4.5 字/秒）切分；
3. 每段生成发给豆包的完整提示词：显式写明首帧画面 = 上一段尾帧画面，
   尾帧画面用「定格」描述并在下一段原样复述，配合真实尾帧截图上传，
   保证多段视频拼接不违和。
"""
import re

SENTENCE_END = "。！？!?；;…"
CLAUSE_END = "，,、：:—"
_ALL_PUNCT = SENTENCE_END + CLAUSE_END + "“”‘’（）()《》—…·,.!?;:\"' \n\t-"

SCRIPT_HEADER_RE = re.compile(
    # ① markdown 标题行: "### 脚本1" / "## **脚本二：开场**"
    r"^\s*#{1,6}\s*\**\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)[^\n]*$|"
    # 标题加粗、时长在加粗外："**脚本1**（50秒）"。
    r"^\s*\*\*\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)[^*\n]*\*\*\s*[（(][^\n]*$|"
    # ② 行内加粗: "**脚本1**" / "**脚本3：xxx**"
    r"\*\*\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)\s*[^*\n]*\*\*|"
    # ③ 独立成行带冒号（冒号后可接任意内容）: "脚本1：" / "脚本2:标题" / "脚本 1：【xxx】"
    r"^\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)\s*[:：][^\n]*$|"
    # ④ 独立成行裸标题: "脚本1"（Gemini 常见输出）
    r"^\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)\s*$|"
    # ⑤ 带时长的裸中文标题，以及英文标题；不匹配目录 "Script 01 ~ 05:"。
    r"^\s*脚本\s*([0-9]+|[一二三四五六七八九十]+)\s*[（(][^\n]*$|"
    r"^\s*(?:#{1,6}\s*)?\**\s*Script\s*([0-9]+)\**\s*(?:[:：][^\n]*|[（(][^\n]*|\s*)$",
    re.MULTILINE | re.IGNORECASE,
)

# Gemini 网页版回复开头的 UI 噪声行
_UI_PREFIX_RE = re.compile(r"(?im)^\s*(gemini said|gemini 说|模型回复|ai 回复)\s*$\n?")

# 时间戳块: "00:00 - 00:04｜摄影与构图：…" / "0:00-0:05 | …"
TS_BLOCK_RE = re.compile(
    r"^\s*(\d{1,2})[:：](\d{2})\s*[-–~—]\s*(\d{1,2})[:：](\d{2})\s*[｜|]?\s*(.*)$",
    re.MULTILINE,
)

_NUMBER = r"\d+(?:\.\d+)?"
_DURATION_META_RE = re.compile(r"(?m)^\s*【脚本总时长：(" + _NUMBER + r")秒】\s*$")
_DURATION_TOKEN_RE = re.compile(r"(" + _NUMBER + r")\s*(?:秒(?:钟)?|s(?:ec(?:ond)?s?)?\b)", re.I)
_TOTAL_DURATION_RE = re.compile(
    r"(?im)^\s*(?:[-*#]+\s*)?[【\[]?\s*(?:(?:脚本|视频|广告)\s*)?"
    r"(?:总时长|目标时长|total[ _]+(?:duration|seconds)|duration_seconds)"
    r"\s*[:：=]?\s*(" + _NUMBER + r")\s*(?:秒(?:钟)?|s(?:ec(?:ond)?s?)?\b)?"
)
_DURATION_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*#]+\s*)?\**\s*(?:时长|duration)\**\s*[:：=]\s*(" + _NUMBER + r")"
    r"\s*(?:秒(?:钟)?|s(?:ec(?:ond)?s?)?\b)?"
)
_AUDIO_LINE_RE = re.compile(
    r"^\s*[-*•]?\s*\**\s*(?:音频(?:与(?:口播|文字))?|口播(?:文案)?|配音(?:文案)?|旁白|文案)"
    r"\**\s*[:：]\s*\**\s*(.*)$"
)
_FIELD_LINE_RE = re.compile(r"^\s*[-*•]?\s*\**\s*[^\W\d_][\w与/ 、]{0,22}\**\s*[:：]")
_VISUAL_LINE_RE = re.compile(r"^\s*[-*•]?\s*\**\s*(?:场景|画面|视觉|摄影|镜头|照明|光线|风格|产品|卖点)")


# ---------------- 时长估算 ----------------
def count_spoken(text: str) -> int:
    """参与朗读的字符数（去掉空白与标点）。"""
    return sum(1 for ch in text if ch not in _ALL_PUNCT)


def estimate_seconds(text: str, chars_per_second: float) -> float:
    cps = max(float(chars_per_second), 0.5)
    return round(count_spoken(text) / cps, 1)


# ---------------- 切分（无时间戳回退路径） ----------------
def _split_keep(text: str, delims: str) -> list:
    """按分隔符切分并保留分隔符在片段末尾。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in delims:
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out


def _hard_cut(text: str, max_chars: int) -> list:
    """无标点长文本按口播字数硬切。"""
    out, cur, spoken = [], "", 0
    for ch in text:
        cur += ch
        if ch not in _ALL_PUNCT:
            spoken += 1
        if spoken >= max_chars:
            out.append(cur)
            cur, spoken = "", 0
    if cur.strip():
        out.append(cur)
    return out


def chunk_narration(text: str, max_chars: int, min_spoken: int = 0) -> list:
    """把口播文案切成每段 ≤ max_chars（按口播字数）的块，优先句边界，其次逗号，最后硬切。
    min_spoken>0 时做二次平衡：过短的段从后一段"偷"开头子句，末尾过短段并入前一段，
    避免 3 秒超短段浪费一次 15 秒生成额度。"""
    text = re.sub(r"\s+", "", text.strip())
    if not text:
        return []
    sentences = _split_keep(text, SENTENCE_END)
    chunks, cur = [], ""
    for s in sentences:
        if count_spoken(cur + s) <= max_chars:
            cur += s
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if count_spoken(s) <= max_chars:
            cur = s
            continue
        for clause in _split_keep(s, CLAUSE_END):
            if count_spoken(cur + clause) <= max_chars:
                cur += clause
                continue
            if cur:
                chunks.append(cur)
                cur = ""
            if count_spoken(clause) <= max_chars:
                cur = clause
            else:
                pieces = _hard_cut(clause, max_chars)
                cur = pieces[-1] if pieces else ""
                for p in pieces[:-1]:
                    chunks.append(p)
    if cur.strip():
        chunks.append(cur)
    fixed = []
    for c in chunks:
        c = c.strip()
        if c and c[-1] not in SENTENCE_END:
            c += "。"
        if c:
            fixed.append(c)
    if min_spoken > 0:
        fixed = _rebalance(fixed, min_spoken, max_chars)
    return fixed


def _rebalance(chunks: list, min_spoken: int, max_chars: int) -> list:
    """短段向前一段之后的段偷开头子句；末尾短段并入前一段。"""
    out = [c for c in chunks if c.strip()]
    changed = True
    guard = 0
    while changed and guard < 50:
        changed = False
        guard += 1
        for i in range(len(out) - 1):
            if count_spoken(out[i]) >= min_spoken:
                continue
            clauses = _split_keep(out[i + 1], CLAUSE_END)
            while clauses and count_spoken(out[i]) < min_spoken:
                c0 = clauses[0]
                if count_spoken(out[i]) + count_spoken(c0) > max_chars:
                    break
                out[i] += c0
                clauses.pop(0)
                changed = True
            if not clauses:
                out.pop(i + 1)
            else:
                out[i + 1] = "".join(clauses)
            if changed:
                break
    if len(out) >= 2 and count_spoken(out[-1]) < min_spoken \
            and count_spoken(out[-2]) + count_spoken(out[-1]) <= max_chars:
        out[-2] = out[-2] + out[-1]
        out.pop()
    return [c for c in out if c.strip()]


def _last_clause(text: str, limit: int = 28) -> str:
    parts = [p for p in re.split(f"[{re.escape(SENTENCE_END + CLAUSE_END)}]", text) if p and p.strip()]
    tail = parts[-1].strip().rstrip(_ALL_PUNCT) if parts else text.strip()
    return (tail[-limit:] if len(tail) > limit else tail) or "产品完整展示画面"


# ---------------- Gemini 回复解析 ----------------
def parse_scripts(gemini_text: str) -> list:
    """仍返回字符串列表；标题中的时长以独立元数据行保留。"""
    text = (gemini_text or "").strip()
    text = re.sub(r"```(?:markdown|text)?", "", text)  # 去代码围栏
    text = _UI_PREFIX_RE.sub("", text)                 # 去 Gemini 网页版 UI 前缀行
    matches = list(SCRIPT_HEADER_RE.finditer(text))
    if not matches:
        return [text] if text else []
    scripts = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip(" \n-*#")
        if body:
            duration = _DURATION_TOKEN_RE.search(m.group(0))
            if duration:
                seconds = float(duration.group(1))
                if seconds > 0 and not _DURATION_META_RE.search(body):
                    body = f"【脚本总时长：{seconds:g}秒】\n" + body
            scripts.append(body)
    return scripts


_NARRATION_LABEL_RE = re.compile(
    r"^\s*[-*•]?\s*\**\s*(?:口播文案|口播|配音文案|文案)\s*\**\s*[:：][ \t]*(.*)$",
    re.MULTILINE,
)


def extract_narration(script_text: str) -> str:
    """从单个脚本中抽取口播文案；没有标记时返回去掉标题/要点行后的全文。"""
    text = (script_text or "").strip()
    blocks = parse_timestamp_blocks(text)
    if blocks:
        return "".join(_block_parts(block)[0] for block in blocks)
    if any(_AUDIO_LINE_RE.match(line) for line in text.splitlines()):
        return _block_parts({'lines': text.splitlines()})[0]
    keep = []
    for ln in text.splitlines():
        if (_DURATION_META_RE.fullmatch(ln) or _TOTAL_DURATION_RE.match(ln)
                or _DURATION_LABEL_RE.match(ln) or SCRIPT_HEADER_RE.fullmatch(ln)):
            continue
        if re.match(r"^\s*#{1,6}\s", ln):
            continue
        if re.match(r"^\s*[-*•]\s*\**\s*(卖点定位|画面风格|画面描述|场景|镜头|时长)\s*\**\s*[:：]", ln):
            continue
        keep.append(ln.strip())
    return "\n".join(x for x in keep if x).strip() or text


# ---------------- 时间戳解析 ----------------
def parse_timestamp_blocks(text: str) -> list:
    """把带时间戳的脚本切成块；单个长时间块也保留。"""
    blocks, cur = [], None
    for ln in text.splitlines():
        m = TS_BLOCK_RE.match(ln)
        if m:
            if int(m.group(2)) >= 60 or int(m.group(4)) >= 60:
                raise ValueError('时间戳秒数必须小于60')
            if cur:
                blocks.append(cur)
            cur = {"start": int(m.group(1)) * 60 + int(m.group(2)),
                   "end": int(m.group(3)) * 60 + int(m.group(4)),
                   "lines": [ln]}
        elif cur is not None and ln.strip():
            cur["lines"].append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def detect_total_seconds(text: str) -> float | None:
    """读取脚本总时长；时间戳的结束时间也构成明确时长。"""
    text = text or ""
    for pattern in (_DURATION_META_RE, _TOTAL_DURATION_RE):
        match = pattern.search(text)
        if match and float(match.group(1)) > 0:
            return float(match.group(1))
    headers = list(SCRIPT_HEADER_RE.finditer(text))
    if len(headers) == 1:
        match = _DURATION_TOKEN_RE.search(headers[0].group(0))
        if match and float(match.group(1)) > 0:
            return float(match.group(1))
    blocks = parse_timestamp_blocks(text)
    if blocks:
        end = max(block['end'] for block in blocks)
        return float(end) if end > 0 else None
    match = _DURATION_LABEL_RE.search(text)
    if match and float(match.group(1)) > 0:
        return float(match.group(1))
    return None


def _spoken_payload(text):
    text = text.strip()
    # Parenthesized delivery instructions and outer quotation marks are not spoken.
    text = re.sub(r"^(?:(?:\([^\n)]*\)|（[^\n）]*）)\s*)+", "", text)
    if len(text) >= 2 and (text[0], text[-1]) in (("\"", "\""), ("“", "”"), ("'", "'"), ("‘", "’")):
        text = text[1:-1]
    return text


def _block_parts(block):
    """Separate spoken payloads from visual instructions without copying audio into both."""
    audio_groups, current, visual = [], None, []
    for original in block['lines']:
        timestamp = TS_BLOCK_RE.match(original)
        line = timestamp.group(5).strip() if timestamp else original.strip()
        audio = _AUDIO_LINE_RE.match(line)
        if audio:
            current = [audio.group(1)]
            audio_groups.append(current)
        elif current is not None and not line:
            current.append('')
        elif current is not None and line and not _FIELD_LINE_RE.match(line) and not line.startswith('#'):
            current.append(line)
        else:
            current = None
            if line:
                visual.append(line)
    if audio_groups:
        return "".join(_spoken_payload("\n".join(group)) for group in audio_groups), visual
    # Unlabelled prose in a time block is the narration fallback; labelled
    # camera/lighting directions remain visual instructions.
    prose = [line for line in visual if not _FIELD_LINE_RE.match(line) and not line.startswith('#')]
    return "\n".join(prose), [line for line in visual if line not in prose]


def _slice_spoken(text, begin_fraction, end_fraction):
    """Use shared cut positions so adjacent windows neither repeat nor lose text."""
    if not text:
        return ""
    candidates = None
    if not re.search(r"[\u3400-\u9fff]", text) and len(re.findall(r"\w+", text)) > 1:
        candidates = [0] + [match.end() for match in re.finditer(r"\s+", text)] + [len(text)]

    def position(fraction):
        if fraction <= 0:
            return 0
        if fraction >= 1:
            return len(text)
        expected = len(text) * fraction
        if candidates:
            return min(candidates, key=lambda value: (abs(value - expected), value))
        return round(expected)

    return text[position(begin_fraction):position(end_fraction)]


def _format_seconds(seconds):
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    second = f"{remainder:02.0f}" if remainder.is_integer() else f"{remainder:05.2f}".rstrip('0')
    return f"{minutes:02d}:{second}"


def _duration_preamble(script_text, blocks):
    first = TS_BLOCK_RE.search(script_text)
    text = script_text[:first.start()] if blocks and first else script_text
    visual = _block_parts({'lines': text.splitlines()})[1]
    return [line for line in visual if _VISUAL_LINE_RE.match(line) and _FIELD_LINE_RE.match(line)]


def _segments_by_duration(script_text, blocks, total, opening, lock, product_name):
    if total <= 0:
        raise ValueError('脚本总时长必须大于0')
    previous_end = 0
    for block in blocks:
        if block['start'] < previous_end or block['end'] <= block['start'] or block['end'] > total:
            raise ValueError('脚本时间块重叠、倒序或超出总时长，不能自动提交')
        if block['start'] != previous_end:
            raise ValueError('脚本时间块存在空缺，不能自动补写后提交')
        previous_end = block['end']
    if blocks and previous_end != total:
        raise ValueError('脚本时间块没有覆盖声明的总时长，不能自动补写后提交')
    windows = []
    start = 0.0
    while start < total:
        end = min(start + 15.0, total)
        windows.append((start, end))
        start = end
    preamble = _duration_preamble(script_text, blocks)
    full_narration = extract_narration(script_text) if not blocks else ''
    parts = [(block, *_block_parts(block)) for block in blocks]
    segments, previous_description = [], ''
    for index, (start, end) in enumerate(windows):
        scenes, spoken, last_visual = [], [], []
        for block, audio, visuals in parts:
            left, right = max(start, block['start']), min(end, block['end'])
            if right <= left:
                continue
            span = block['end'] - block['start']
            excerpt = _slice_spoken(audio, (left - block['start']) / span,
                                    (right - block['start']) / span)
            spoken.append(excerpt)
            scenes.append(f"{_format_seconds(float(left))} - {_format_seconds(float(right))}｜" +
                          (visuals[0] if visuals else '画面按本段时间推进。'))
            scenes.extend(visuals[1:])
            if excerpt:
                scenes.append('口播文案：' + excerpt)
            last_visual = visuals or last_visual
        if not blocks:
            spoken = [_slice_spoken(full_narration, start / total, end / total)]
            scenes = ['口播文案：' + spoken[0]] if spoken[0] else []
        narration = ''.join(spoken)
        start_description = opening if index == 0 else previous_description
        visual = next((line.split('：', 1)[-1].strip() for line in reversed(last_visual)
                       if line.startswith(('视觉', '画面'))), '产品完整展示画面')
        end_description = '严格定格在：' + visual[:60]
        lines = [f"【第{index + 1}段，共{len(windows)}段】真实电商广告风格短视频。" +
                 (f"产品：{product_name}。" if product_name else '')]
        if index:
            lines.append('上传的第一张图片是上一段视频的最后一帧，请以它作为本段视频的首帧，画面无缝衔接。')
        lines.append('首帧画面：' + start_description + (lock if index else ''))
        lines.extend(preamble)
        lines.append(f"时间戳分解（本段对应原片 {start:g}-{end:g} 秒，段长精确 {end - start:g} 秒）：")
        lines.extend(scenes)
        if not narration:
            lines.append('本段没有新增口播，保持画面与动作连续，不补写其他分段的台词。')
        lines.append('尾帧画面：' + end_description + (lock if index + 1 < len(windows) else '。'))
        segments.append(dict(index=index, total=len(windows), narration=narration,
                             est_seconds=end - start, start_seconds=start, end_seconds=end,
                             start_frame_desc=start_description, end_frame_desc=end_description,
                             prompt='\n'.join(lines)))
        previous_description = end_description
    return segments


def _last_visual_desc(chunk_blocks: list, limit: int = 60) -> str:
    """取段内最后一个时间戳块的"视觉与动作"描述，作为尾帧定格描述。"""
    for ln in reversed(chunk_blocks[-1]["lines"]):
        s = ln.strip()
        if s.startswith(("视觉与动作", "视觉")):
            desc = s.split("：", 1)[-1].strip() if "：" in s else s
            if desc:
                return desc[:limit]
    last = chunk_blocks[-1]["lines"][-1].strip()
    desc = last.split("：", 1)[-1].strip() if "：" in last else last
    return desc[:limit] or "产品完整展示画面"


# ---------------- 分段与豆包提示词 ----------------
def build_segments(script_text: str, cfg: dict, product_name: str = "") -> list:
    """把完整脚本切成 N 段，每段带可直接发给豆包的提示词。
    明确时长固定每15秒切分；只有无时长脚本才按口播字数估算。"""
    vc = cfg.get("video", {}) or {}
    cps = float(vc.get("chars_per_second", 4.5))
    target = float(vc.get("target_segment_seconds", 13))
    min_seg = float(vc.get("min_segment_seconds", 5))
    max_sec = float(vc.get("max_segment_seconds", 15))
    if cps <= 0 or target <= 0 or max_sec <= 0:
        raise ValueError('语速和分段时长必须大于0')
    max_chars = max(int(min(target, max_sec, 15) * cps), 1)
    min_spoken = max(int(min_seg * cps), 0)
    opening = vc.get("opening_frame_desc", "产品静置于画面中央，光线明亮柔和，镜头缓慢推近")
    lock = vc.get("frame_lock_suffix", "；产品外观、位置、光线与机位保持不变，与上一段结尾画面无缝衔接")

    blocks = parse_timestamp_blocks(script_text)
    total = detect_total_seconds(script_text)
    if total is not None:
        return _segments_by_duration(script_text, blocks, total, opening, lock, product_name)
    return _segments_by_narration(script_text, cps, max_chars, min_spoken, opening, lock, product_name)


def _segments_by_timestamp(script_text, blocks, max_sec, opening, lock, product_name) -> list:
    # Keep the former helper available for callers, without its unsafe
    # oversized-block passthrough behavior.
    return _segments_by_duration(script_text, blocks, float(max(b['end'] for b in blocks)),
                                 opening, lock, product_name)


def _segments_by_narration(script_text, cps, max_chars, min_spoken, opening, lock, product_name) -> list:
    narration = extract_narration(script_text)
    chunks = chunk_narration(narration, max_chars, min_spoken=min_spoken)
    n = len(chunks)
    if n == 0:
        return []

    segments, prev_end, elapsed = [], "", 0.0
    for i, chunk in enumerate(chunks):
        start_desc = opening if i == 0 else prev_end
        end_desc = f"镜头定格在：{_last_clause(chunk)}"
        est = estimate_seconds(chunk, cps)
        lines = [f"【第{i + 1}段，共{n}段】真实电商广告风格的产品宣传短视频。"
                 + (f"产品：{product_name}。" if product_name else "")]
        if i > 0:
            lines.append("上传的第一张图片是上一段视频的最后一帧，请以它作为本段视频的首帧，画面无缝衔接。")
        lines += [
            f"首帧画面：{start_desc}" + ("" if i == 0 else lock),
            "镜头运动：跟随口播内容自然运镜，产品外观必须与上传图片完全一致，不变形、不换包装。",
            f"口播文案（约{est}秒，需完整念完）：{chunk}",
            f"尾帧画面：{end_desc}" + (lock if i < n - 1 else "。"),
        ]
        segments.append({
            "index": i,
            "total": n,
            "narration": chunk,
            "est_seconds": est,
            "start_seconds": elapsed,
            "end_seconds": elapsed + est,
            "start_frame_desc": start_desc,
            "end_frame_desc": end_desc,
            "prompt": "\n".join(lines),
        })
        prev_end = end_desc
        elapsed += est
    return segments
