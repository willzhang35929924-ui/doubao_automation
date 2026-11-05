"""飞书开放平台客户端：tenant_access_token、多维表格记录、附件下载、固定指令读取。

需要在飞书开放平台创建自建应用，开通权限（并创建/发布版本）：
  - bitable:app:readonly （查看多维表格）
  - drive:drive:readonly （下载云空间文件/附件）
然后把应用添加为目标 Base 的协作者（可阅读即可）。
"""
import re
import hashlib
import json
import logging
import os
import socket
import ssl
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import requests

from .config import get
from .paths import LOGS_DIR, PRODUCTS_DIR, ROOT, SOURCES_DIR, resolve, safe_filename


class FeishuError(Exception):
    """Safe public diagnostic; never includes credentials or attachment URLs."""

    def __init__(self, message, *, operation=None, http_status=None, api_code=None,
                 request_id=None, retryable=False):
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status
        self.api_code = api_code
        self.request_id = request_id
        self.retryable = retryable


_LOG = logging.getLogger(__name__)
_ATTEMPTS = 3
_RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))


def _event(event, **fields):
    """Persist only explicitly selected diagnostic fields, never request data."""
    allowed = ('operation', 'attempt', 'http_status', 'api_code', 'request_id', 'error_class')
    record = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), event=event,
                  **{key: fields[key] for key in allowed if key in fields})
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with (LOGS_DIR/'feishu_sync.log').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False)+'\n')
    except OSError:
        _LOG.warning('无法写入飞书网络诊断日志')


class FeishuClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("feishu", {}) or {}
        self.base = str(self.cfg.get("base_url", "https://open.feishu.cn")).rstrip("/")
        self.app_id = str(self.cfg.get("app_id") or "").strip()
        self.app_secret = str(self.cfg.get("app_secret") or "").strip()
        self.app_token = str(self.cfg.get("app_token") or "").strip()
        self._token = ""
        self._token_expire = 0.0
        # Counts describe the fetched records before dashboard-only hiding or
        # keyword matching, so callers can explain where rows were filtered.
        self.sync_stats = {'products': {}, 'sources': {}}
        if not self.app_id or not self.app_secret:
            raise FeishuError(
                "缺少飞书凭证：请在 config/config.yaml 的 feishu.app_id / feishu.app_secret 填入"
                "自建应用的 App ID 与 App Secret（应用需开通多维表格读取与云空间文件下载权限，"
                "并被添加为该 Base 的协作者）。"
            )
        if not self.app_token:
            raise FeishuError("缺少 feishu.app_token（多维表格链接 /base/ 后面那段）。")
        # Reuse verified TLS connections throughout a sync rather than opening
        # a new handshake for each field, record page and attachment.
        self.session = requests.Session()

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _request_id(response):
        value = response.headers.get('X-Tt-Logid') or response.headers.get('X-Request-Id') or ''
        return value if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(value)) else None

    def _response_error(self, operation, response, data=None):
        code = data.get('code') if isinstance(data, dict) else None
        if not isinstance(code, int) and not (isinstance(code, str) and re.fullmatch(r'-?\d{1,16}', code)):
            code = None
        request_id = self._request_id(response)
        parts = [f'{operation}失败', f'HTTP {response.status_code}']
        if code is not None:
            parts.append(f'飞书错误码 {code}')
        if request_id:
            parts.append(f'请求编号 {request_id}')
        if operation == '下载附件' and response.status_code in (400, 403, 404):
            parts.append('请核对附件仍存在及应用的附件读取权限；多维表格高级权限需保留附件授权参数')
        error = FeishuError('；'.join(parts), operation=operation, http_status=response.status_code,
                            api_code=code, request_id=request_id,
                            retryable=response.status_code in _RETRY_STATUSES)
        _event('response_error', operation=operation, http_status=response.status_code,
               api_code=code, request_id=request_id)
        return error

    @staticmethod
    def _certificate_failure(exc):
        text = str(exc).lower()
        return (isinstance(exc, ssl.SSLCertVerificationError)
                or 'certificate_verify_failed' in text or 'certificate verify failed' in text
                or 'hostname mismatch' in text)

    @classmethod
    def _retryable_transport(cls, exc, *, auth=False):
        if isinstance(exc, requests.exceptions.SSLError):
            if cls._certificate_failure(exc):
                return False
            text = str(exc).lower()
            # Restrict authentication retry to this known transient TLS EOF,
            # not arbitrary TLS errors or POST response/read failures.
            return 'unexpected_eof_while_reading' in text or 'eof occurred in violation of protocol' in text
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        if not auth:
            return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                                    requests.exceptions.ChunkedEncodingError,
                                    requests.exceptions.ContentDecodingError))
        # Connection creation/DNS errors happen before the authentication body
        # can be sent. Other authentication POST failures are not replayed.
        seen = set()
        pending = [exc]
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, (socket.gaierror, ConnectionRefusedError)):
                return True
            if type(value).__name__ in ('NewConnectionError', 'NameResolutionError'):
                return True
            if isinstance(value, BaseException):
                pending.extend(v for v in (*value.args, value.__cause__, value.__context__)
                               if isinstance(v, BaseException))
        return False

    @classmethod
    def _transport_error(cls, operation, exc, attempts, *, auth=False):
        if isinstance(exc, requests.exceptions.SSLError):
            reason = ('TLS 证书验证失败，请检查系统时间与可信证书；不会跳过证书验证'
                      if cls._certificate_failure(exc) else
                      'TLS 连接被中断，请检查本机代理节点和网络；证书校验保持开启')
        elif isinstance(exc, requests.exceptions.Timeout):
            reason = '连接或读取超时，请检查网络后重试'
        else:
            reason = '网络连接中断，请检查本机代理和网络后重试'
        _event('transport_error', operation=operation, attempt=attempts, error_class=type(exc).__name__)
        return FeishuError(f'{operation}失败：{reason}（已尝试 {attempts} 次）', operation=operation,
                           retryable=cls._retryable_transport(exc, auth=auth))

    @staticmethod
    def _retry_delay(attempt, response=None):
        delay = min(0.5 * (2 ** (attempt - 1)), 2)
        if response is not None:
            value = response.headers.get('Retry-After', '')
            if str(value).isdigit():
                delay = max(delay, min(float(value), 5))
        time.sleep(delay)

    def _request(self, method, path, operation, *, auth=False, attempts=_ATTEMPTS, **kwargs):
        """Bounded retries only for reads and this credential-retrieval POST."""
        kwargs.setdefault('timeout', (10, 45))
        # No redirect may forward a credential or token from this API host.
        kwargs.setdefault('allow_redirects', False)
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(method, self.base + path, **kwargs)
            except requests.RequestException as exc:
                if attempt < attempts and self._retryable_transport(exc, auth=auth):
                    _event('retry', operation=operation, attempt=attempt, error_class=type(exc).__name__)
                    self._retry_delay(attempt)
                    continue
                raise self._transport_error(operation, exc, attempt, auth=auth) from None
            if method == 'GET' and response.status_code in _RETRY_STATUSES and attempt < attempts:
                _event('retry', operation=operation, attempt=attempt, http_status=response.status_code,
                       request_id=self._request_id(response))
                response.close()
                self._retry_delay(attempt, response)
                continue
            return response

    def _json(self, method, path, operation, **kwargs):
        with self._request(method, path, operation, **kwargs) as response:
            try:
                data = response.json()
            except ValueError:
                if response.status_code >= 400:
                    raise self._response_error(operation, response) from None
                _event('invalid_response', operation=operation, http_status=response.status_code,
                       request_id=self._request_id(response))
                raise FeishuError(f'{operation}失败：飞书返回非 JSON 响应（HTTP {response.status_code}）',
                                   operation=operation, http_status=response.status_code,
                                   request_id=self._request_id(response)) from None
            if not isinstance(data, dict) or not 200 <= response.status_code < 300 or data.get('code') != 0:
                raise self._response_error(operation, response, data)
            _event('success', operation=operation, http_status=response.status_code,
                   request_id=self._request_id(response))
            return data

    def validate_sync_config(self):
        """Check local prerequisites before requesting or downloading any data."""
        for key, label in (("products_table", "产品图表"), ("scripts_table", "提示词/视频表")):
            table = self.cfg.get(key) or {}
            if not isinstance(table, dict) or not str(table.get("table_id") or "").strip():
                raise FeishuError(f"未配置{label}的表格 ID：请填写 feishu.{key}.table_id。")
        spec = self.cfg.get("fixed_prompt") or {}
        if not isinstance(spec, dict):
            raise FeishuError("飞书固定提示词配置格式错误：feishu.fixed_prompt 应为配置项。")
        source = str(spec.get("source", "file"))
        if source == "file":
            p = resolve(spec.get("file", "config/gemini_prompt.txt"))
            if not p.is_file():
                raise FeishuError("未找到固定提示词文件。请在本机创建 config/gemini_prompt.txt，或配置 feishu.fixed_prompt 使用飞书表格/文档。")
        elif source == "bitable":
            if not str(spec.get("table_id") or "").strip():
                raise FeishuError("固定提示词使用飞书表格时，请填写 feishu.fixed_prompt.table_id。")
        elif source == "docx":
            if not str(spec.get("document_id") or "").strip():
                raise FeishuError("固定提示词使用飞书文档时，请填写 feishu.fixed_prompt.document_id。")
        else:
            raise FeishuError("未知的固定提示词来源，请使用 file、bitable 或 docx。")

    def validate_sync_config(self):
        """Check local prerequisites before requesting or downloading any data."""
        for key, label in (("products_table", "产品图表"), ("scripts_table", "提示词/视频表")):
            table = self.cfg.get(key) or {}
            if not isinstance(table, dict) or not str(table.get("table_id") or "").strip():
                raise FeishuError(f"未配置{label}的表格 ID：请填写 feishu.{key}.table_id。")
        spec = self.cfg.get("fixed_prompt") or {}
        if not isinstance(spec, dict):
            raise FeishuError("飞书固定提示词配置格式错误：feishu.fixed_prompt 应为配置项。")
        source = str(spec.get("source", "file"))
        if source == "file":
            p = resolve(spec.get("file", "config/gemini_prompt.txt"))
            if not p.is_file():
                raise FeishuError("未找到固定提示词文件。请在本机创建 config/gemini_prompt.txt，或配置 feishu.fixed_prompt 使用飞书表格/文档。")
        elif source == "bitable":
            if not str(spec.get("table_id") or "").strip():
                raise FeishuError("固定提示词使用飞书表格时，请填写 feishu.fixed_prompt.table_id。")
        elif source == "docx":
            if not str(spec.get("document_id") or "").strip():
                raise FeishuError("固定提示词使用飞书文档时，请填写 feishu.fixed_prompt.document_id。")
        else:
            raise FeishuError("未知的固定提示词来源，请使用 file、bitable 或 docx。")

    # ---- 认证 ----
    def token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        data = self._json(
            'POST', '/open-apis/auth/v3/tenant_access_token/internal', '飞书认证', auth=True,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=(10, 30),
        )
        if not isinstance(data.get('tenant_access_token'), str) or not data['tenant_access_token']:
            raise FeishuError('飞书认证失败：成功响应中缺少访问令牌', operation='飞书认证')
        self._token = data['tenant_access_token']
        self._token_expire = time.time() + float(data.get("expire", 7200))
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    # ---- 多维表格 ----
    def list_records(self, table_id: str, page_size: int = 100) -> list:
        records, page_token, seen_tokens = [], "", set()
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._json(
                'GET', f"/open-apis/bitable/v1/apps/{quote(self.app_token, safe='')}/tables/{quote(table_id, safe='')}/records",
                '读取多维表格记录', headers=self._headers(), params=params,
            )
            payload = data.get("data", {})
            records.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return records
            next_token = payload.get("page_token")
            if not isinstance(next_token, str) or not next_token.strip() or next_token in seen_tokens:
                raise FeishuError('读取多维表格记录失败：分页标记缺失或重复，已停止同步以避免漏导入记录',
                                  operation='读取多维表格记录')
            seen_tokens.add(next_token)
            page_token = next_token

    @staticmethod
    def field_text(value) -> str:
        """多维表格文本列可能是 str 或分段 list[dict]，统一转纯文本。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for seg in value:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text", "")))
                else:
                    parts.append(str(seg))
            return "".join(parts)
        if isinstance(value, dict):
            return str(value.get("text", value))
        return str(value)

    @staticmethod
    def field_attachments(value) -> list:
        """附件列 → [{'file_token','name','size',...}]。"""
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict) and x.get("file_token")]
        return []

    def _attachment_params(self, attachment):
        """Keep the Bitable authorization context, never trust a metadata URL as a target."""
        if not attachment:
            return {}
        base = urlsplit(self.base)
        for key in ('url', 'tmp_url'):
            value = attachment.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                url = urlsplit(value)
                trusted = (url.scheme == 'https' and url.hostname == base.hostname
                           and (url.port or 443) == (base.port or 443)
                           and not url.username and not url.password)
                if not trusted:
                    continue
                values = parse_qs(url.query, max_num_fields=30).get('extra', [])
            except (ValueError, TypeError):
                continue
            if len(values) == 1 and values[0] and len(values[0]) <= 65536:
                return {'extra': values[0]}
        return {}

    def _media_response(self, path, params, headers):
        """Follow only HTTPS download redirects, without leaking Bearer across origins."""
        url = self.base + path
        for hop in range(4):
            response = self.session.request('GET', url, params=params, headers=headers,
                                            stream=True, timeout=(10, 60), allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get('Location', '')
            following = urljoin(url, location)
            source, target = urlsplit(url), urlsplit(following)
            if (hop == 3 or not location or target.scheme != 'https' or not target.hostname
                    or target.username or target.password):
                error = self._response_error('下载附件', response)
                response.close()
                raise error
            if (source.hostname, source.port or 443) != (target.hostname, target.port or 443):
                headers = {}  # Credential stays at the API origin, never at a CDN/other host.
            response.close()
            url, params = following, None

    def download_media(self, file_token: str, dest: Path, attachment=None) -> Path:
        if not isinstance(file_token, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', file_token):
            raise FeishuError('下载附件失败：附件标识格式错误，请重新同步最新的表格记录', operation='下载附件')
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        params = self._attachment_params(attachment)
        try:
            expected = int((attachment or {}).get('size') or 0)
        except (ValueError, TypeError):
            expected = 0
        path = f'/open-apis/drive/v1/medias/{file_token}/download'
        for attempt in range(1, _ATTEMPTS + 1):
            temporary = None
            try:
                with self._media_response(path, params, self._headers()) as response:
                    # This is a complete download with no Range request. A 206
                    # response must never silently publish only part of a file.
                    if response.status_code != 200:
                        if response.status_code in _RETRY_STATUSES and attempt < _ATTEMPTS:
                            _event('retry', operation='下载附件', attempt=attempt,
                                   http_status=response.status_code, request_id=self._request_id(response))
                            response.close()
                            self._retry_delay(attempt, response)
                            continue
                        try:
                            data = response.json()
                        except ValueError:
                            data = None
                        raise self._response_error('下载附件', response, data)
                    if 'text/html' in response.headers.get('Content-Type', '').lower():
                        raise self._invalid_media_response(response)
                    if 'json' in response.headers.get('Content-Type', '').lower():
                        try:
                            data = response.json()
                        except ValueError:
                            data = None
                        raise self._response_error('下载附件', response, data)
                    fd, name = tempfile.mkstemp(prefix='.' + dest.name + '.', suffix='.part', dir=dest.parent)
                    temporary = Path(name)
                    length = 0
                    with os.fdopen(fd, 'wb') as handle:
                        for chunk in response.iter_content(chunk_size=1 << 16):
                            if chunk:
                                handle.write(chunk)
                                length += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    advertised = response.headers.get('Content-Length', '')
                    wire_length = int(advertised) if str(advertised).isdigit() else 0
                    encoded = response.headers.get('Content-Encoding', '').lower() not in ('', 'identity')
                    if (not length or (expected > 0 and length != expected)
                            or (wire_length > 0 and not encoded and length != wire_length)):
                        raise requests.exceptions.ChunkedEncodingError('附件传输内容不完整')
                    with temporary.open('rb') as handle:
                        prefix = handle.read(512).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
                    if prefix.startswith((b'<!doctype html', b'<html', b'<head', b'<body')):
                        raise self._invalid_media_response(response)
                os.replace(temporary, dest)
                temporary = None
                _event('success', operation='下载附件', http_status=response.status_code,
                       request_id=self._request_id(response))
                return dest
            except requests.RequestException as exc:
                if attempt < _ATTEMPTS and self._retryable_transport(exc):
                    _event('retry', operation='下载附件', attempt=attempt, error_class=type(exc).__name__)
                    self._retry_delay(attempt)
                    continue
                raise self._transport_error('下载附件', exc, attempt) from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _invalid_media_response(self, response):
        request_id = self._request_id(response)
        _event('invalid_media', operation='下载附件', http_status=response.status_code,
               request_id=request_id)
        suffix = f'；请求编号 {request_id}' if request_id else ''
        return FeishuError('下载附件失败：服务器返回 HTML 页面，未覆盖本地附件；请检查代理链路或下载权限' + suffix,
                           operation='下载附件', http_status=response.status_code, request_id=request_id)

    def _require_field(self, table_id: str, field: str, what: str) -> str:
        """校验列名存在；不存在时抛出带可用列清单的错误，便于快速改配置。"""
        names = [f.get("field_name", "") for f in self.list_fields(table_id)]
        if field not in names:
            raise FeishuError(f"表 {table_id} 找不到{what}列「{field}」，可用列: {names}")
        return field

    # ---- 业务封装 ----
    @staticmethod
    def _attachment_filename(attachment):
        """Keep same-named attachments distinct without exposing file tokens."""
        token = str(attachment['file_token'])
        name = attachment.get('name') or 'attachment'
        if name == token:
            name = 'attachment'
        cleaned = Path(safe_filename(name))
        # Reserve space for the full digest and atomic-download temp suffix.
        stem = cleaned.stem.encode('utf-8')[:90].decode('utf-8', errors='ignore') or 'attachment'
        suffix = cleaned.suffix.encode('utf-8')[:16].decode('utf-8', errors='ignore')
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        return f'{stem}_{digest}{suffix}'

    def fetch_products(self) -> list:
        """产品图表 → [{'record_id','name','prompt_text','images':[Path]}]"""
        tcfg = self.cfg.get("products_table", {}) or {}
        table_id = tcfg.get("table_id", "")
        image_field = self._require_field(table_id, tcfg.get("image_field", "产品图"), "产品图")
        name_field = (tcfg.get("name_field") or "").strip()
        prompt_field = tcfg.get("prompt_field") or "提示词"
        keyword_field = self._require_field(table_id, tcfg.get("keyword_field", "keyword"), "keyword")
        out = []
        records = self.list_records(table_id)
        stats = dict(remote_records=len(records), imported_records=0, skipped_no_images=0,
                     attachments=0, downloaded_attachments=0)
        self.sync_stats['products'] = stats
        for rec in records:
            rid = rec.get("record_id", "")
            fields = rec.get("fields", {}) or {}
            keyword = self.field_text(fields.get(keyword_field)).strip()
            atts = self.field_attachments(fields.get(image_field))
            stats['attachments'] += len(atts)
            if not atts:
                stats['skipped_no_images'] += 1
                print(f"[飞书] 产品表记录 {rid} 的「{image_field}」列没有附件，跳过")
                continue
            name = self.field_text(fields.get(name_field)) if name_field else ""
            # Optional product instructions are preserved for display; they do
            # not replace the source prompt used by the generation pipeline.
            prompt_text = self.field_text(fields.get(prompt_field))
            rec_dir = PRODUCTS_DIR / safe_filename(rid)
            images = []
            for att in atts:
                fname = self._attachment_filename(att)
                images.append(self.download_media(att["file_token"], rec_dir / fname, attachment=att))
                stats['downloaded_attachments'] += 1
            out.append({"record_id": rid, "name": name or rid, "keyword": keyword,
                        "prompt_text": prompt_text, "images": [str(p) for p in images]})
            stats['imported_records'] += 1
            print(f"[飞书] 产品「{name or rid}」下载 {len(images)} 张图片")
        return out

    def fetch_sources(self) -> list:
        """提示词/视频表 → [{'record_id','prompt_text','videos':[Path]}]"""
        tcfg = self.cfg.get("scripts_table", {}) or {}
        table_id = tcfg.get("table_id", "")
        prompt_field = self._require_field(table_id, tcfg.get("prompt_field", "提示词"), "提示词")
        video_field = self._require_field(table_id, tcfg.get("video_field", "视频"), "视频")
        max_mb = float(self.cfg.get("max_video_size_mb", 200))
        out = []
        records = self.list_records(table_id)
        stats = dict(remote_records=len(records), imported_records=0, skipped_empty=0,
                     skipped_oversized_only=0, oversized_attachments=0,
                     attachments=0, downloaded_attachments=0)
        self.sync_stats['sources'] = stats
        for rec in records:
            rid = rec.get("record_id", "")
            fields = rec.get("fields", {}) or {}
            prompt_text = self.field_text(fields.get(prompt_field)).strip()
            atts = self.field_attachments(fields.get(video_field))
            stats['attachments'] += len(atts)
            videos = []
            for att in atts:
                size_mb = float(att.get("size", 0)) / 1048576
                if max_mb and size_mb > max_mb:
                    stats['oversized_attachments'] += 1
                    print(f"[飞书] ⚠ 视频 {att.get('name')} 有 {size_mb:.0f}MB，超过上限 {max_mb:.0f}MB，跳过")
                    continue
                fname = self._attachment_filename(att)
                videos.append(str(self.download_media(att["file_token"], SOURCES_DIR / rid / fname, attachment=att)))
                stats['downloaded_attachments'] += 1
            if not prompt_text and not videos:
                stats['skipped_oversized_only' if atts else 'skipped_empty'] += 1
                print(f"[飞书] 记录 {rid} 既无提示词也无视频，跳过")
                continue
            keyword_field = self.cfg.get("scripts_table", {}).get("keyword_field", "keyword")
            keyword = self.field_text(fields.get(keyword_field)).strip()
            out.append({"record_id": rid, "keyword": keyword, "prompt_text": prompt_text, "videos": videos})
            stats['imported_records'] += 1
            print(f"[飞书] 源记录 {rid}: 提示词 {len(prompt_text)} 字, 视频 {len(videos)} 个")
        return out

    def list_fields(self, table_id: str) -> list:
        """列出表的字段（顺序与多维表格列顺序一致）。"""
        data = self._json(
            'GET', f"/open-apis/bitable/v1/apps/{quote(self.app_token, safe='')}/tables/{quote(table_id, safe='')}/fields",
            '读取多维表格字段', headers=self._headers(), params={"page_size": 100},
        )
        return data.get("data", {}).get("items") or []

    def load_fixed_prompt(self) -> str:
        """读取固定提示词指令，来源：file | bitable | docx。
        bitable 模式：field 留空时按 field_index（0 起算）自动解析列名；
        record_index=-1 时扫描所有行取该列第一个非空单元格。"""
        spec = self.cfg.get("fixed_prompt", {}) or {}
        source = str(spec.get("source", "file"))
        if source == "file":
            p = resolve(spec.get("file", "config/gemini_prompt.txt"))
            if not p.exists():
                raise FeishuError(f"固定指令文件不存在: {p}")
            return p.read_text(encoding="utf-8").strip()
        if source == "bitable":
            table_id = str(spec.get("table_id", ""))
            field = str(spec.get("field", "") or "").strip()
            if not field:
                idx = int(spec.get("field_index", 0) or 0)
                fields = self.list_fields(table_id)
                names = [f.get("field_name", "") for f in fields]
                if idx >= len(names):
                    raise FeishuError(f"表 {table_id} 只有 {len(names)} 列（{names}），取不到第 {idx + 1} 列")
                field = names[idx]
                print(f"[飞书] 固定指令列（第 {idx + 1} 列）解析为: 「{field}」")
            recs = self.list_records(table_id)
            rec_idx = int(spec.get("record_index", -1))
            if rec_idx >= 0:
                if rec_idx >= len(recs):
                    raise FeishuError(f"固定指令表记录不足（需要第 {rec_idx} 条，共 {len(recs)} 条）")
                text = self.field_text(recs[rec_idx].get("fields", {}).get(field)).strip()
            else:
                text = ""
                for rec in recs:
                    t = self.field_text(rec.get("fields", {}).get(field)).strip()
                    if t:
                        text = t
                        break
            if not text:
                raise FeishuError(f"表 {table_id} 的「{field}」列在所有记录中都为空")
            return text
        if source == "docx":
            doc_id = str(spec.get("document_id", ""))
            data = self._json(
                'GET', f"/open-apis/docx/v1/documents/{quote(doc_id, safe='')}/raw_content",
                '读取飞书文档', headers=self._headers(),
            )
            return str(data.get("data", {}).get("content", "")).strip()
        raise FeishuError(f"未知的固定指令来源: {source}")
