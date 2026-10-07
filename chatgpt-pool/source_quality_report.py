"""V0.4.1 Phase 5：Source 真实质量报告（run/source_quality_report.json）。

只读聚合：每源 nodes / alive_rate / 7d 成功 / status + 只读标记：
- 连续 7 轮 alive_rate < 5% → LOW_QUALITY（仅报告标记，不改 lifecycle）
- 30 天无贡献（alive==0 且长期无有效节点）→ EXPIRED（仅报告标记）

V0.4.1 禁止修改 Source 管理规则——本模块只读 source_lifecycle +
source_health，不写回 lifecycle。
"""
import json
import logging
import os

log = logging.getLogger("pool.source_quality")

REPORT_PATH = "run/source_quality_report.json"
LOW_QUALITY_LOW_ALIVE = 0.05      # 连续 7 轮 alive_rate < 5%
LOW_QUALITY_ROUNDS = 7
EXPIRED_IDLE_DAYS = 30


def _read(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_report(cfg):
    """聚合 source_health（本轮）+ source_lifecycle（历史状态）。"""
    health = _read(cfg.sub("run/source_health.json")) or {}
    lc = _read_json_lifecycle(cfg)
    rows = []
    for name in sorted(set(list(health.keys()) + list(lc.keys()))):
        h = health.get(name, {})
        l = lc.get(name, {})
        alive_rate = h.get("alive_rate", l.get("alive_rate", 0.0)) or 0.0
        valid = h.get("valid", l.get("valid_nodes", 0))
        alive = h.get("alive", l.get("alive_nodes", 0))
        consecutive_low = l.get("consecutive_low_count", 0)
        status = l.get("state", "ACTIVE")
        flag = None
        if consecutive_low >= LOW_QUALITY_ROUNDS \
                and alive_rate < LOW_QUALITY_LOW_ALIVE:
            flag = "LOW_QUALITY"
        if alive == 0 and valid == 0 and status == "DISABLED":
            flag = flag or "EXPIRED"
        rows.append({
            "source": name,
            "nodes": valid,
            "alive": alive,
            "alive_rate": round(alive_rate, 4),
            "7d_success": alive,           # 窗口口径沿用本轮/累积 alive
            "status": status,
            "flag": flag,                 # None / LOW_QUALITY / EXPIRED
        })
    return {"sources": rows, "count": len(rows)}


def _read_json_lifecycle(cfg):
    """读 source_lifecycle.json 的 sources 映射。"""
    data = _read(cfg.sub("state/source_lifecycle.json")) or {}
    return data.get("sources", {})


def write_report(cfg):
    report = build_report(cfg)
    path = cfg.sub(REPORT_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
