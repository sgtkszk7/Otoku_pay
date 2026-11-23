"""Bounded HTTPS retrieval of pages on the configured official hosts."""
import http.client
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, urljoin


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def is_official(url, domains):
    if not isinstance(url, str) or any(ord(c) <= 32 or c == "\\" for c in url):
        return False
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and u.port in (None, 443) and u.username is None
                and u.password is None and u.hostname and
                any(u.hostname == d or u.hostname.endswith("." + d) for d in domains))
    except ValueError:
        return False


def fetch_page(url, domains):
    for _ in range(5):
        if not is_official(url, domains):
            raise ValueError("公式HTTPS出典の範囲外です")
        u = urlsplit(url)
        addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
            raise ValueError("公開アドレスの公式出典が必要です")
        conn = http.client.HTTPSConnection(u.hostname, timeout=12)
        try:
            # Pin the checked address, while using the original host for TLS and Host.
            with socket.create_connection((str(ips[0]), 443), timeout=12) as sock:
                conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
                conn.request("GET", urlunsplit(("", "", u.path or "/", u.query, "")),
                             headers={"User-Agent": "OtokuPay-RateChecker", "Accept-Encoding": "identity"})
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                    url = urljoin(url, resp.getheader("Location"))
                    continue
                if resp.status != 200 or resp.headers.get_content_type() not in ("text/html", "text/plain", "application/xhtml+xml"):
                    raise ValueError("公式ページの本文を確認できません")
                raw = resp.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("出典ページの取得上限を超えました")
                parser = PageText()
                parser.feed(raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace"))
                page = "\n".join(parser.parts)[:20000]
                if not page:
                    raise ValueError("出典ページの本文が空です")
                return url, page
        finally:
            conn.close()
    raise ValueError("出典のリダイレクト回数が上限を超えました")
