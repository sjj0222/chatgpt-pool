"""V0.4 API Layer（Phase 0.4-3）。

- GET  /api/status /api/nodes /api/sources /api/events
- POST /api/refresh /api/start /api/stop
- 附加：/api/export_config /api/import_config（0.4-6）
- 全部 JSON；仅本机 127.0.0.1（绑定由 start 方控制）。
- 本层不直接读 Core 文件——一律经 services（Service Layer）。
"""
import json
import logging
import urllib.parse

import services

log = logging.getLogger("pool.api")


def route(cfg, method, path, body=None):
    """路由单个 API 请求；返回 (status, payload_dict)。"""
    GET = {
        "/api/status": lambda: services.get_status(cfg),
        "/api/nodes": lambda: services.get_nodes(cfg),
        "/api/sources": lambda: services.get_sources(cfg),
        "/api/events": lambda: services.get_events(cfg),
        "/api/export_config": lambda: services.export_config(cfg),
        # V0.3 旧路径兼容（行为不变）
        "/api/health": lambda: services.get_status(cfg),
        "/api/node": lambda: services.get_nodes(cfg),
        "/api/source": lambda: services.get_sources(cfg),
        "/api/history": lambda: _history(cfg),
        "/api/diagnostic": lambda: _diagnostic(cfg),
    }
    POST = {
        "/api/refresh": lambda: services.refresh(cfg),
        "/api/start": lambda: services.start(cfg),
        "/api/stop": lambda: services.stop(cfg),
        "/api/import_config": lambda: services.import_config(cfg, body),
    }
    if method == "GET" and path in GET:
        return 200, GET[path]()
    if method == "POST" and path in POST:
        try:
            return 200, POST[path]()
        except Exception as e:                       # 操作失败 → 可解释错误
            log.exception("api %s %s failed", method, path)
            return 500, {"ok": False, "error": str(e)}
    return 404, {"ok": False, "error": "not found"}


def parse_body(raw, content_type=""):
    """解析 JSON 请求体（POST）。"""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _read(path):
    import json as _json
    import os as _os
    if not _os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError):
        return None


def _tail(path, n=4000):
    import os as _os
    if not _os.path.isfile(path):
        return "(none)"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()[-n:]
    except OSError:
        return "(unreadable)"


def _history(cfg):
    hist = _read(cfg.sub("state/node_history.json"))
    if hist is None:
        return {"count": 0, "history": {}}
    return {"count": len(hist), "history": hist}


def _diagnostic(cfg):
    return {
        "refresh_state": _read(cfg.sub("run/refresh_state.json")),
        "source_health": _read(cfg.sub("run/source_health.json")),
        "detect_log_tail": _tail(cfg.sub("run/detect.log")),
    }


def dispatch(cfg, method, path, raw_body=None):
    """HTTP 入口：方法+路径 → (status, dict)。"""
    p = urllib.parse.urlparse(path)
    return route(cfg, method, p.path, raw_body)
