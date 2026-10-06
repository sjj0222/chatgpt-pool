"""V0.3 Web 管理页面与 API（§36/§37）。

- 仅本机访问 127.0.0.1（生产 18888 / 测试 18889）。
- 页面：Dashboard（ACTIVE 数量/成功率/系统健康）、Node（状态/score/能力/历史）、
  Source（源评分/alive_rate/状态）、Diagnostic（V0.2.2.2 诊断结果）。
- API：/api/node /api/source /api/history /api/health。
- 纯标准库（http.server），不引入 DB/Docker/云/多节点同步/用户认证（§一）。
"""
import json
import logging
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
"""

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>%(title)s · chatgpt-pool</title>
<style>%(css)s</style></head><body>
<h1>%(title)s</h1>
<div class="sub">chatgpt-pool V0.3 · 本机管理页面（仅 127.0.0.1）</div>
<nav><a href="/">Dashboard</a><a href="/node">Node</a>
<a href="/source">Source</a><a href="/diagnostic">Diagnostic</a>
<a href="/api/health">API health</a></nav>
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


def api_health(cfg):
    """/api/health：系统健康。"""
    import scheduler as sched_mod
    pool = _read_json(cfg.sub("run/final/current.json"))
    refresh_state = _read_json(cfg.sub("run/refresh_state.json"))
    src = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    sources = src.get("sources", {})
    return {
        "pool": {
            "count": len(pool.get("nodes", [])) if pool else 0,
            "round_id": pool.get("round_id") if pool else None,
        },
        "last_refresh": (refresh_state or {}).get("last_refresh"),
        "last_failure_reason": (refresh_state or {}).get(
            "last_failure_reason"),
        "sources": {"total": len(sources),
                    "degraded": sum(1 for r in sources.values()
                                    if r.get("state") == "DEGRADED")},
        "scheduler": sched_mod.schedule_status(cfg).get("schedule", {}),
        "diagnostics": {
            "source_health": os.path.isfile(
                cfg.sub("run/source_health.json")),
            "detect_log": os.path.isfile(cfg.sub("run/detect.log")),
            "refresh_state": os.path.isfile(cfg.sub("run/refresh_state.json")),
        },
    }


def api_node(cfg):
    """/api/node：节点生命周期（状态/score/能力/历史）。"""
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    nodes = lc.get("nodes", {})
    return {"schema_version": lc.get("schema_version"),
            "count": len(nodes),
            "nodes": [{"node_id": nid, **rec}
                      for nid, rec in sorted(nodes.items())]}


def api_source(cfg):
    """/api/source：源评分/alive_rate/状态。"""
    src = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    sources = src.get("sources", {})
    return {"schema_version": src.get("schema_version"),
            "count": len(sources),
            "sources": [{"source": name, **rec}
                        for name, rec in sorted(sources.items())]}


def api_history(cfg):
    """/api/history：节点历史信用（V0.2.1 node_history.json）。"""
    hist = _read_json(cfg.sub("state/node_history.json"))
    if hist is None:
        return {"count": 0, "history": {}}
    return {"count": len(hist), "history": hist}


def api_diagnostic(cfg):
    """诊断数据（V0.2.2.2 产物）。"""
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
    h = api_health(cfg)
    pool = h["pool"]
    src = h["sources"]
    sched = h["scheduler"]
    diag = h["diagnostics"]
    metrics = (
        '<div class="metrics">'
        '<div class="metric"><div class="v">%d</div>'
        '<div class="k">ACTIVE 池节点</div></div>'
        '<div class="metric"><div class="v">%d</div>'
        '<div class="k">数据源总数（降级 %d）</div></div>'
        '<div class="metric"><div class="v">%s</div>'
        '<div class="k">最近刷新</div></div>'
        '<div class="metric"><div class="v">%s</div>'
        '<div class="k">上次失败原因</div></div></div>'
    ) % (pool["count"], src["total"], src["degraded"],
         pool["round_id"] or "(无)", h["last_failure_reason"] or "(无)")
    sched_rows = [[name, v.get("period_s"),
                   "%s" % v.get("next_in_s")] for name, v in sched.items()]
    diag_rows = [[k, "存在" if v else "缺失"] for k, v in diag.items()]
    return (metrics
            + '<div class="card"><b>调度任务（§23）</b>'
            + _html_table(["任务", "周期(秒)", "下次运行(秒)"], sched_rows)
            + "</div>"
            + '<div class="card"><b>诊断产物（V0.2.2.2）</b>'
            + _html_table(["文件", "状态"], diag_rows) + "</div>")


def _node_body(cfg):
    data = api_node(cfg)
    rows = []
    for n in data["nodes"]:
        sc = n.get("score") or {}
        ss = n.get("service_score") or {}
        rows.append([
            n["node_id"][:16], _state_badge(n.get("life_state", "?")),
            sc.get("overall", "-"), ss.get("overall", "-"),
            ",".join(n.get("capability") or []),
            n.get("success_count", 0), n.get("fail_count", 0),
        ])
    return ('<div class="card"><b>节点生命周期（共 %d）· 评分仅排序，'
            '不改准入</b>%s</div>'
            % (data["count"],
               _html_table(["node_id", "状态", "score", "service",
                            "capability", "成功", "失败"], rows) or "(empty)"))


def _source_body(cfg):
    data = api_source(cfg)
    rows = []
    for s in data["sources"]:
        rows.append([
            s["source"][:48], _state_badge(s.get("state", "ACTIVE")),
            s.get("source_score", "-"), s.get("alive_rate", "-"),
            s.get("valid_nodes", 0), s.get("alive_nodes", 0),
            "隔离" if s.get("quarantine") else "正常",
        ])
    return ('<div class="card"><b>数据源（共 %d）· 目标 20-30 优质源</b>%s</div>'
            % (data["count"],
               _html_table(["source", "状态", "评分", "alive_rate", "valid",
                            "alive", "隔离"], rows) or "(empty)"))


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


def _render(title, body, cfg):
    return (PAGE % {"title": title, "css": CSS, "body": body})


class DashboardHandler(BaseHTTPRequestHandler):
    cfg = None

    def do_GET(self):
        cfg = self.cfg
        path = self.path.split("?")[0]
        api = {
            "/api/health": lambda: api_health(cfg),
            "/api/node": lambda: api_node(cfg),
            "/api/source": lambda: api_source(cfg),
            "/api/history": lambda: api_history(cfg),
            "/api/diagnostic": lambda: api_diagnostic(cfg),
        }
        page = {
            "/": ("Dashboard", _dashboard_body(cfg)),
            "/node": ("Node", _node_body(cfg)),
            "/source": ("Source", _source_body(cfg)),
            "/diagnostic": ("Diagnostic", _diagnostic_body(cfg)),
        }
        if path in api:
            payload = json.dumps(api[path](), ensure_ascii=False, indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload.encode())))
            self.end_headers()
            self.wfile.write(payload.encode())
            return
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

    def log_message(self, fmt, *args):
        log.info("dashboard %s", fmt % args)


def start_dashboard(cfg, port=None, bind="127.0.0.1"):
    """启动本机 Web 管理服务（§36：仅 127.0.0.1）。"""
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
