"""Read bounded product evidence from a public page without executing it."""

import contextlib
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies


MAX_PAGE_URL_LENGTH = 2048
_MAX_BYTES = 1024 * 1024
_MAX_TEXT = 6400
_TIMEOUT = 15
_MAX_REDIRECTS = 4
_DNS_SLOTS = threading.BoundedSemaphore(2)
_REDIRECTS = {301, 302, 303, 307, 308}
_PRIVATE_SUFFIXES = (
    '.localhost', '.local', '.localdomain', '.internal', '.lan',
    '.home.arpa', '.test', '.invalid', '.example', '.onion', '.home', '.corp',
)
_HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_V4_SPECIAL = tuple(ipaddress.ip_network(value) for value in ('192.0.0.0/24', '192.88.99.0/24'))
_V6_GLOBAL = ipaddress.ip_network('2000::/3')
_V6_TRANSITION = tuple(ipaddress.ip_network(value) for value in ('2002::/16', '2001::/32'))
_SYNTHETIC_DNS = ipaddress.ip_network('198.18.0.0/15')
_DOH_HOST = 'cloudflare-dns.com'
_DOH_ADDRESS = '1.1.1.1'
_MAX_DOH_BYTES = 32 * 1024
_TIMEOUT_ERROR = '读取商品网页超时，请稍后重试。'
_PUBLIC_ADDRESS_ERROR = '只能读取公开的商品网页，不能访问本机、内网或保留地址。'


class CaptionPageError(ValueError):
    """A page error safe to display without URL credentials or remote content."""

class _SyntheticDns(CaptionPageError):
    """The system resolver supplied only synthetic VPN destinations."""



@dataclass(frozen=True)
class ProductPage:
    url: str
    title: str
    text: str


def _is_public_ip(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 6:
        return address in _V6_GLOBAL and not any(address in network for network in _V6_TRANSITION)
    return not any(address in network for network in _V4_SPECIAL)


def normalize_page_url(url: str) -> str:
    """Validate URL syntax and literal addresses without making a DNS request."""
    invalid = '请输入完整的公开 HTTP 或 HTTPS 商品链接。'
    if not isinstance(url, str):
        raise CaptionPageError(invalid)
    value = url.strip()
    if (not value or len(value) > MAX_PAGE_URL_LENGTH or '\\' in value
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)):
        raise CaptionPageError(invalid)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        standard_port = {'http': 80, 'https': 443}.get(parsed.scheme)
        if (not host or not standard_port or parsed.username is not None
                or parsed.password is not None or port not in (None, standard_port)
                or parsed.netloc.endswith(':') or '%' in host
                or ('[' in parsed.netloc or ']' in parsed.netloc)
                and not re.fullmatch(r'\[[^\]]+\](?::[0-9]+)?', parsed.netloc)):
            raise ValueError
        host = host.removesuffix('.').encode('idna').decode('ascii').lower()
    except (ValueError, UnicodeError):
        raise CaptionPageError(invalid) from None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or '.' not in host or host.endswith(_PRIVATE_SUFFIXES)
                or re.fullmatch(r'[0-9.]+', host)
                or not all(_HOST_LABEL.fullmatch(label) for label in host.split('.'))):
            raise CaptionPageError(_PUBLIC_ADDRESS_ERROR) from None
        if parsed.netloc.startswith('['):
            raise CaptionPageError(invalid)
        authority = host
    else:
        if not _is_public_ip(ip):
            raise CaptionPageError(_PUBLIC_ADDRESS_ERROR)
        if parsed.netloc.startswith('[') != (ip.version == 6):
            raise CaptionPageError(invalid)
        authority = f'[{ip}]' if ip.version == 6 else str(ip)
    try:
        path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
    except UnicodeError:
        raise CaptionPageError(invalid) from None
    # HTTP origin-form must begin with a single slash.
    if path.startswith('//'):
        raise CaptionPageError(invalid)
    normalized = urlunsplit((parsed.scheme, authority, path, query, ''))
    if len(normalized) > MAX_PAGE_URL_LENGTH:
        raise CaptionPageError(invalid)
    return normalized


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise CaptionPageError(_TIMEOUT_ERROR)
    return seconds


def _resolve_public_addresses(host, port, deadline):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _is_public_ip(ip):
            raise CaptionPageError(_PUBLIC_ADDRESS_ERROR)
        if ip.version == 4:
            return [(socket.AF_INET, (str(ip), port))]
        return [(socket.AF_INET6, (str(ip), port, 0, 0))]
    # getaddrinfo has no caller-controlled timeout. A bounded daemon lookup
    # leaves no path for a stalled resolver to block the workbench or its exit.
    result = queue.Queue(maxsize=1)
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise CaptionPageError(_TIMEOUT_ERROR)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host + '.', port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
        except OSError:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=lookup, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise CaptionPageError('暂时无法解析商品网页地址。') from None
    try:
        answers = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise CaptionPageError(_TIMEOUT_ERROR) from None
    if not answers:
        raise CaptionPageError('无法解析商品网页地址，请检查链接或网络。')
    if all(family == socket.AF_INET and ipaddress.ip_address(sockaddr[0]) in _SYNTHETIC_DNS
           for family, _, _, _, sockaddr in answers):
        raise _SyntheticDns(_PUBLIC_ADDRESS_ERROR)
    addresses = []
    for family, _, _, _, sockaddr in answers:
        ip = ipaddress.ip_address(sockaddr[0])
        if (not _is_public_ip(ip) or (family == socket.AF_INET6 and sockaddr[3])
                or family != (socket.AF_INET if ip.version == 4 else socket.AF_INET6)):
            raise CaptionPageError(_PUBLIC_ADDRESS_ERROR)
        # Build our own canonical numeric sockaddr. The dial never resolves
        # the supplied hostname again, even if the DNS answer later changes.
        address = (str(ip), port) if ip.version == 4 else (str(ip), port, 0, 0)
        if (family, address) not in addresses:
            addresses.append((family, address))
    return addresses


def _system_loopback_proxy(scheme):
    try:
        proxies = getproxies()
        value = proxies.get(scheme) or proxies.get('http') or proxies.get('https') or ''
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
        if (parsed.scheme != 'http' or not address.is_loopback or not port
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or '%' in (parsed.hostname or '')):
            raise ValueError
    except (OSError, ValueError):
        return None
    if address.version == 4:
        return socket.AF_INET, (str(address), port)
    return socket.AF_INET6, (str(address), port, 0, 0)


def _dial(addresses, tls_name, deadline, *, proxy=None):
    context = ssl.create_default_context() if tls_name else None
    for family, address in addresses:
        dial_family, dial_address = proxy if proxy else (family, address)
        sock = socket.socket(dial_family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            sock.settimeout(min(5, _remaining(deadline)))
            sock.connect(dial_address)
            if proxy:
                _proxy_connect(sock, address, deadline)
            if context is not None:
                sock.settimeout(min(6, _remaining(deadline)))
                sock = context.wrap_socket(sock, server_hostname=tls_name)
            return sock
        except (CaptionPageError, ssl.SSLError):
            sock.close()
            raise
        except (OSError, http.client.HTTPException):
            sock.close()
    _remaining(deadline)
    if proxy:
        raise CaptionPageError('本机 HTTP 代理无法连接公开的商品网页地址。')
    raise CaptionPageError('无法连接商品网页，请检查地址或网络。')


def _abort_socket(sock):
    with contextlib.suppress(OSError):
        # Only interrupt the descriptor. SSLSocket.shutdown also resets its
        # SSL state, which another thread may still be reading.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


@contextlib.contextmanager
def _socket_deadline(sock, deadline):
    remaining = _remaining(deadline)
    sock.settimeout(remaining)
    timer = threading.Timer(remaining, _abort_socket, args=(sock,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _proxy_connect(sock, address, deadline):
    host, port = address[:2]
    if ':' in host:
        host = f'[{host}]'
    authority = f'{host}:{port}'
    with _socket_deadline(sock, deadline):
        sock.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'))
        with contextlib.closing(http.client.HTTPResponse(sock, method='CONNECT')) as response:
            response.begin()
            if response.status != 200:
                raise OSError('proxy CONNECT failed')


@contextlib.contextmanager
def _http_get(host, authority, port, tls, target, addresses, deadline, accept, *, proxy=None):
    sock = (_dial(addresses, host if tls else None, deadline, proxy=proxy) if proxy
            else _dial(addresses, host if tls else None, deadline))
    conn = http.client.HTTPConnection(host, port)
    conn.sock = sock
    try:
        with _socket_deadline(sock, deadline):
            conn.request('GET', target, headers={
                'Host': authority,
                'User-Agent': 'DoubaoStudio/1.0 (public product page reader)',
                'Accept': accept,
                'Accept-Encoding': 'identity',
                'Connection': 'close',
            })
            with conn.getresponse() as response:
                yield response
    finally:
        conn.close()


def _resolve_with_doh(host, port, proxy, deadline):
    addresses = []
    for record_name, record_type in (('A', 1), ('AAAA', 28)):
        try:
            target = f'/dns-query?name={host}&type={record_name}'
            endpoint = [(socket.AF_INET, (_DOH_ADDRESS, 443))]
            with _http_get(_DOH_HOST, _DOH_HOST, 443, True, target, endpoint, deadline,
                           'application/dns-json', proxy=proxy) as response:
                if (response.status != 200
                        or response.headers.get_content_type() != 'application/dns-json'
                        or response.length is not None and response.length > _MAX_DOH_BYTES):
                    raise ValueError
                raw = response.read(_MAX_DOH_BYTES + 1)
            _remaining(deadline)
            if len(raw) > _MAX_DOH_BYTES:
                raise ValueError
            decoded = json.loads(raw)
            if decoded['Status'] != 0 or decoded.get('TC'):
                raise ValueError
            question, = decoded['Question']
            if question['name'].rstrip('.').lower() != host or question['type'] != record_type:
                raise ValueError
            for answer in decoded.get('Answer', []):
                kind = answer['type']
                if kind not in (1, 28):
                    continue
                ip = ipaddress.ip_address(answer['data'])
                if not _is_public_ip(ip):
                    raise CaptionPageError(_PUBLIC_ADDRESS_ERROR)
                if kind != record_type or ip.version != (4 if kind == 1 else 6):
                    raise ValueError
                address = ((socket.AF_INET, (str(ip), port)) if ip.version == 4
                           else (socket.AF_INET6, (str(ip), port, 0, 0)))
                if address not in addresses:
                    addresses.append(address)
        except CaptionPageError:
            raise
        except (OSError, http.client.HTTPException, ValueError, AttributeError, KeyError, TypeError, RecursionError):
            _remaining(deadline)
            raise CaptionPageError('无法通过本机代理安全解析商品网页地址。') from None
    if not addresses:
        raise CaptionPageError('商品网页未解析到公开地址。')
    return addresses


def _request_page(url, deadline):
    parsed = urlsplit(url)
    host = parsed.hostname
    tls = parsed.scheme == 'https'
    port = 443 if tls else 80
    proxy = _system_loopback_proxy(parsed.scheme)
    try:
        addresses = _resolve_public_addresses(host, port, deadline)
    except _SyntheticDns:
        if proxy is None:
            raise CaptionPageError('本机 DNS 返回代理虚拟地址，商品网页读取需要已配置的本机 HTTP 代理。') from None
        addresses = _resolve_with_doh(host, port, proxy, deadline)
    target = parsed.path + ('?' + parsed.query if parsed.query else '')
    try:
        with _http_get(host, parsed.netloc, port, tls, target, addresses, deadline,
                       'text/html, application/xhtml+xml', proxy=proxy) as response:
            if response.status in _REDIRECTS:
                return response.getheader('Location'), None
            if response.status in (401, 403, 407, 429, 451):
                raise CaptionPageError('商品网页限制访问，可能需要登录、验证或稍后重试。')
            if response.status != 200:
                raise CaptionPageError(f'商品网页读取失败（HTTP {response.status}）。')
            if response.headers.get_content_type() not in ('text/html', 'application/xhtml+xml'):
                raise CaptionPageError('此链接没有返回可读取的商品网页。')
            if response.length is not None and response.length > _MAX_BYTES:
                raise CaptionPageError('商品网页内容过大，请尝试更简洁的商品详情链接。')
            data = response.read(_MAX_BYTES + 1)
            _remaining(deadline)
            if len(data) > _MAX_BYTES:
                raise CaptionPageError('商品网页内容过大，请尝试更简洁的商品详情链接。')
            encoding = response.getheader('Content-Encoding', '').strip().lower()
            if encoding == 'gzip':
                data = _gunzip(data)
            elif encoding not in ('', 'identity'):
                raise CaptionPageError('商品网页返回了暂不支持的压缩格式。')
            return None, _decode_html(data, response.headers.get_content_charset())
    except ssl.SSLError:
        raise CaptionPageError('商品网页的 HTTPS 连接或证书验证失败。') from None
    except TimeoutError:
        raise CaptionPageError(_TIMEOUT_ERROR) from None
    except (OSError, http.client.HTTPException):
        _remaining(deadline)
        raise CaptionPageError('无法读取商品网页，请检查链接或网络。') from None


def _gunzip(data):
    try:
        reader = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = reader.decompress(data, _MAX_BYTES + 1)
    except zlib.error:
        raise CaptionPageError('商品网页的压缩内容无法读取。') from None
    if len(decoded) > _MAX_BYTES or reader.unconsumed_tail:
        raise CaptionPageError('商品网页解压后内容过大。')
    if not reader.eof:
        raise CaptionPageError('商品网页的压缩内容不完整。')
    return decoded


def _decode_html(data, charset):
    if not charset:
        match = re.search(rb'<meta\b[^>]{0,256}\bcharset\s*=\s*["\']?([a-zA-Z0-9_-]{2,24})', data[:4096], re.I)
        charset = match[1].decode('ascii') if match else 'utf-8-sig'
    if charset.lower() in ('gb2312', 'gbk'):
        charset = 'gb18030'
    try:
        return data.decode(charset, errors='replace')
    except (LookupError, UnicodeError):
        return data.decode('utf-8-sig', errors='replace')


_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_SKIP = frozenset('style script template noscript iframe object svg canvas nav footer form'.split())
_MARKUP = re.compile(r'<[^>]{0,1024}>')
_BLOCKED_TITLES = (
    'access denied', 'attention required', 'just a moment', 'robot check', 'security check',
    'verify you are human', 'request blocked', 'sign in', 'log in',
    '访问受限', '安全验证', '请先登录', '人机验证',
)


def _plain(value, size):
    return ' '.join(str(value).split())[:size]


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.title = ''
        self.lines = []
        self._length = 0
        self.main_lines = []
        self._main_length = 0
        self._landmarks = {'main': 0, 'article': 0}
        self._skipped_tag = None
        self._skip_depth = 0
        self._in_title = False
        self._jsonld = None
        self.products = []

    def handle_starttag(self, tag, attrs):
        if self._skipped_tag:
            if tag == self._skipped_tag:
                self._skip_depth += 1
            return
        attrs = dict(attrs)
        hidden = (
            'hidden' in attrs or (attrs.get('aria-hidden') or '').lower() == 'true'
            or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style') or '', re.I)
        )
        if tag == 'script' and not hidden and (attrs.get('type') or '').lower().strip() == 'application/ld+json':
            self._jsonld = ''
        if tag not in _VOID and (hidden or tag in _SKIP):
            self._skipped_tag = tag
            self._skip_depth = 1
        if hidden:
            return
        if tag in self._landmarks:
            self._landmarks[tag] += 1
        if tag == 'meta':
            name = (attrs.get('property') or attrs.get('name') or '').strip().lower()
            if name in ('og:title', 'og:description', 'twitter:title', 'twitter:description', 'description',
                        'product:price:amount', 'product:price:currency', 'product:availability'):
                self.metadata.setdefault(name, _plain(attrs.get('content') or '', 800))
        elif tag == 'title':
            self._in_title = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self._skipped_tag:
            if tag == self._skipped_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skipped_tag = None
                    if self._jsonld is not None:
                        if len(self.products) < 3:
                            self.products.extend(_jsonld_products(self._jsonld, 3 - len(self.products)))
                        self._jsonld = None
            return
        if tag == 'title':
            self._in_title = False
        elif tag in self._landmarks and self._landmarks[tag]:
            self._landmarks[tag] -= 1

    def handle_data(self, data):
        if self._jsonld is not None:
            self._jsonld = (self._jsonld + data)[:96_001]
        elif not self._skipped_tag:
            if self._in_title:
                self.title = _plain(self.title + ' ' + data, 240)
            else:
                keep_main = any(self._landmarks.values()) and self._main_length < _MAX_TEXT
                if self._length < _MAX_TEXT or keep_main:
                    value = _plain(data, _MAX_TEXT)
                    if value and self._length < _MAX_TEXT:
                        self.lines.append(value[:_MAX_TEXT - self._length])
                        self._length += len(self.lines[-1]) + 1
                    if value and keep_main:
                        self.main_lines.append(value[:_MAX_TEXT - self._main_length])
                        self._main_length += len(self.main_lines[-1]) + 1


def _jsonld_products(raw, limit):
    if len(raw) > 96_000:
        return []
    try:
        root = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    pending = deque([root])
    products = []
    while pending and len(products) < limit:
        node = pending.popleft()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            kinds = node.get('@type', [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if isinstance(kinds, list) and any(
                isinstance(kind, str) and kind.rsplit('/', 1)[-1] in ('Product', 'ProductGroup')
                for kind in kinds
            ):
                products.append(node)
            pending.extend(value for value in node.values() if isinstance(value, (dict, list)))
    return products


def _product_details(products):
    lines = []
    fields = {
        'name': '商品名称', 'description': '商品介绍', 'brand': '品牌', 'sku': 'SKU', 'model': '型号',
        'color': '颜色', 'material': '材质', 'size': '尺寸', 'category': '类别',
    }
    for product in products:
        for key, label in fields.items():
            value = product.get(key)
            if isinstance(value, dict):
                value = value.get('name') or value.get('value')
            if isinstance(value, (str, int, float)) and value:
                text = _plain(_MARKUP.sub(' ', str(value)), 600)
                if text:
                    lines.append(f'{label}：{text}')
        offer = product.get('offers')
        if isinstance(offer, list):
            offer = next((item for item in offer if isinstance(item, dict)), None)
        if isinstance(offer, dict):
            values = [offer.get(key) for key in ('price', 'priceCurrency', 'availability')]
            tokens = [_plain(str(value).rsplit('/', 1)[-1], 100) for value in values
                      if isinstance(value, (str, int, float))]
            if tokens:
                lines.append('页面所列报价和库存：' + ' '.join(tokens))
    return '\n'.join(dict.fromkeys(lines))


def _extract_page(url, markup):
    parser = _PageParser()
    try:
        parser.feed(markup)
        parser.close()
    except (ValueError, AssertionError, RecursionError):
        raise CaptionPageError('商品网页内容无法解析。') from None
    meta = parser.metadata
    title = (meta.get('og:title') or meta.get('twitter:title') or parser.title)[:240]
    description = meta.get('og:description') or meta.get('twitter:description') or meta.get('description') or ''
    details = _product_details(parser.products)
    visible = '\n'.join(dict.fromkeys(parser.main_lines + parser.lines))
    if (not details and any(title.lower().startswith(prefix) for prefix in _BLOCKED_TITLES)
            or not details and not description and len(visible) < 40):
        raise CaptionPageError('没有读取到公开的商品内容，网页可能需要登录、验证或由脚本加载。')
    sections = [part for part in (
        f'页面标题：{title}' if title else '',
        f'页面摘要：{description}' if description else '',
        '商品结构化资料：\n' + details if details else '',
        '页面可见文字：\n' + visible if visible else '',
    ) if part]
    return ProductPage(url, title, '\n'.join(sections)[:_MAX_TEXT])


def fetch_product_page(url: str) -> ProductPage:
    """Fetch only the supplied public page and its independently validated redirects."""
    current = normalize_page_url(url)
    deadline = time.monotonic() + _TIMEOUT
    for count in range(_MAX_REDIRECTS + 1):
        location, markup = _request_page(current, deadline)
        if markup is not None:
            page = _extract_page(current, markup)
            _remaining(deadline)
            return page
        if (not location or count == _MAX_REDIRECTS or len(location) > MAX_PAGE_URL_LENGTH
                or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in location)):
            raise CaptionPageError('商品链接的重定向无效或次数过多。')
        try:
            next_url = normalize_page_url(urljoin(current, location))
        except ValueError:
            raise CaptionPageError('商品链接重定向到无效或非公开地址。') from None
        if current.startswith('https://') and not next_url.startswith('https://'):
            raise CaptionPageError('商品链接从 HTTPS 跳转到不安全的 HTTP。')
        current = next_url
    raise CaptionPageError('商品链接的重定向次数过多。')
