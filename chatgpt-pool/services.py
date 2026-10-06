"""V0.4 Service Layer（Phase 0.4-1）。

四层架构：Core Engine（pipeline/scoring/lifecycle/…）→ **Service Layer** →
API Layer → UI Layer。核心代码（pipeline 等）不依赖本模块与 UI。

服务：
- get_status / get_nodes / get_sources / get_events（读模型）
- refresh / start / stop（控制模型，锁防并发）
- export_config / import_config（0.4-6 Local First 数据安全）
- sync_payload（0.4-5 云同步接口设计，默认关闭，白名单）
"""
import json
import logging
import os
import time

log = logging.getLogger("pool.services")


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 读取模型

def get_status(cfg, mihomo=None):
    """/api/status：pool 状态、成功率、最近刷新、调度、mihomo、诊断产物。"""
    import scheduler as sched_mod
    pool = _read_json(cfg.sub("run/final/current.json"))
    rs = _read_json(cfg.sub("run/refresh_state.json")) or {}
    src = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    sources = src.get("sources", {})
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    nodes = lc.get("nodes", {})
    ok = sum(int(n.get("success_count", 0) or 0) for n in nodes.values())
    fail = sum(int(n.get("fail_count", 0) or 0) for n in nodes.values())
    success_rate = (round(ok / (ok + fail), 4) if ok + fail > 0 else 0.0)
    if mihomo is None:
        try:
            from mihomo_svc import ProductionMihomo
            mihomo = ProductionMihomo(cfg).status()
        except Exception:
            mihomo = {"running": False}
    return {
        "pool": {
            "count": len(pool.get("nodes", [])) if pool else 0,
            "round_id": pool.get("round_id") if pool else None,
            "success_rate": success_rate,
        },
        "mihomo": {"running": bool(mihomo.get("running")),
                   "pid": mihomo.get("pid"),
                   "ports": {"proxy": cfg.prod_mihomo_port,
                             "api": cfg.mihomo_api_port}},
        "last_refresh": rs.get("last_refresh"),
        "last_failure_reason": rs.get("last_failure_reason"),
        "sources": {"total": len(sources),
                    "degraded": sum(1 for r in sources.values()
                                    if r.get("state") == "DEGRADED")},
        "scheduler": sched_mod.schedule_status(cfg).get("schedule", {}),
        "diagnostics": {
            "source_health": os.path.isfile(
                cfg.sub("run/source_health.json")),
            "detect_log": os.path.isfile(cfg.sub("run/detect.log")),
            "refresh_state": os.path.isfile(cfg.sub("run/refresh_state.json")),
            "runtime_report": os.path.isfile(
                cfg.sub("run/runtime_report.json")),
        },
    }


def get_nodes(cfg, limit=None):
    """/api/nodes：节点列表（完整记录含 node_id/状态/score/source/protocol）。"""
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    nodes = lc.get("nodes", {})
    out = [{"node_id": nid, **rec}
           for nid, rec in sorted(nodes.items())]
    if limit:
        out = out[:limit]
    return {"schema_version": lc.get("schema_version"),
            "count": len(out), "nodes": out}


def get_sources(cfg):
    """/api/sources：源评分/alive_rate/状态。"""
    src = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    sources = src.get("sources", {})
    return {"schema_version": src.get("schema_version"),
            "count": len(sources),
            "sources": [{"source": name, **rec}
                        for name, rec in sorted(sources.items())]}


def get_events(cfg, limit=200):
    """/api/events：成功（发布）/失败（节点/轮次）/状态变化（生命周期事件）。

    事件源：refresh_state（发布）、run/failed（失败轮次）、
    state/node_lifecycle.json 节点 events（状态变化）。
    """
    events = []
    rs = _read_json(cfg.sub("run/refresh_state.json")) or {}
    if rs.get("published"):
        events.append({"ts": rs.get("last_refresh") or _now(),
                       "type": "refresh", "result": "success",
                       "detail": "pool_size=%s round=%s"
                       % (rs.get("pool_size"), rs.get("round_id"))})
    elif rs.get("status"):
        events.append({"ts": rs.get("last_refresh") or _now(),
                       "type": "refresh", "result": "failure",
                       "detail": rs.get("status")})
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    for nid, rec in (lc.get("nodes", {}) or {}).items():
        for ev in rec.get("events") or []:
            events.append({"ts": ev.get("ts"), "type": "state_change",
                           "result": ev.get("event"), "node_id": nid,
                           "detail": json.dumps(ev.get("detail"),
                                                ensure_ascii=False)
                           if ev.get("detail") else ""})
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return {"count": len(events), "events": events[:limit]}


# ---------------------------------------------------------------- 控制模型

def refresh(cfg):
    """POST /api/refresh：锁防并发，执行刷新；返回发布摘要。"""
    import pipeline
    import scheduler as sched_mod
    lock = sched_mod.Lock(cfg, "refresh")
    if not lock.acquire():
        return {"ok": False, "error": "REFRESH_LOCKED",
                "detail": "another refresh is running"}
    try:
        return pipeline.run_refresh(cfg)
    finally:
        lock.release()


def start(cfg):
    """POST /api/start：启动生产 Mihomo（受保护：无有效池拒绝启动）。"""
    from mihomo_svc import ProductionMihomo
    svc = ProductionMihomo(cfg)
    return svc.start()


def stop(cfg):
    """POST /api/stop：停止生产 Mihomo（生命周期：等待 PID 退出+端口释放）。"""
    from mihomo_svc import ProductionMihomo
    svc = ProductionMihomo(cfg)
    return svc.stop()


# ---------------------------------------------------------------- 数据安全 0.4-6

CONFIG_EXPORT_KEYS = ("supported_countries_file", "refresh_interval_s",
                      "prod_mihomo_port", "mihomo_api_port",
                      "prod_web_port", "max_nodes_per_round",
                      "target_source_min", "target_source_max",
                      "version", "exported_at")
"""export_config 导出白名单：仅运行参数，不含节点/密码/隐私。"""


def export_config(cfg):
    """导出运行配置（非敏感字段）为可移植 dict。"""
    base = {k: getattr(cfg, k, None) for k in CONFIG_EXPORT_KEYS
            if hasattr(cfg, k)}
    base["exported_at"] = _now()
    base["version"] = "V0.4"
    return base


def import_config(cfg, data):
    """导入配置并落盘（仅白名单字段；Unknown 字段拒绝）。"""
    path = cfg.sub("config_import.json")
    if not isinstance(data, dict):
        raise ValueError("config must be a dict")
    allowed = {k: data[k] for k in CONFIG_EXPORT_KEYS if k in data}
    allowed["imported_at"] = _now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(allowed, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"imported_keys": sorted(allowed), "path": path}


# ---------------------------------------------------------------- 云同步 0.4-5

SYNC_ALLOWED_KEYS = ("score", "source_credit", "version", "anonymous_stats")
"""sync_payload 白名单：匿名统计/score/source 信用/版本。

禁止：节点密码/用户隐私/完整配置（本函数不读取也不导出）。
"""


def sync_payload(cfg):
    """生成可同步负载（接口设计；默认关闭由调用方控制）。

    返回 dict 只含白名单字段；绝不包含节点 server/port/账号/密码。
    """
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    nodes = lc.get("nodes", {})
    scores = [n.get("score", {}).get("overall", 0.0)
              for n in nodes.values() if n.get("score")]
    src = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    sources = src.get("sources", {})
    return {
        "version": "V0.4",
        "anonymous_stats": {
            "node_count": len(nodes),
            "active_count": sum(1 for n in nodes.values()
                                if n.get("life_state") == "ACTIVE"),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        },
        "source_credit": {name: rec.get("source_score")
                          for name, rec in sources.items()},
        "score": scores[-200:],
    }
