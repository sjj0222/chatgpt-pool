"""V0.4 UI Layer（Phase 0.4-2）——本机 Web 管理页面。

四层架构：Core → Service（services.py）→ API（api.py）→ **UI（本文件）**。
- 页面：Dashboard / Nodes / Sources / Events / Diagnostic / Mobile（/m/，APK 入口）
- 页面渲染一律经 services/API，UI 不直接读 Core 文件（0.4-1：核心不依赖 UI；
  反向同理，UI 只依赖 Service/API 层）。
- API 兼容别名：api_health/api_node/api_source/api_history/api_diagnostic
  （V0.3 既有测试与旧 URL 不破坏）。
- 仅 127.0.0.1；纯标准库（http.server）。
"""
import json
import logging
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api
import services

log = logging.getLogger("pool.dashboard")

PROD_WEB_PORT = 18888
TEST_WEB_PORT = 18889

CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC',sans-serif;
background:#0f1420;color:#e6ebf5;margin:0;padding:20px;}
h1{font-size:20px;margin:0 0 4px;} .sub{color:#8fa3c4;font-size:12.5px;
margin-bottom:16px;}
nav a{color:#7ee0a3;text-decoration:none;margin-right:14px;font-size:13px;}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:10px;}
th,td{border:1px solid #2a3f63;padding:6px 8px;text-align:left;}
th{background:#13203a;color:#ffd479;}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11.5px;}
.ok{background:#1a3a24;color:#7ee0a3;} .warn{background:#3a2f1a;color:#ffd479;}
.bad{background:#3a1a1a;color:#ff9e9e;} .info{background:#1a2a3a;color:#8fc7ff;}
.card{background:#13203a;border:1px solid #2a3f63;border-radius:8px;
padding:12px 14px;margin-bottom:12px;}
.metrics{display:flex;gap:12px;flex-wrap:wrap;}
.metric{flex:1;min-width:140px;background:#0f1420;border:1px solid #2a3f63;
border-radius:8px;padding:10px;text-align:center;}
.metric .v{font-size:26px;font-weight:700;color:#7ee0a3;}
.metric .k{font-size:11.5px;color:#8fa3c4;margin-top:2px;}
pre{font-size:11.5px;color:#cfe0f5;overflow:auto;max-height:360px;}
button{background:#1a3a24;color:#7ee0a3;border:1px solid #2f6b46;
border-radius:6px;padding:8px 16px;font-size:14px;margin-right:10px;}
.mob button{width:100%;margin:8px 0;padding:16px;font-size:18px;}
"""

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s · chatgpt-pool</title>
<style>%(css)s</style></head><body>
<h1>%(title)s</h1>
<div class="sub">chatgpt-pool V0.4 · 本机管理页面（仅 127.0.0.1）</div>
<nav><a href="/">Dashboard</a><a href="/node">Nodes</a>
<a href="/source">Sources</a><a href="/events">Events</a>
<a href="/diagnostic">Diagnostic</a><a href="/m">Mobile</a></nav>
%(body)s
</body></html>
"""


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_tail(path, n=4000):
    if not os.path.isfile(path):
        return "(none)"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        return data[-n:]
    except OSError:
        return "(unreadable)"


# ---- V0.3 兼容 API 别名（内部走 services；V0.3 API 路径保持可用） ----

def api_health(cfg):
    return services.get_status(cfg)


def api_node(cfg):
    return services.get_nodes(cfg)


def api_source(cfg):
    return services.get_sources(cfg)


def api_history(cfg):
    hist = _read_json(cfg.sub("state/node_history.json"))
    if hist is None:
        return {"count": 0, "history": {}}
    return {"count": len(hist), "history": hist}


def api_diagnostic(cfg):
    return {
        "refresh_state": _read_json(cfg.sub("run/refresh_state.json")),
        "source_health": _read_json(cfg.sub("run/source_health.json")),
        "detect_log_tail": _read_tail(cfg.sub("run/detect.log")),
    }


def _state_badge(state):
    color = {"ACTIVE": "ok", "STANDBY": "info", "PROBATION": "info",
             "RECOVERING": "info", "DEGRADED": "warn", "COOLDOWN": "warn",
             "FAILED": "bad", "BLACKLIST": "bad"}.get(state, "info")
    return '<span class="badge %s">%s</span>' % (color, state)


def _html_table(headers, rows):
    h = "".join("<th>%s</th>" % x for x in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join("<td>%s</td>" % x for x in r) + "</tr>"
    return "<table><tr>%s</tr>%s</table>" % (h, body)


def _dashboard_body(cfg):
    h = services.get_status(cfg)
    pool = h["pool"]
    src = h["sources"]
    sched = h["scheduler"]
    diag = h["diagnostics"]
    mihomo = h["mihomo"]
    metrics = (
        '<div class="metrics">'
        '<div class="metric"><div class="v">%d</div>'
        '<div class="k">最终池节点</div></div>'
        '<div class="metric"><div class="v">%s</div>'
        '<div class="k">成功率</div></div>'
        '<div class="metric"><div class="v">%d</div>'
        '<div class="k">数据源（降级 %d）</div></div>'
        '<div class="metric"><div class="v">%s</div>'
        '<div class="k">Mihomo</div></div></div>'
    ) % (pool["count"],
         ("%s%%" % (pool["success_rate"] * 100)) if pool.get("success_rate")
         is not None else "-",
         src["total"], src["degraded"],
         "运行中" if mihomo["running"] else "已停止")
    last = ('<div class="card"><b>最近刷新</b> %s · 上次失败原因：<b>%s</b></div>'
            % (h["last_refresh"] or "(无)", h["last_failure_reason"] or "(无)"))
    sched_rows = [[name, v.get("period_s"),
                   "%s" % v.get("next_in_s")] for name, v in sched.items()]
    diag_rows = [[k, "存在" if v else "缺失"] for k, v in diag.items()]
    return (metrics + last
            + '<div class="card"><b>调度任务</b>'
            + _html_table(["任务", "周期(秒)", "下次运行(秒)"], sched_rows)
            + "</div>"
            + '<div class="card"><b>诊断产物</b>'
            + _html_table(["文件", "状态"], diag_rows) + "</div>")


def _node_body(cfg):
    data = services.get_nodes(cfg)
    rows = []
    for n in data["nodes"]:
        sc = n.get("score") or {}
        ss = n.get("service_score") or {}
        rows.append([
            n["node_id"][:16], _state_badge(n.get("life_state", "?")),
            sc.get("overall", "-"), ss.get("overall", "-"),
            ",".join(n.get("capability") or []),
            (n.get("source") or "-")[:24], n.get("protocol") or "-",
            n.get("success_count", 0), n.get("fail_count", 0),
        ])
    return ('<div class="card"><b>节点（共 %d）· 评分仅排序，不改准入</b>%s</div>'
            % (data["count"],
               _html_table(["node_id", "状态", "score", "service",
                            "capability", "source", "protocol", "成功",
                            "失败"], rows) or "(empty)"))


def _source_body(cfg):
    data = services.get_sources(cfg)
    rows = []
    for s in data["sources"]:
        rows.append([
            s["source"][:48], _state_badge(s.get("state", "ACTIVE")),
            s.get("source_score", "-"),
            ("%s%%" % round(float(s.get("alive_rate", 0)) * 100, 1))
            if s.get("alive_rate") is not None else "-",
            s.get("valid_nodes", 0), s.get("alive_nodes", 0),
            "隔离" if s.get("quarantine") else "正常",
        ])
    return ('<div class="card"><b>数据源（共 %d）</b>%s</div>'
            % (data["count"],
               _html_table(["source", "状态", "评分", "alive_rate", "valid",
                            "alive", "隔离"], rows) or "(empty)"))


def _events_body(cfg):
    data = services.get_events(cfg, limit=200)
    rows = []
    for e in data["events"]:
        badge = _state_badge(e.get("result", "?"))
        if e.get("type") == "refresh":
            badge = ('<span class="badge %s">%s</span>'
                     % ("ok" if e.get("result") == "success" else "bad",
                        "发布" if e.get("result") == "success" else "失败"))
        rows.append([
            e.get("ts", "-"), badge, e.get("type", "-"),
            e.get("node_id", "-")[:16], e.get("detail", ""),
        ])
    return ('<div class="card"><b>事件（成功/失败/状态变化，最近 %d）</b>%s</div>'
            % (len(rows), _html_table(["时间", "结果", "类型", "node_id",
                                       "明细"], rows) or "(empty)"))


def _diagnostic_body(cfg):
    d = api_diagnostic(cfg)
    rs = d["refresh_state"] or {}
    return ('<div class="card"><b>refresh_state</b><pre>%s</pre></div>'
            '<div class="card"><b>source_health</b><pre>%s</pre></div>'
            '<div class="card"><b>detect.log（尾部）</b><pre>%s</pre></div>'
            % (json.dumps(rs, ensure_ascii=False, indent=2),
               json.dumps(d["source_health"], ensure_ascii=False, indent=2)
               if d["source_health"] else "(none)",
               d["detect_log_tail"]))


def _mobile_body(cfg):
    """V0.4-4：Android APK Web 控制入口页（/m/）——仅控制，不直接操作底层。"""
    h = services.get_status(cfg)
    pool = h["pool"]
    return ('<div class="card mob"><b>chatgpt-pool 控制台</b>'
            '<div style="margin:10px 0;">最终池：%d 节点 · 成功率：%s · '
            'Mihomo：%s</div>'
            '<button data-act="refresh">手动刷新</button>'
            '<button data-act="start">启动 Mihomo</button>'
            '<button data-act="stop">停止 Mihomo</button>'
            '<div id="mob-result" style="margin-top:10px;font-size:13px;'
            'color:#8fa3c4;">（操作结果将显示在这里）</div></div>'
            '<script>'
            'document.querySelectorAll("button[data-act]").forEach(function(b){'
            'b.onclick=function(){var r=document.getElementById("mob-result");'
            'r.textContent="执行中…";'
            'fetch("/api/"+b.getAttribute("data-act"),{method:"POST"})'
            '.then(function(x){return x.json()})'
            '.then(function(d){r.textContent=JSON.stringify(d)})'
            '.catch(function(e){r.textContent="错误: "+e})};});'
            '</script>'
            % (pool["count"],
               ("%s%%" % round(pool["success_rate"] * 100, 1))
               if pool.get("success_rate") is not None else "-",
               "运行中" if h["mihomo"]["running"] else "已停止"))


def _render(title, body, cfg):
    return (PAGE % {"title": title, "css": CSS, "body": body})


class DashboardHandler(BaseHTTPRequestHandler):
    cfg = None

    def do_GET(self):
        cfg = self.cfg
        path = self.path.split("?")[0]
        # V0.4 API Layer（含旧路径兼容）
        status, payload = api.dispatch(cfg, "GET", path)
        if status == 200:
            self._send_json(payload)
            return
        page = {
            "/": ("Dashboard", _dashboard_body(cfg)),
            "/node": ("Nodes", _node_body(cfg)),
            "/source": ("Sources", _source_body(cfg)),
            "/events": ("Events", _events_body(cfg)),
            "/diagnostic": ("Diagnostic", _diagnostic_body(cfg)),
            "/m": ("Mobile", _mobile_body(cfg)),
        }
        if path in page:
            title, body = page[path]
            html = _render(title, body, cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self):
        cfg = self.cfg
        length = int(self.headers.get("Content-Length") or 0)
        body = api.parse_body(self.rfile.read(length)) if length else None
        status, payload = api.dispatch(cfg, "POST", self.path, body)
        if status == 404:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self._send_json(payload, status=status)

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw.encode())))
        self.end_headers()
        self.wfile.write(raw.encode())

    def log_message(self, fmt, *args):
        log.info("dashboard %s", fmt % args)


def start_dashboard(cfg, port=None, bind="127.0.0.1"):
    """启动本机 Web 管理服务（仅 127.0.0.1，V0.4 端口保持 18888/18889）。"""
    port = port if port is not None else cfg.web_port
    handler = type("Handler", (DashboardHandler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer((bind, int(port)), handler)
    return httpd


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()
