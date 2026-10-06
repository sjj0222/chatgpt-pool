"""V0.3 异常隔离与回滚（§16/§25/§30/§13）。

- 环境异常发布守卫（§16）：某阶段失败 ≥80% 且失败节点 ≥10 → 本轮检测不可信，
  拒绝发布新池、保留旧池（不批量删除节点）。
- 节点关联分析（§25）：node_group（IP 段 / 来源 / ASN）用于识别"同批死亡"。
- 快照回滚（§30）：发布成功后保存 run/history/pool/{ts}.json，异常可回滚。
- 过期清理（§13）：FAILED 30 天无恢复 → EXPIRED → 移除（记事件）。
"""
import glob
import json
import logging
import os
import shutil
import time

from lifecycle import FAILED_EXPIRY_SECONDS, LifecycleEngine
from lifecycle_store import NodeLifecycleStore
from models import LifecycleState, NodeEventType

log = logging.getLogger("pool.isolation")

SNAPSHOT_KEEP = 20           # 保留最近 N 份快照
IP_SEGMENT_BITS = 24         # /24 关联分组（§25）
GROUP_FAIL_RATIO_WARN = 0.8  # 组内失败率预警阈值


def node_group(node):
    """§25：节点关联分组——IP 段 / 来源 / ASN（ASN 未知时 None）。"""
    ip = (node.get("server") or "").strip()
    seg = ""
    if ip and ip.replace(".", "").isdigit():
        parts = ip.split(".")
        if len(parts) == 4:
            seg = ".".join(parts[:3]) + ".0/%d" % IP_SEGMENT_BITS
    return {"ip_segment": seg, "source": node.get("source", ""),
            "asn": None}


def group_stats(cfg):
    """§25：按分组统计节点数与失败率（诊断，不改行为）。"""
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    groups = {}
    for rec in lc.all().values():
        g = rec.get("node_group") or {}
        key = (g.get("ip_segment"), g.get("source"))
        st = groups.setdefault(key, {"count": 0, "failed": 0})
        st["count"] += 1
        if rec.get("life_state") in (LifecycleState.FAILED.value,
                                     LifecycleState.BLACKLIST.value):
            st["failed"] += 1
    for st in groups.values():
        st["fail_ratio"] = (round(st["failed"] / st["count"], 2)
                            if st["count"] else 0.0)
    return {"groups": [{("ip_segment" if k[0] else "source"): k[0] or k[1],
                        **v} for k, v in groups.items()]}


def snapshot_pool(cfg, round_id=""):
    """§30：发布成功后保存当前池快照 run/history/pool/{ts}.json。"""
    src = cfg.sub("run/final/current.json")
    if not os.path.isfile(src):
        return None
    ts = time.strftime("%Y%m%d%H%M%S")
    hist_dir = cfg.sub("run/history/pool")
    os.makedirs(hist_dir, exist_ok=True)
    dst = os.path.join(hist_dir, "%s_%s.json" % (ts, round_id))
    shutil.copy2(src, dst)
    # 保留最近 SNAPSHOT_KEEP 份
    snaps = sorted(glob.glob(os.path.join(hist_dir, "*.json")))
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dst


def rollback_pool(cfg, ts=None):
    """§30：恢复上一版本池（最新快照或指定 ts）；返回恢复的快照路径。"""
    hist_dir = cfg.sub("run/history/pool")
    snaps = sorted(glob.glob(os.path.join(hist_dir, "*.json")))
    if not snaps:
        return None
    chosen = snaps[-1]
    if ts:
        for s in snaps:
            if ts in s:
                chosen = s
                break
    try:
        data = json.load(open(chosen, encoding="utf-8"))
        if not data.get("nodes"):
            log.warning("rollback snapshot empty: %s", chosen)
            return None
    except (OSError, ValueError) as e:
        log.error("rollback snapshot unreadable: %s (%s)", chosen, e)
        return None
    dst = cfg.sub("run/final/current.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(chosen, dst)
    log.info("pool rolled back to %s", chosen)
    return chosen


def cleanup_expired(cfg, now=None):
    """§13：FAILED 30 天无恢复 → EXPIRED → 从 lifecycle 移除（记事件后删除）。"""
    now = now if now is not None else int(time.time())
    eng = LifecycleEngine()
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    removed = []
    for nid, rec in list(lc.all().items()):
        if rec.get("life_state") == LifecycleState.FAILED.value \
                and eng.is_expired(rec, now=now):
            lc.add_event(nid, NodeEventType.EXPIRED.value,
                         {"after_s": now - int(rec.get("last_failure", 0)
                                               or 0)})
            lc.remove(nid)
            removed.append(nid)
    lc.save()
    return removed


def apply_anomaly_guard(cfg, summary):
    """§16：环境异常 → 本轮拒绝发布（检测不可信，保留旧池）。

    在发布门前调用；若命中，summary 置 reason 并返回 True。
    """
    anomaly = summary.get("environment_anomaly")
    if not anomaly or not anomaly.get("detected"):
        return False
    summary["reason"] = "ENVIRONMENT_ANOMALY"
    summary["anomaly"] = anomaly
    log.warning("anomaly guard: refusing publish this round (%s)",
                anomaly.get("stage"))
    return True
