"""V0.4.1 Phase 3：真实业务质量统计（run/service_quality.json）。

记录各服务（ChatGPT/GitHub/Claude/Gemini/Web）的探测指标：
success_rate / avg_latency / fail_reason 计数。

- 只统计，不改变任何节点行为与核心逻辑（V0.4.1 禁止事项）。
- 累积写盘；异常不中断主流程。
"""
import json
import logging
import os
import time

log = logging.getLogger("pool.service_quality")

QUALITY_PATH = "run/service_quality.json"


def _read(path):
    if not os.path.isfile(path):
        return {"services": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"services": {}}
    data.setdefault("services", {})
    return data


def record(cfg, service, ok, latency_ms=None, fail_reason=None):
    """记录一次服务探测结果并累积落盘。"""
    data = _read(cfg.sub(QUALITY_PATH))
    s = data["services"].setdefault(service, {
        "success": 0, "fail": 0, "latency_sum_ms": 0.0,
        "latency_n": 0, "fail_reason": {},
        "last_ts": None,
    })
    if ok:
        s["success"] += 1
        if latency_ms is not None:
            s["latency_sum_ms"] += float(latency_ms)
            s["latency_n"] += 1
    else:
        s["fail"] += 1
        if fail_reason:
            s["fail_reason"][fail_reason] = \
                s["fail_reason"].get(fail_reason, 0) + 1
    s["last_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path = cfg.sub(QUALITY_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return data


def get_report(cfg):
    """汇总报告：成功率 / 平均延迟 / 失败原因。"""
    data = _read(cfg.sub(QUALITY_PATH))
    out = {"services": {}}
    for name, s in data["services"].items():
        total = s["success"] + s["fail"]
        out["services"][name] = {
            "service": name,
            "success_rate": round(s["success"] / total, 4) if total else 0.0,
            "avg_latency": round(s["latency_sum_ms"] / s["latency_n"], 1)
            if s["latency_n"] else 0.0,
            "fail_reason": s.get("fail_reason", {}),
            "samples": total,
            "last_ts": s.get("last_ts"),
        }
    return out
