"""V0.3 源管理系统（§17/§18/§19/§20/§21/§24）。

- Source Lifecycle（§17）：ACTIVE→DEGRADED→DISABLED；质量恢复可回 ACTIVE。
- 源评分（§18）：source_score 0-100 = 有效节点数(40) + alive_rate(40)
  + 更新频率(10) + 历史质量(10)。
- 连续 7 次刷新 alive_rate < 5% → DEGRADED（§20）。
- 源黑白名单（§21）：policy=deny / state=DISABLED → 禁止抓取。
- 源隔离（§24）：某源本轮节点全部失败（alive=0 且 valid>0）→ 隔离该源
  （quarantine 标记 + DEGRADED），而不是删除全部节点。
- 目标源数量（§19）：20-30 个优质源，不是无限添加。
"""
import logging
import time

from lifecycle_store import SourceLifecycleStore
from models import SourceState

log = logging.getLogger("pool.source_mgmt")

LOW_ALIVE_RATE = 0.05        # alive_rate < 5% 视为低质（§20）
CONSECUTIVE_LOW_LIMIT = 7    # 连续 7 次刷新低质 → DEGRADED（§20）
VALID_NODE_TARGET = 50       # 有效节点数满分基准（§18）
ALIVE_RATE_TARGET = 0.20     # alive_rate 满分基准
HISTORY_LIMIT = 30
TARGET_SOURCE_MIN = 20       # 目标优质源数量（§19）
TARGET_SOURCE_MAX = 30


def compute_source_score(rec):
    """§18：有效节点数(40) + alive_rate(40) + 更新频率(10) + 历史质量(10)。"""
    valid = float(rec.get("valid_nodes", 0) or 0)
    alive = float(rec.get("alive_rate", 0) or 0)
    fetched = bool(rec.get("fetch"))
    parsed = int(rec.get("parsed", 0) or 0)
    hist = rec.get("score_history") or []
    mean = (sum(float(h.get("source_score", 0) or 0) for h in hist)
            / len(hist)) if hist else 0.0
    score = (min(valid / VALID_NODE_TARGET, 1.0) * 40.0
             + min(alive / ALIVE_RATE_TARGET, 1.0) * 40.0
             + (10.0 if fetched and parsed > 0 else 0.0)
             + mean * 0.10)
    return round(score, 1)


def update_source_lifecycle(cfg, source_health, round_id=""):
    """refresh 后更新源生命周期/评分/隔离（§17/§20/§24）。

    source_health：run/source_health.json 格式（V0.2.2 P0-05 不变）。
    """
    sc = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for src, h in source_health.items():
        rec = sc.get(src) or {"state": SourceState.ACTIVE.value,
                              "consecutive_low": 0,
                              "score_history": [], "quarantine": False}
        alive_rate = float(h.get("alive_rate", 0) or 0)
        valid = int(h.get("valid", 0) or 0)
        alive = int(h.get("alive", 0) or 0)
        rec.update(fetch=bool(h.get("fetch")), parsed=int(h.get("parsed", 0) or 0),
                   valid_nodes=valid, alive_nodes=alive,
                   alive_rate=alive_rate, last_refresh=now)
        rec["source_score"] = compute_source_score(rec)
        # §20：连续低质降级；恢复正常回 ACTIVE（§17）
        if alive_rate < LOW_ALIVE_RATE and valid > 0:
            rec["consecutive_low"] = int(rec.get("consecutive_low", 0) or 0) + 1
            if int(rec["consecutive_low"]) >= CONSECUTIVE_LOW_LIMIT:
                rec["state"] = SourceState.DEGRADED.value
                log.warning("source degraded (%d low rounds): %s",
                            rec["consecutive_low"], src)
        elif alive_rate < LOW_ALIVE_RATE and valid == 0:
            # 本轮无有效节点（源可能失效）；累计但暂不降级
            rec["consecutive_low"] = int(rec.get("consecutive_low", 0) or 0) + 1
        else:
            rec["consecutive_low"] = 0
            if rec.get("state") == SourceState.DEGRADED.value:
                rec["state"] = SourceState.ACTIVE.value
        # §24：源隔离——本轮全部节点失败（alive=0 且 valid>0）
        if alive == 0 and valid > 0:
            rec["quarantine"] = True
            rec["state"] = SourceState.DEGRADED.value
            log.warning("source quarantined (all nodes failed): %s", src)
        else:
            rec["quarantine"] = False
        hist = rec.setdefault("score_history", [])
        hist.append({"t": now, "source_score": rec["source_score"],
                     "alive_rate": alive_rate})
        rec["score_history"] = hist[-HISTORY_LIMIT:]
        sc.upsert(src, rec)
    sc.save()


def is_allowed(cfg, source, sc=None):
    """§21 黑白名单：policy=deny 或 state=DISABLED → 禁止抓取。"""
    sc = sc or SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
    rec = sc.get(source)
    if not rec:
        return True
    if rec.get("policy") == "deny":
        return False
    if rec.get("state") == SourceState.DISABLED.value:
        return False
    return True


def filter_allowed(cfg, fetchers):
    """按策略过滤抓取器列表（§21），返回允许的 [(name, fn), ...]。"""
    sc = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
    out = []
    for name, fn in fetchers:
        if is_allowed(cfg, name, sc=sc):
            out.append((name, fn))
        else:
            log.info("source blocked by policy: %s", name)
    return out


def source_count_state(cfg):
    """§19：当前源数量与状态统计（目标 20-30 优质源）。"""
    sc = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
    recs = sc.all()
    return {
        "total": len(recs),
        "active": sum(1 for r in recs.values()
                      if r.get("state") == SourceState.ACTIVE.value),
        "degraded": sum(1 for r in recs.values()
                        if r.get("state") == SourceState.DEGRADED.value),
        "disabled": sum(1 for r in recs.values()
                        if r.get("state") == SourceState.DISABLED.value),
        "target_range": [TARGET_SOURCE_MIN, TARGET_SOURCE_MAX],
    }
