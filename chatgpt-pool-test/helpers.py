"""测试公共设施：全部 Mock，绝不触碰生产环境。

- 目录：本目录下 base/<随机>/（不写 ~/chatgpt-pool）
- 端口：18887 / 19000-19999（生产 18787/7890/18000-18699 完全不使用）
- 引擎：FakeEngine（脚本化，不启动任何 Mihomo，不产生 PID）
- 探测：本地 LocalMockSite + ScriptedProbe（不发真实网络请求）
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

from config import Config            # noqa: E402
from engine import ProxyStartError   # noqa: E402
from models import FailureReason     # noqa: E402
from probe import ProbeError, ProbeResult  # noqa: E402

TEST_ROOT = os.path.dirname(os.path.abspath(__file__))


def make_cfg(name):
    base_root = os.path.join(TEST_ROOT, "base")
    os.makedirs(base_root, exist_ok=True)
    base = tempfile.mkdtemp(prefix=name + "_", dir=base_root)
    cfg = Config.for_test(base=base)
    cfg.stability_interval = 0
    cfg.concurrency = 2
    cfg.server_port = cfg.test_server_port
    cfg.prod_engine_port_range = list(cfg.test_port_range)
    return cfg


class LocalMockSite:
    """本地 mock 站点：routes = {path: (status, headers, body_bytes)}。"""

    def __init__(self, routes):
        self.routes = dict(routes)

    def __enter__(self):
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                     self._handler())
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def port(self):
        return self._srv.server_address[1]

    def get(self, path):
        return self.routes.get(path)

    def _handler(self):
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                route = site.get(self.path)
                if route is None:
                    body = b"nf"
                    self.send_response(404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                status, headers, body = route
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        return H


def strict_success_routes():
    return {
        "/": (200, {"Content-Type": "text/html"},
              b"<html><title>ChatGPT</title><div id=__NEXT_DATA__>auth</div></html>"),
        "/auth": (200, {"Content-Type": "application/json"},
                  b'{"csrfToken":"mock-csrf"}'),
        "/backend": (200, {"Content-Type": "application/json"},
                     b'{"ok":true}'),
    }


def chatgpt_probe(site, geoip_body=None):
    """把 chatgpt 端点映射到本地站点；geoip 返回脚本化出口 IP。"""
    if geoip_body is None:
        geoip_body = json.dumps({"query": "203.0.113.9", "countryCode": "US"})

    def fn(url):
        if "ip-api.com" in url:
            return ProbeResult(200, url, "application/json", geoip_body)
        path = urllib.parse.urlsplit(url).path or "/"
        key = "/" if path == "/" else ("/auth" if "csrf" in path else "/backend")
        route = site.get(key)
        if route is None:
            return ProbeResult(404, url, "text/plain", "nf")
        status, headers, body = route
        return ProbeResult(status, url, (headers or {}).get("Content-Type", ""),
                           body.decode("utf-8", "replace"))
    return fn


class FakeEngine:
    """脚本化引擎：不启动进程、无 PID，只记录行为。"""

    instances = []

    def __init__(self, start_ok=True):
        self.start_ok = start_ok
        self.stopped = False
        self.port = None
        FakeEngine.instances.append(self)

    def start(self, node_dict, mixed_port):
        if not self.start_ok:
            raise ProxyStartError("fake start failed")
        self.port = mixed_port or 19000
        return self.port

    def stop(self):
        self.stopped = True


def fake_engine_factory(start_ok=True):
    def factory(node):
        return FakeEngine(start_ok=start_ok)
    return factory


def scripted_probe(fn):
    return fn


def err_probe(reason):
    def fn(url):
        raise ProbeError(reason)
    return fn


def fake_country_resolver(codes, warning=None):
    def resolver(cfg):
        return (codes, "test", warning)
    return resolver


def write_old_pool(cfg, text="proxies:\n  - name: old\n    type: ss\n"
                              "    server: 9.9.9.9\n    port: 1\n"):
    with open(cfg.pool_path, "w", encoding="utf-8") as f:
        f.write(text)
    return cfg.pool_path
