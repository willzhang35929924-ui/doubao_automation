"""Public product page safety and extraction. No live DNS or HTTP."""

import gzip
import io
import json
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import Mock, patch

from doubao_automation import caption_page as page


URL = 'https://shop.example.com/p/hat?style=red'
PUBLIC_IP = '93.184.216.34'


def address(value):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (value, 443))]


def response(status='200 OK', body='', **headers):
    payload = body.encode('utf-8')
    fields = {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(payload)), **headers}
    lines = [f'HTTP/1.1 {status}', *(f'{name}: {value}' for name, value in fields.items()), '', '']
    sock = Mock()
    sock.makefile.return_value = io.BytesIO('\r\n'.join(lines).encode('ascii') + payload)
    return sock


def doh_response(record_type, records):
    body = json.dumps({
        'Status': 0,
        'Question': [{'name': 'shop.example.com.', 'type': record_type}],
        'Answer': [{'type': record_type, 'data': address} for address in records],
    })
    return response(body=body, **{'Content-Type': 'application/dns-json'})


class CaptionPageTests(unittest.TestCase):
    def setUp(self):
        proxy = patch.object(page, 'getproxies', return_value={})
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_normalize_url_keeps_query_drops_fragment_and_encodes_path(self):
        normalized = page.normalize_page_url('  HTTPS://SHOP.Example.com:443/帽子?color=红#details  ')
        self.assertEqual(normalized, 'https://shop.example.com/%E5%B8%BD%E5%AD%90?color=%E7%BA%A2')
        self.assertEqual(page.normalize_page_url('https://[2606:4700:4700::1111]'), 'https://[2606:4700:4700::1111]/')
        self.assertEqual(page.normalize_page_url('http://8.8.8.8:80/'), 'http://8.8.8.8/')

    def test_unsafe_urls_never_resolve(self):
        with patch.object(page.socket, 'getaddrinfo') as resolve:
            for link in (
                'file:///etc/passwd', 'https://user:password@shop.example.com', 'http://127.0.0.1/',
                'https://192.168.1.5', 'http://169.254.169.254/', 'http://10.0.0.1/',
                'http://192.0.0.9/', 'http://192.88.99.2/',
                'http://224.0.0.1/', 'http://100.64.0.1/', 'http://127.000.0.1/', 'http://2130706433/',
                'http://[::1]/', 'http://[fe80::1%25en0]/', 'http://[::ffff:8.8.8.8]/',
                'http://[64:ff9b::a00:1]/', 'https://[2002:7f00:1::]/', 'https://[2606:4700::1111]extra/',
                'https://localhost./', 'https://printer.local/', 'https://app.home.arpa/',
                'https://host.internal/', 'https://shop.example.com:8782/', 'https://shop.example.com:/',
                'https://shop.example.com//elsewhere', 'https://shop.example.com\\@other.com/',
            ):
                with self.subTest(link=link), self.assertRaises(page.CaptionPageError) as failed:
                    page.fetch_product_page(link)
                self.assertNotIn('password', str(failed.exception))
        resolve.assert_not_called()

    def test_every_dns_answer_must_be_public_before_dial(self):
        with patch.object(page.socket, 'getaddrinfo',
                          return_value=address(PUBLIC_IP) + address('127.0.0.1')) as resolve, \
             patch.object(page, '_dial') as dial:
            with self.assertRaisesRegex(page.CaptionPageError, '只能读取公开'):
                page.fetch_product_page(URL)
        self.assertEqual(resolve.call_args.args[:2], ('shop.example.com.', 443))
        dial.assert_not_called()

    def test_transport_pins_resolved_socket_preserving_tls_hostname_and_certificate(self):
        raw_socket, tls_socket = Mock(), Mock()
        deadline = time.monotonic() + 1
        with patch.object(page.socket, 'socket', return_value=raw_socket), \
             patch.object(page.ssl, 'create_default_context') as create:
            create.return_value.wrap_socket.return_value = tls_socket
            dialed = page._dial([(socket.AF_INET, (PUBLIC_IP, 443))], 'shop.example.com', deadline)
        self.assertIs(dialed, tls_socket)
        raw_socket.connect.assert_called_once_with((PUBLIC_IP, 443))
        create.assert_called_once_with()
        create.return_value.wrap_socket.assert_called_once_with(raw_socket, server_hostname='shop.example.com')

    def test_get_uses_pinned_public_address_and_origin_form_without_cookies(self):
        sock = response(body='<title>Red hat</title><main>A red cotton hat with an adjustable closure.</main>')
        with patch.object(page.socket, 'getaddrinfo', return_value=address(PUBLIC_IP)) as resolve, \
             patch.object(page, '_dial', return_value=sock) as dial:
            found = page.fetch_product_page(URL)
        self.assertEqual(found.url, URL)
        self.assertEqual(found.title, 'Red hat')
        self.assertIn('adjustable closure', found.text)
        resolve.assert_called_once()
        self.assertEqual(dial.call_args.args[:2], ([(socket.AF_INET, (PUBLIC_IP, 443))], 'shop.example.com'))
        request = sock.sendall.call_args.args[0]
        self.assertIn(b'GET /p/hat?style=red HTTP/1.1\r\n', request)
        self.assertIn(b'Host: shop.example.com\r\n', request)
        self.assertNotIn(b'Cookie:', request)
        self.assertNotIn(b'Authorization:', request)

    def test_redirect_validates_new_dns_answers_and_never_dials_private_ip(self):
        sock = response('302 Found', Location='https://other.example.com/target')
        with patch.object(page.socket, 'getaddrinfo',
                          side_effect=[address(PUBLIC_IP), address('10.0.0.1')]) as resolve, \
             patch.object(page, '_dial', return_value=sock) as dial:
            with self.assertRaisesRegex(page.CaptionPageError, '只能读取公开'):
                page.fetch_product_page(URL)
        self.assertEqual([call.args[0] for call in resolve.call_args_list],
                         ['shop.example.com.', 'other.example.com.'])
        dial.assert_called_once()

    def test_redirect_syntax_and_limits(self):
        for link in ('http://shop.example.com/', 'https://127.0.0.1/', 'file:///etc/passwd',
                     'https://other.example.com:8080/', 'https://other.example.com/\npath'):
            with self.subTest(link=link), patch.object(page, '_request_page', return_value=(link, None)) as get:
                with self.assertRaises(page.CaptionPageError):
                    page.fetch_product_page(URL)
                get.assert_called_once()
        with patch.object(page, '_request_page', return_value=('/loop', None)) as get:
            with self.assertRaisesRegex(page.CaptionPageError, '重定向'):
                page.fetch_product_page(URL)
        self.assertEqual(get.call_count, page._MAX_REDIRECTS + 1)

    def test_synthetic_dns_uses_verified_fixed_doh_and_numeric_proxy_tunnels(self):
        sockets = [response('200 Connection established') for _ in range(3)]
        a = doh_response(1, [PUBLIC_IP])
        aaaa = doh_response(28, ['2606:4700:4700::1111'])
        target = response(body='<meta name="description" content="公开商品规格和使用方法。">')
        with patch.object(page.socket, 'getaddrinfo', return_value=address('198.18.0.42')), \
             patch.object(page, 'getproxies', return_value={'https': 'http://127.0.0.1:1082'}), \
             patch.object(page.socket, 'socket', side_effect=sockets), \
             patch.object(page.ssl, 'create_default_context') as context:
            context.return_value.wrap_socket.side_effect = [a, aaaa, target]
            found = page.fetch_product_page(URL)
        self.assertIn('公开商品规格', found.text)
        for sock, expected in zip(sockets, ('1.1.1.1:443', '1.1.1.1:443', f'{PUBLIC_IP}:443')):
            sock.connect.assert_called_once_with(('127.0.0.1', 1082))
            self.assertTrue(sock.sendall.call_args.args[0].startswith(f'CONNECT {expected} '.encode('ascii')))
        self.assertEqual(
            [call.kwargs['server_hostname'] for call in context.return_value.wrap_socket.call_args_list],
            ['cloudflare-dns.com', 'cloudflare-dns.com', 'shop.example.com'],
        )
        self.assertIn(b'GET /dns-query?name=shop.example.com&type=A HTTP/', a.sendall.call_args.args[0])
        self.assertIn(b'GET /dns-query?name=shop.example.com&type=AAAA HTTP/', aaaa.sendall.call_args.args[0])
        self.assertIn(b'GET /p/hat?style=red HTTP/', target.sendall.call_args.args[0])
        for query in (a, aaaa):
            self.assertNotIn(b'style=red', query.sendall.call_args.args[0])

    def test_proxy_fallback_refuses_untrusted_proxy_values_and_mixed_synthetic_dns(self):
        with patch.object(page.socket, 'getaddrinfo', return_value=address('198.19.0.40')), \
             patch.object(page, '_resolve_with_doh') as doh:
            for configured in ({}, {'https': 'http://proxy.example.com:1082'},
                               {'https': 'http://name:password@127.0.0.1:1082'},
                               {'https': 'https://127.0.0.1:1082'},
                               {'https': 'http://[::1%zone]:1082'}):
                with self.subTest(configured=bool(configured)), patch.object(page, 'getproxies', return_value=configured):
                    with self.assertRaisesRegex(page.CaptionPageError, '本机 HTTP 代理') as failed:
                        page.fetch_product_page(URL)
                    self.assertNotIn('password', str(failed.exception))
            doh.assert_not_called()
        with patch.object(page.socket, 'getaddrinfo',
                          return_value=address('198.19.0.40') + address(PUBLIC_IP)), \
             patch.object(page, '_resolve_with_doh') as doh:
            with self.assertRaisesRegex(page.CaptionPageError, '只能读取公开'):
                page.fetch_product_page(URL)
        doh.assert_not_called()

    def test_regular_public_ip_or_dns_uses_supported_loopback_proxy_without_doh(self):
        for link in (URL, f'https://{PUBLIC_IP}/'):
            with self.subTest(link=link):
                target = response(body='<meta name="description" content="公开商品规格和使用方法。">')
                with patch.object(page, 'getproxies', return_value={'https': 'http://127.0.0.1:1082'}), \
                     patch.object(page.socket, 'getaddrinfo', return_value=address(PUBLIC_IP)), \
                     patch.object(page, '_resolve_with_doh') as doh, \
                     patch.object(page, '_dial', return_value=target) as dial:
                    self.assertIn('公开商品规格', page.fetch_product_page(link).text)
                doh.assert_not_called()
                self.assertEqual(dial.call_args.args[0], [(socket.AF_INET, (PUBLIC_IP, 443))])
                self.assertEqual(dial.call_args.kwargs['proxy'], (socket.AF_INET, ('127.0.0.1', 1082)))

    def test_proxy_fallback_never_dials_private_doh_answers_or_follows_doh_redirect(self):
        for sockets in (
            [doh_response(1, [PUBLIC_IP, '127.0.0.1'])],
            [doh_response(1, [PUBLIC_IP]), doh_response(28, ['::1'])],
            [response('302 Found', Location='https://other.example.com/resolver')],
            [response(**{'Content-Type': 'application/dns-json', 'Content-Length': str(page._MAX_DOH_BYTES + 1)})],
        ):
            with patch.object(page.socket, 'getaddrinfo', return_value=address('198.18.0.42')), \
                 patch.object(page, 'getproxies', return_value={'https': 'http://127.0.0.1:1082'}), \
                 patch.object(page, '_dial', side_effect=sockets) as dial:
                with self.assertRaises(page.CaptionPageError):
                    page.fetch_product_page(URL)
            self.assertEqual(dial.call_count, len(sockets))
            self.assertTrue(all(call.args[1] == 'cloudflare-dns.com' for call in dial.call_args_list))

    def test_safe_relative_redirect_returns_final_url_and_keeps_query(self):
        markup = '<meta name="description" content="页面列出一顶带拉绳的红帽。">'
        with patch.object(page, '_request_page', side_effect=[('../item?sku=99#x', None), (None, markup)]):
            found = page.fetch_product_page(URL)
        self.assertEqual(found.url, 'https://shop.example.com/item?sku=99')
        self.assertIn('带拉绳的红帽', found.text)

    def test_tls_and_access_errors_avoid_raw_credentials_or_response_body(self):
        with patch.object(page, '_resolve_public_addresses', return_value=[]), \
             patch.object(page, '_dial', side_effect=ssl.SSLCertVerificationError('fixture TLS secret')):
            with self.assertRaisesRegex(page.CaptionPageError, '证书验证失败') as failed:
                page.fetch_product_page(URL)
        self.assertNotIn('secret', str(failed.exception))
        with patch.object(page, '_resolve_public_addresses', return_value=[]), \
             patch.object(page, '_dial', return_value=response('403 Forbidden', 'private response')):
            with self.assertRaisesRegex(page.CaptionPageError, '限制访问') as failed:
                page.fetch_product_page(URL)
        self.assertNotIn('private response', str(failed.exception))

    def test_dns_wait_is_bounded(self):
        resume = threading.Event()
        done = threading.Event()

        def slow_dns(*_args, **_kwargs):
            try:
                resume.wait(2)
                return address(PUBLIC_IP)
            finally:
                done.set()

        try:
            with patch.object(page, '_TIMEOUT', 0.05), patch.object(page.socket, 'getaddrinfo', slow_dns):
                with self.assertRaisesRegex(page.CaptionPageError, '超时'):
                    page.fetch_product_page(URL)
        finally:
            resume.set()
            self.assertTrue(done.wait(1))

    def test_extract_product_jsonld_and_visible_content_excluding_scripts_and_hidden(self):
        html = '''
        <title>品牌目录</title>
        <meta property="og:title" content="防雨外套">
        <meta name="description" content="商品展示图和参数">
        <script type="application/ld+json">{"@graph":[{"@type":"Product","name":"轻量外套",
          "description":"&lt;保暖&gt;，<b>可折叠</b>", "brand":{"name":"品牌示例"},
          "material":"尼龙", "offers":{"price":35,"priceCurrency":"USD","availability":"https://schema.org/InStock"}}]}</script>
        <script>developer_secret();</script>
        <nav><ul><li>菜单噪声</nav>
        <main><h1>产品标题</h1><p>可调节袖口与拉链</p></main>
        <div hidden><span>隐藏文字</span></div>
        <div style="display: none">隐藏样式</div><footer>页脚文字</footer>
        '''
        found = page._extract_page(URL, html)
        self.assertEqual(found.title, '防雨外套')
        for fact in ('轻量外套', '可折叠', '品牌示例', '尼龙', '35 USD InStock', '可调节袖口与拉链'):
            self.assertIn(fact, found.text)
        for hidden in ('developer_secret', '<b>', '菜单噪声', '隐藏文字', '隐藏样式', '页脚文字'):
            self.assertNotIn(hidden, found.text)

    def test_main_product_text_survives_long_leading_boilerplate(self):
        html = '<div>' + ('categories and banners ' * 400) + '</div>'
        html += '<article><p>这款橙色背包有双肩带、正面拉链和一个反光口袋。</p></article>'
        found = page._extract_page(URL, html)
        self.assertLessEqual(len(found.text), page._MAX_TEXT)
        self.assertIn('正面拉链', found.text)

    def test_scripts_only_and_access_gate_return_actionable_errors(self):
        for markup in ('<title>Shop</title><script src="/app.js"></script>',
                       '<title>Just a moment</title><meta name="description" content="Browser check">'):
            with self.subTest(markup=markup), self.assertRaisesRegex(page.CaptionPageError, '可能需要登录'):
                page._extract_page(URL, markup)
        with patch.object(page._PageParser, 'feed', side_effect=AssertionError('raw response')):
            with self.assertRaisesRegex(page.CaptionPageError, '无法解析'):
                page._extract_page(URL, '')

    def test_reject_non_html_and_bound_identity_and_gzip_size(self):
        with patch.object(page, '_resolve_public_addresses', return_value=[]):
            for sock, error in (
                (response(body='binary', **{'Content-Type': 'video/mp4'}), '没有返回可读取'),
                (response(body='short', **{'Content-Length': str(page._MAX_BYTES + 1)}), '内容过大'),
            ):
                with self.subTest(error=error), patch.object(page, '_dial', return_value=sock):
                    with self.assertRaisesRegex(page.CaptionPageError, error):
                        page.fetch_product_page(URL)
        with self.assertRaisesRegex(page.CaptionPageError, '解压后内容过大'):
            page._gunzip(gzip.compress(b'x' * (page._MAX_BYTES + 1)))


if __name__ == '__main__':
    unittest.main()
