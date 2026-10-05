"""订阅服务（§25 / P1-08）。

只绑定 127.0.0.1；只负责交付最终订阅文件 /chatgpt.yaml，
不是 Mihomo 代理端口。
"""
import http.server
import logging
import os
import threading

log = logging.getLogger("pool.server")


def make_handler(cfg):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/chatgpt.yaml", "/chatgpt.yml"):
                p = cfg.pool_path
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-yaml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._reply(404, b"pool not found")
            elif self.path == "/healthz":
                self._reply(200, b"ok")
            else:
                self._reply(404, b"not found")

        def _reply(self, code, msg):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

        def log_message(self, fmt, *args):
            log.info("http %s", fmt % args)

    return Handler


def start_server(cfg, port=None, block=False):
    port = port if port is not None else getattr(cfg, "server_port", cfg.prod_server_port)
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(cfg))
    log.info("subscription server on 127.0.0.1:%s", port)
    if block:
        srv.serve_forever()
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
