"""The browser tunnel never resolves or dials an unchecked destination."""

import base64
import socket
import unittest
from contextlib import ExitStack
from urllib.parse import urlsplit
from unittest.mock import patch

from doubao_automation import caption_browser_proxy as proxy_module
from doubao_automation import caption_page


PUBLIC = [(socket.AF_INET, ("93.184.216.34", 443))]
UPSTREAM = (socket.AF_INET, ("127.0.0.1", 1082))


def authorization(settings):
    token = (settings["username"] + ":" + settings["password"]).encode("ascii")
    return "Basic " + base64.b64encode(token).decode("ascii")


def connect_client(settings, target="shop.example.com:443", *, auth=True, method="CONNECT", extra=""):
    endpoint = urlsplit(settings["server"])
    client = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
    client.settimeout(2)
    auth_header = "Proxy-Authorization: " + authorization(settings) + "\r\n" if auth else ""
    client.sendall((f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n" + auth_header + extra + "\r\n").encode("ascii"))
    return client


def read_status(client):
    header = bytearray()
    while b"\r\n\r\n" not in header:
        data = client.recv(4096)
        if not data:
            break
        header.extend(data)
    return bytes(header)


class CaptionBrowserProxyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.proxy_lookup = self.stack.enter_context(
            patch.object(caption_page, "_system_loopback_proxy", return_value=None))
        self.resolve = self.stack.enter_context(
            patch.object(caption_page, "_resolve_public_addresses", return_value=PUBLIC))
        self.doh = self.stack.enter_context(patch.object(caption_page, "_resolve_with_doh"))
        self.dial = self.stack.enter_context(patch.object(caption_page, "_dial"))

    def tunnel(self):
        local, remote = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(remote.close)
        remote.settimeout(2)
        self.dial.return_value = local
        return remote

    def test_authentication_and_private_targets_never_reach_dns_or_dial(self):
        with proxy_module.public_browser_proxy() as settings:
            with connect_client(settings, auth=False) as client:
                self.assertIn(b"407 Proxy Authentication Required", read_status(client))
            for target in ("127.0.0.1:443", "192.168.1.1:443", "169.254.169.254:443",
                           "[::1]:443", "printer.local:443", "localhost:443",
                           "shop.example.com:80", "shop.example.com:8782",
                           "user:password@shop.example.com:443", "shop.example.com:443/path"):
                with self.subTest(target=target), connect_client(settings, target) as client:
                    self.assertIn(b"403 Forbidden", read_status(client))
            with connect_client(settings, method="GET") as client:
                self.assertIn(b"405 Method Not Allowed", read_status(client))
        self.resolve.assert_not_called()
        self.dial.assert_not_called()

    def test_tunnel_pins_public_ip_and_preserves_browser_tls_bytes_both_ways(self):
        remote = self.tunnel()
        with proxy_module.public_browser_proxy() as settings:
            with connect_client(settings) as client:
                self.assertEqual(read_status(client), b"HTTP/1.1 200 Connection Established\r\n\r\n")
                client.sendall(b"fixture TLS ClientHello")
                self.assertEqual(remote.recv(100), b"fixture TLS ClientHello")
                remote.sendall(b"fixture TLS ServerHello")
                self.assertEqual(client.recv(100), b"fixture TLS ServerHello")
                self.dial.assert_called_once()
                self.assertEqual(self.dial.call_args.args[:2], (PUBLIC, None))
                self.assertEqual(self.dial.call_args.kwargs, {"proxy": None})
        self.proxy_lookup.assert_called_once_with("https")
        self.doh.assert_not_called()

    def test_system_proxy_and_synthetic_dns_use_only_validated_numeric_destinations(self):
        self.proxy_lookup.return_value = UPSTREAM
        self.resolve.side_effect = caption_page._SyntheticDns("fixture")
        self.doh.return_value = PUBLIC
        self.tunnel()
        with proxy_module.public_browser_proxy() as settings:
            with connect_client(settings) as client:
                self.assertIn(b"200 Connection Established", read_status(client))
        self.doh.assert_called_once()
        self.assertEqual(self.doh.call_args.args[:3], ("shop.example.com", 443, UPSTREAM))
        self.assertEqual(self.dial.call_args.args[:2], (PUBLIC, None))
        self.assertEqual(self.dial.call_args.kwargs, {"proxy": UPSTREAM})

    def test_private_dns_or_synthetic_without_upstream_cannot_open_tunnel(self):
        for error in (caption_page.CaptionPageError("fixture private answer"),
                      caption_page._SyntheticDns("fixture virtual answer")):
            with self.subTest(error=type(error).__name__):
                self.resolve.side_effect = error
                with proxy_module.public_browser_proxy() as settings:
                    with connect_client(settings) as client:
                        self.assertIn(b"502 Bad Gateway", read_status(client))
        self.dial.assert_not_called()
        self.doh.assert_not_called()

    def test_oversized_or_duplicate_authorization_headers_cannot_open_tunnel(self):
        with proxy_module.public_browser_proxy() as settings:
            with connect_client(settings, extra="X-Large: " + "a" * 8192 + "\r\n") as client:
                self.assertIn(b"400 Bad Request", read_status(client))
            with connect_client(settings, extra="Proxy-Authorization: duplicate\r\n") as client:
                self.assertIn(b"400 Bad Request", read_status(client))
        self.resolve.assert_not_called()
        self.dial.assert_not_called()

    def test_exit_closes_listener_and_every_established_owned_socket(self):
        remote = self.tunnel()
        with proxy_module.public_browser_proxy() as settings:
            endpoint = urlsplit(settings["server"])
            client = connect_client(settings)
            self.addCleanup(client.close)
            self.assertIn(b"200 Connection Established", read_status(client))
        self.assertEqual(client.recv(1), b"")
        self.assertEqual(remote.recv(1), b"")
        with self.assertRaises(OSError):
            socket.create_connection((endpoint.hostname, endpoint.port), timeout=0.2)

    def test_deadline_expires_an_open_tunnel_without_waiting_for_browser_exit(self):
        remote = self.tunnel()
        with proxy_module.public_browser_proxy(timeout=0.2) as settings:
            with connect_client(settings) as client:
                self.assertIn(b"200 Connection Established", read_status(client))
                self.assertEqual(client.recv(1), b"")
                self.assertEqual(remote.recv(1), b"")

    def test_bandwidth_limit_stops_forwarding_extra_response_content(self):
        remote = self.tunnel()
        with patch.object(proxy_module, "_MAX_BYTES", 4), proxy_module.public_browser_proxy() as settings:
            with connect_client(settings) as client:
                self.assertIn(b"200 Connection Established", read_status(client))
                remote.sendall(b"too much data")
                self.assertEqual(client.recv(20), b"")


if __name__ == "__main__":
    unittest.main()
