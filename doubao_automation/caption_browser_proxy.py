"""Short-lived, authenticated public-IP tunnels for product-page Chromium."""

import base64
import contextlib
import math
import secrets
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from . import caption_page


_MAX_HEADER = 8192
_MAX_CONNECTIONS = 256
_MAX_CONCURRENT = 24
_MAX_BYTES = 64 * 1024 * 1024
_HEADER_TIMEOUT = 5
_CONNECT_TIMEOUT = 15


class _RequestError(Exception):
    def __init__(self, status):
        self.status = status


def _reply(sock, status):
    reasons = {
        200: "Connection Established", 400: "Bad Request", 403: "Forbidden",
        405: "Method Not Allowed", 407: "Proxy Authentication Required",
        429: "Too Many Requests", 502: "Bad Gateway",
    }
    headers = f"HTTP/1.1 {status} {reasons[status]}\r\n"
    if status == 407:
        headers += 'Proxy-Authenticate: Basic realm="ProductPage"\r\n'
    if status != 200:
        headers += "Connection: close\r\nContent-Length: 0\r\n"
    with contextlib.suppress(OSError):
        sock.sendall((headers + "\r\n").encode("ascii"))


def _read_target(sock, authorization, deadline):
    header_deadline = min(deadline, time.monotonic() + _HEADER_TIMEOUT)
    data = bytearray()
    while b"\r\n\r\n" not in data:
        sock.settimeout(caption_page._remaining(header_deadline))
        chunk = sock.recv(min(1024, _MAX_HEADER + 1 - len(data)))
        if not chunk:
            raise _RequestError(400)
        data.extend(chunk)
        if len(data) > _MAX_HEADER:
            raise _RequestError(400)
    head, trailing = bytes(data).split(b"\r\n\r\n", 1)
    if trailing:
        raise _RequestError(400)
    try:
        lines = head.decode("ascii").split("\r\n")
        method, authority, version = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            name = name.lower()
            if (not separator or not name or name.strip() != name or name in headers
                    or any(ord(char) < 32 or ord(char) == 127 for char in line)):
                raise ValueError
            headers[name] = value.strip()
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise ValueError
    except (UnicodeError, ValueError):
        raise _RequestError(400) from None
    supplied = headers.get("proxy-authorization", "")
    if not secrets.compare_digest(supplied, authorization):
        raise _RequestError(407)
    if method != "CONNECT":
        raise _RequestError(405)
    if "content-length" in headers or "transfer-encoding" in headers:
        raise _RequestError(400)
    try:
        if any(char in authority for char in "/?#\\"):
            raise ValueError
        parsed = urlsplit("https://" + authority)
        if parsed.port != 443:
            raise ValueError
        normalized = caption_page.normalize_page_url("https://" + authority)
    except (ValueError, TypeError):
        raise _RequestError(403) from None
    return urlsplit(normalized).hostname


def _close_socket(sock):
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class _PublicProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = _MAX_CONCURRENT

    def __init__(self, timeout, authorization):
        self.deadline = time.monotonic() + timeout
        self.authorization = authorization
        self.upstream_proxy = caption_page._system_loopback_proxy("https")
        self.stopped = threading.Event()
        self.guard = threading.Lock()
        self.stop_guard = threading.Lock()
        self.slots = threading.BoundedSemaphore(_MAX_CONCURRENT)
        self.sockets = set()
        self.resolved = {}
        self.resolve_locks = {}
        self.connections = 0
        self.transferred = 0
        super().__init__(("127.0.0.1", 0), _TunnelHandler)

    def process_request(self, request, address):
        with self.guard:
            allowed = (not self.stopped.is_set() and self.connections < _MAX_CONNECTIONS
                       and self.transferred < _MAX_BYTES)
            if allowed:
                self.connections += 1
        if not allowed or not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Never log the CONNECT target, proxy authorization or remote errors.
        pass

    def track(self, sock):
        with self.guard:
            if self.stopped.is_set():
                _close_socket(sock)
                return False
            self.sockets.add(sock)
        return True

    def forget(self, sock):
        with self.guard:
            self.sockets.discard(sock)
        _close_socket(sock)

    def addresses(self, host, deadline):
        with self.guard:
            host_guard = self.resolve_locks.setdefault(host, threading.Lock())
        if not host_guard.acquire(timeout=caption_page._remaining(deadline)):
            raise caption_page.CaptionPageError("商品网页地址解析超时。")
        try:
            if host in self.resolved:
                return self.resolved[host]
            try:
                result = caption_page._resolve_public_addresses(host, 443, deadline)
            except caption_page._SyntheticDns:
                if self.upstream_proxy is None:
                    raise caption_page.CaptionPageError("本机代理无法解析公开商品地址。") from None
                result = caption_page._resolve_with_doh(host, 443, self.upstream_proxy, deadline)
            self.resolved[host] = result
            return result
        finally:
            host_guard.release()

    def charge(self, size):
        with self.guard:
            self.transferred += size
            return self.transferred <= _MAX_BYTES and not self.stopped.is_set()

    def stop(self):
        with self.stop_guard:
            if self.stopped.is_set():
                return
            self.stopped.set()
            with self.guard:
                owned = list(self.sockets)
            for sock in owned:
                _close_socket(sock)
            self.shutdown()
            self.server_close()


class _TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        client = self.request
        if not server.track(client):
            return
        upstream = None
        established = False
        try:
            host = _read_target(client, server.authorization, server.deadline)
            connect_deadline = min(server.deadline, time.monotonic() + _CONNECT_TIMEOUT)
            addresses = server.addresses(host, connect_deadline)
            if server.stopped.is_set():
                return
            upstream = caption_page._dial(addresses, None, connect_deadline,
                                          proxy=server.upstream_proxy)
            if not server.track(upstream):
                return
            _reply(client, 200)
            established = True
            client.settimeout(2)
            upstream.settimeout(2)
            while not server.stopped.is_set():
                remaining = caption_page._remaining(server.deadline)
                ready, _, _ = select.select((client, upstream), (), (), min(0.2, remaining))
                for source in ready:
                    data = source.recv(64 * 1024)
                    if not data or not server.charge(len(data)):
                        return
                    destination = upstream if source is client else client
                    destination.sendall(data)
        except _RequestError as error:
            _reply(client, error.status)
        except (OSError, ValueError):
            if not established:
                _reply(client, 502)
        finally:
            if upstream is not None:
                server.forget(upstream)
            server.forget(client)


@contextlib.contextmanager
def public_browser_proxy(timeout=60):
    """Yield a private Chromium proxy and close every established tunnel on exit."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 180):
        raise ValueError("商品页面浏览器的代理超时配置无效。")
    username, password = secrets.token_urlsafe(18), secrets.token_urlsafe(24)
    encoded = base64.b64encode((username + ":" + password).encode("ascii")).decode("ascii")
    server = _PublicProxy(timeout, "Basic " + encoded)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    timer = threading.Timer(timeout, server.stop)
    timer.daemon = True
    started = False
    try:
        thread.start()
        started = True
        timer.start()
        yield {"server": f"http://127.0.0.1:{server.server_address[1]}",
               "username": username, "password": password}
    finally:
        timer.cancel()
        if started:
            server.stop()
            thread.join(timeout=2)
        else:
            server.server_close()
