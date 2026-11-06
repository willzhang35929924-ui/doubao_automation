"""Bind local descriptions to saved assets and compile them as script evidence."""

import hashlib
import json
import uuid
from pathlib import Path

from .paths import PRODUCTS_DIR, SOURCES_DIR, resolve

_CAPTION_SECTION = '\n\n【工作台素材识别参考】\n'
_CONTEXT_VERSION = 1
MAX_IMAGES = 6
MAX_DESCRIPTION = 2000
_VIDEO_SUFFIXES = frozenset(('.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.mpeg', '.mpg'))


def caption_source(snapshot, record_id):
    if not isinstance(record_id, str) or not record_id:
        raise ValueError('请选择要识别的素材记录')
    matches = [row for row in snapshot.get('sources', []) if row.get('record_id') == record_id]
    if len(matches) != 1:
        raise ValueError('素材记录不存在或已变化，请刷新页面')
    return matches[0]


def _product(snapshot, source):
    keyword = (source.get('keyword') or '').strip()
    matches = [p for p in snapshot.get('products', [])
               if keyword and (p.get('keyword') or '').strip() == keyword]
    if len(matches) == 1 and matches[0].get('record_id'):
        return matches[0]
    return None


def caption_images(snapshot, source):
    product = _product(snapshot, source)
    return list(dict.fromkeys(product.get('images') or [])) if product else []


def caption_videos(source):
    return [dict(index=index, path=path) for index, path in enumerate(source.get('videos') or [])
            if isinstance(path, str) and Path(path).suffix.lower() in _VIDEO_SUFFIXES]


def _caption_key(snapshot, source, kind):
    if kind == 'images':
        product = _product(snapshot, source)
        if not product:
            raise ValueError('请为当前 keyword 匹配唯一的产品记录后再识别图片')
        return 'images:' + product['record_id']
    if kind == 'video':
        record_id = source.get('record_id')
        if not isinstance(record_id, str) or not record_id:
            raise ValueError('素材记录缺少编号')
        return 'video:' + record_id
    raise ValueError('请选择图片或视频识别')


def caption_target(snapshot, source, kind, index=None, role=None):
    key = _caption_key(snapshot, source, kind)
    if kind == 'images':
        options = caption_images(snapshot, source)
        root = PRODUCTS_DIR.resolve()
        if role not in (None, 'product'):
            raise ValueError('产品图的用途必须是商品实拍')
        role = 'product'
        if index is None:
            chosen = options[:MAX_IMAGES]
        elif type(index) is int and 0 <= index < len(options):
            chosen = [options[index]]
        else:
            raise ValueError('请选择当前记录里的有效图片')
    else:
        options = {item['index']: item['path'] for item in caption_videos(source)}
        root = SOURCES_DIR.resolve()
        role = 'reference' if role is None else role
        if role not in ('product', 'reference'):
            raise ValueError('请选择商品实拍或创意参考')
        if role == 'product' and not _product(snapshot, source):
            raise ValueError('商品实拍需匹配唯一的产品记录')
        if index is None:
            index = next(iter(options), None)
        if type(index) is not int or index not in options:
            raise ValueError('请选择当前记录里的有效视频')
        chosen = [options[index]]
    if not chosen:
        raise ValueError('这条记录没有可识别的素材')
    paths, assets = [], []
    for value in chosen:
        if not isinstance(value, str) or not value:
            raise ValueError('素材路径格式无效')
        path = resolve(value).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError('素材文件不存在或不在本项目素材目录，请重新同步或上传')
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError('素材已变化，请重新选择') from exc
        paths.append(path)
        assets.append([str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
    product = _product(snapshot, source)
    binding = [key, kind, index, role, source.get('keyword', ''),
               product.get('record_id') if product else None, assets]
    fingerprint = hashlib.sha256(json.dumps(binding, ensure_ascii=False).encode()).hexdigest()
    return dict(key=key, kind=kind, paths=paths, fingerprint=fingerprint, index=index, role=role)


def caption_entry(snapshot, target):
    records = snapshot.get('captions') or {}
    entry = records.get(target['key']) if isinstance(records, dict) else None
    return entry if isinstance(entry, dict) else None


def put_caption(snapshot, target, text, enabled=True, edited=False):
    if not isinstance(text, str) or len(text.strip()) > MAX_DESCRIPTION:
        raise ValueError(f'识别描述最多 {MAX_DESCRIPTION} 字')
    if type(enabled) is not bool:
        raise ValueError('带入脚本的选项必须是布尔值')
    entry = {k: target[k] for k in ('fingerprint', 'index', 'role')}
    entry.update(text=text.strip(), enabled=enabled, edited=edited, revision=uuid.uuid4().hex)
    snapshot.setdefault('captions', {})[target['key']] = entry
    return entry


def row_captions(snapshot, source):
    results = {}
    for kind in ('images', 'video'):
        try:
            key = _caption_key(snapshot, source, kind)
        except ValueError:
            results[kind] = None
            continue
        entry = caption_entry(snapshot, {'key': key})
        if entry is None:
            results[kind] = None
            continue
        try:
            target = caption_target(snapshot, source, kind, entry.get('index'), entry.get('role'))
            stale = target['fingerprint'] != entry.get('fingerprint')
        except (OSError, ValueError):
            stale = True
        results[kind] = dict(entry, stale=stale)
    return results


def apply_caption_context(instruction, snapshot, source):
    details = {}
    for kind, entry in row_captions(snapshot, source).items():
        if not entry or entry['stale'] or not entry.get('enabled') or not entry.get('text'):
            continue
        label = ('产品图片观察' if kind == 'images' else
                 '商品实拍观察' if entry.get('role') == 'product' else '创意视频参考')
        details[label] = entry['text']
    if not details:
        return instruction
    product = _product(snapshot, source)
    if product and product.get('prompt_text'):
        details['飞书商品资料'] = product['prompt_text']
    reference = {
        'version': _CONTEXT_VERSION,
        '用途': '以下内容仅为参考资料。执行上方原指令。商品观察用于当前款式，创意参考只用于镜头和节奏。规格仅引用明确填写的资料，保留不确定信息。',
        '资料': details,
    }
    return instruction + _CAPTION_SECTION + json.dumps(reference, ensure_ascii=False)


def script_instruction(message):
    """Use the original command for count and duration, never caption text."""
    original, separator, reference = message.rpartition(_CAPTION_SECTION)
    if separator:
        try:
            parsed = json.loads(reference)
        except ValueError:
            return message
        if (isinstance(parsed, dict) and parsed.get('version') == _CONTEXT_VERSION
                and isinstance(parsed.get('资料'), dict)):
            return original
    return message
