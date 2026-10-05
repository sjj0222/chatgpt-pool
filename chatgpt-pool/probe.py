"""HTTP 探测层：通过代理（或直连）发起请求，并把传输层异常映射为 §16 枚举。

- ProxyProbe：经 127.0.0.1:<port> 代理（Mihomo 本地端口）。
- NoProxyProbe：直连（用于国家列表在线来源、节点源抓取）。
- 异常映射：TLS_FAIL / DNS_FAIL / TIMEOUT / PROXY_ERROR。
"""
import http.client
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request

from models import FailureReason

log = logging.getLogger("pool.probe")


class ProbeResult:
    __slots__ = ("status", "final_url", "content_type", "body", "elapsed")

    def __init__(self, status, final_url, content_type, body, elapsed=0.0):
        self.status = status
        self.final_url = final_url
        self.content_type = content_type
        self.body = body
        self.elapsed = elapsed


class ProbeError(Exception):
    def __init__(self, reason, message=""):
        super().__init__(message or reason.value)
        self.reason = reason


def _map_exception(e):
    if isinstance(e, ssl.SSLError):
        return FailureReason.TLS_FAIL
    if isinstance(e, socket.gaierror):
        return FailureReason.DNS_FAIL
    if isinstance(e, (socket.timeout, TimeoutError)):
        return FailureReason.TIMEOUT
    if isinstance(e, ConnectionRefusedError):
        return FailureReason.PROXY_ERROR
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, (socket.timeout, TimeoutError)):
            return FailureReason.TIMEOUT
        if isinstance(r, ssl.SSLError):
            return FailureReason.TLS_FAIL
        if isinstance(r, socket.gaierror):
            return FailureReason.DNS_FAIL
        if isinstance(r, ConnectionRefusedError):
            return FailureReason.PROXY_ERROR
        return FailureReason.PROXY_ERROR
    return FailureReason.PROXY_ERROR


class _CapRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects):
        super().__init__()
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        n = int(req.headers.get("X-Pool-Redirect", "0")) + 1
        if n > self.max_redirects:
            raise urllib.error.HTTPError(
                newurl, code, "too many redirects", headers, fp)
        r = super().redirect_request(req, fp, code, msg, headers, newurl)
        if r is not None:
            r.add_unredirected_header("X-Pool-Redirect", str(n))
        return r


def _do_get(url, cfg, proxy_port=None, read_limit=65536):
    handlers = [_CapRedirect(cfg.max_redirects)]
    if proxy_port:
        handlers.append(urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{proxy_port}",
            "https": f"http://127.0.0.1:{proxy_port}",
        }))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": cfg.user_agent, "Accept": "*/*"})
    t0 = time.time()
    try:
        with opener.open(req, timeout=cfg.total_timeout) as resp:
            body = resp.read(read_limit).decode("utf-8", "replace")
            return ProbeResult(
                resp.status, resp.geturl(),
                resp.headers.get("Content-Type", ""), body,
                time.time() - t0)
    except urllib.error.HTTPError as e:
        body = e.read(read_limit).decode("utf-8", "replace")
        return ProbeResult(
            e.code, e.geturl() if hasattr(e, "geturl") else url,
            e.headers.get("Content-Type", ""), body, time.time() - t0)
    except urllib.error.URLError as e:
        raise ProbeError(_map_exception(e))
    except (ssl.SSLError, socket.timeout, TimeoutError, OSError,
            http.client.HTTPException) as e:
        raise ProbeError(_map_exception(e))


class ProxyProbe:
    """经 Mihomo 本地代理端口发请求。"""

    def __init__(self, cfg, proxy_port, read_limit=65536):
        self.cfg = cfg
        self.proxy_port = proxy_port
        self.read_limit = read_limit

    def __call__(self, url):
        return _do_get(url, self.cfg, proxy_port=self.proxy_port,
                       read_limit=self.read_limit)


class NoProxyProbe:
    """直连请求（国家列表在线来源、节点源抓取）。"""

    def __init__(self, cfg, read_limit=65536):
        self.cfg = cfg
        self.read_limit = read_limit

    def __call__(self, url):
        return _do_get(url, self.cfg, proxy_port=None, read_limit=self.read_limit)
