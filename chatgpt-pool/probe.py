"""HTTP 探测层：通过代理（或直连）发起请求，并把传输层异常映射为诊断信息。

- ProxyProbe：经 127.0.0.1:<port> 代理（Mihomo 本地端口）；每次调用先做
  TCP 前置探测（代理端口连通性），失败 → PROXY_HANDSHAKE_FAIL。
- NoProxyProbe：直连（用于国家列表在线来源、节点源抓取）。
- classify_exception：把传输层异常细分为 (顶层 §16 原因, V0.2.2 细分原因,
  诊断细节)。顶层 reason 保持 V5.2 权威（准入/优先级不变），细分原因
  （SubReason）回答"为什么失败"。
"""
import http.client
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request

from models import FailureReason, SubReason

log = logging.getLogger("pool.probe")

# ssl.SSLError 报文中与 SNI/握手相关的关键词（细分 TLS_SNI_FAIL）
_SNI_MARKERS = ("sni", "hostname", "server hostname", "server name")
_HANDSHAKE_MARKERS = ("handshake", "alert", "wrong version", "tls",
                      "ssl", "protocol", "record")


class ProbeResult:
    __slots__ = ("status", "final_url", "content_type", "body", "elapsed")

    def __init__(self, status, final_url, content_type, body, elapsed=0.0):
        self.status = status
        self.final_url = final_url
        self.content_type = content_type
        self.body = body
        self.elapsed = elapsed


class ProbeError(Exception):
    """传输层探测失败；reason=顶层 §16 枚举，sub=细分原因，detail=诊断细节。"""

    def __init__(self, reason, message="", sub=None, detail=None):
        super().__init__(message or reason.value)
        self.reason = reason
        self.sub = sub or SubReason.UNKNOWN_FAIL
        self.detail = detail or {}


def classify_exception(e, url):
    """传输层异常 → (顶层 reason, SubReason, detail)。

    口径（V0.2.2）：
    - 前置 TCP 探测失败（代理端口不可连）→ PROXY_ERROR / PROXY_HANDSHAKE_FAIL；
    - 直连/经代理的 TCP 层 → TCP_REFUSED / TCP_ERROR / TCP_TIMEOUT；
    - TLS 层 → TLS_CERT_FAIL / TLS_SNI_FAIL / TLS_HANDSHAKE_FAIL / TLS_TIMEOUT；
    - https URL 下 socket.timeout 归 TLS_TIMEOUT（连接已建立，超时发生在 TLS/读阶段）；
    - DNS 解析失败（gaierror）→ DNS_FAIL / UNKNOWN_FAIL（detail 记录域名）。
    """
    d = {"type": e.__class__.__name__}
    if isinstance(e, socket.gaierror):
        host = ""
        try:
            host = e.args[1] if len(e.args) > 1 else ""
        except Exception:
            pass
        return FailureReason.DNS_FAIL, SubReason.UNKNOWN_FAIL, {
            **d, "dns_error": str(e)[:200], "host": host[:200]}
    if isinstance(e, ssl.SSLCertVerificationError):
        return FailureReason.TLS_FAIL, SubReason.TLS_CERT_FAIL, {
            **d, "verify_msg": str(e)[:300]}
    if isinstance(e, ssl.SSLError):
        msg = str(e).lower()
        if "certificate" in msg or "cert" in msg:
            return FailureReason.TLS_FAIL, SubReason.TLS_CERT_FAIL, {
                **d, "ssl_msg": str(e)[:300]}
        if any(m in msg for m in _SNI_MARKERS):
            return FailureReason.TLS_FAIL, SubReason.TLS_SNI_FAIL, {
                **d, "ssl_msg": str(e)[:300]}
        if any(m in msg for m in _HANDSHAKE_MARKERS):
            return FailureReason.TLS_FAIL, SubReason.TLS_HANDSHAKE_FAIL, {
                **d, "ssl_msg": str(e)[:300]}
        return FailureReason.TLS_FAIL, SubReason.TLS_HANDSHAKE_FAIL, {
            **d, "ssl_msg": str(e)[:300]}
    if isinstance(e, (socket.timeout, TimeoutError)):
        if str(url).startswith("https"):
            return FailureReason.TLS_FAIL, SubReason.TLS_TIMEOUT, {
                **d, "url": str(url)[:200]}
        return FailureReason.TIMEOUT, SubReason.TCP_TIMEOUT, {
            **d, "url": str(url)[:200]}
    if isinstance(e, ConnectionRefusedError):
        return FailureReason.PROXY_ERROR, SubReason.TCP_REFUSED, {
            **d, "url": str(url)[:200]}
    if isinstance(e, ConnectionResetError):
        return FailureReason.PROXY_ERROR, SubReason.TCP_ERROR, {
            **d, "url": str(url)[:200]}
    if isinstance(e, OSError):
        return FailureReason.PROXY_ERROR, SubReason.TCP_ERROR, {
            **d, "url": str(url)[:200], "os_error": str(e)[:200]}
    return FailureReason.PROXY_ERROR, SubReason.UNKNOWN_FAIL, {
        **d, "url": str(url)[:200], "message": str(e)[:200]}


def _proxy_preflight(proxy_port, timeout):
    """TCP 前置探测：本地代理端口是否可连（V0.2.2 P0-01）。

    连不上 → (顶层 PROXY_ERROR, 细分 PROXY_HANDSHAKE_FAIL, detail)。
    成功 → None。
    """
    t0 = time.time()
    try:
        s = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
        s.close()
        return None
    except OSError as e:
        return (FailureReason.PROXY_ERROR, SubReason.PROXY_HANDSHAKE_FAIL, {
            "type": e.__class__.__name__,
            "proxy_host": "127.0.0.1", "proxy_port": proxy_port,
            "tcp_error": str(e)[:200],
            "elapsed_ms": int((time.time() - t0) * 1000),
        })


def _map_exception(e, url=""):
    """兼容旧签名：返回顶层 reason（旧调用方/测试仍可用）。"""
    top, _, _ = classify_exception(e, url)
    return top


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


def _do_get(url, cfg, proxy_port=None, read_limit=65536, preflight=True):
    t0 = time.time()
    if proxy_port and preflight:
        bad = _proxy_preflight(proxy_port, cfg.connect_timeout)
        if bad is not None:
            top, sub, detail = bad
            raise ProbeError(top, sub=sub, detail=detail)
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
        top, sub, detail = classify_exception(e.reason, url)
        raise ProbeError(top, sub=sub,
                         detail={**detail, "elapsed_ms": int((time.time() - t0) * 1000)})
    except (ssl.SSLError, socket.timeout, TimeoutError, OSError,
            http.client.HTTPException) as e:
        top, sub, detail = classify_exception(e, url)
        raise ProbeError(top, sub=sub,
                         detail={**detail, "elapsed_ms": int((time.time() - t0) * 1000)})


class ProxyProbe:
    """经 Mihomo 本地代理端口发请求（每次先做 TCP 前置探测）。"""

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
        return _do_get(url, self.cfg, proxy_port=None, read_limit=self.read_limit,
                       preflight=False)
