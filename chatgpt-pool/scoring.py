"""V0.3 节点质量评分系统（§7/§8/§9/§15/§28）。

- NodeQualityScore（§7）：overall = 稳定性40% + 可用率30% + 延迟20% + 信用10%。
  可用率窗口：5m/1h/24h/7d 加权（近窗权重大，§15 时间衰减）。
  breakdown 必须可单独展示（§28），不能只显示总分。
- ServiceScore（§8）：多维服务分（chatgpt/github/claude/gemini/web），禁止只有总分。
- Capability（§9）：服务分 ≥ 阈值 → 能力标签。
- 评分仅用于排序与分层（P1-03/§7），绝不改变 V5.2 准入与淘汰规则。
"""
import logging
import time

from models import (AVAILABILITY_WINDOWS, Capability, ChatgptStatus,
                    NodeQualityScore, SCORE_WEIGHTS, ServiceScore,
                    TIME_DECAY)

log = logging.getLogger("pool.scoring")

# 窗口权重（近窗更重；§15：24h 70% + 30d 30% 的时间衰减由窗口加权体现）
WINDOW_WEIGHTS = {"5m": 0.40, "1h": 0.30, "24h": 0.20, "7d": 0.10}
LATENCY_BASE_MS = 300.0      # ≤300ms 得满分
LATENCY_SLOPE = 20.0         # 每超 20ms 扣 1 分
CREDIT_BASE = 60.0
CREDIT_SCORE_HISTORY_W = 0.20
CREDIT_RECOVER_BONUS = 2.0
CREDIT_RECOVER_CAP = 20.0
CREDIT_LONG_TERM_BONUS = 5.0
SERVICE_ACTIVE_THRESHOLD = 70.0     # 能力标签阈值（§9）
SERVICE_STRICT_PASS = 95.0
SERVICE_NETWORK_PASS = 60.0
SCORE_HISTORY_LIMIT = 50
ATTEMPTS_LIMIT = 200


def _clip(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def stability_score(rec):
    """稳定性：成功次数 / 总检测次数 × 100（§7）。"""
    ok = int(rec.get("success_count", 0) or 0)
    fail = int(rec.get("fail_count", 0) or 0)
    total = ok + fail
    if total == 0:
        return 0.0
    return round(100.0 * ok / total, 1)


def _attempts(rec):
    return rec.get("recent_attempts") or []


def availability_score(rec, now=None):
    """可用率：5m/1h/24h/7d 窗口加权；无数据窗口跳过并重归一化（§7/§15）。"""
    now = now if now is not None else int(time.time())
    attempts = _attempts(rec)
    if not attempts:
        return 0.0
    window_avail = {}
    for name, secs in AVAILABILITY_WINDOWS.items():
        cutoff = now - secs
        ws = [a for a in attempts if int(a.get("t", 0)) >= cutoff]
        if ws:
            window_avail[name] = (
                100.0 * sum(1 for a in ws if a.get("ok"))
                / len(ws))
    if not window_avail:
        return 0.0
    weight_sum = 0.0
    acc = 0.0
    for name, w in WINDOW_WEIGHTS.items():
        if name in window_avail:
            acc += window_avail[name] * w
            weight_sum += w
    if weight_sum <= 0:
        return 0.0
    return round(acc / weight_sum, 1)


def latency_score(rec):
    """延迟得分：平均延迟 ≤300ms → 100；每超 20ms 扣 1 分，最低 0。"""
    lats = [a.get("latency_ms") for a in _attempts(rec)
            if a.get("latency_ms") is not None]
    if not lats:
        return 0.0
    avg = sum(lats) / len(lats)
    return round(_clip(100.0 - max(0.0, avg - LATENCY_BASE_MS)
                       / LATENCY_SLOPE), 1)


def credit_score(rec):
    """历史信用（§14/§15）：基础分 + 历史评分均值 + 恢复记录奖励 + 长期稳定奖励。

    §15 时间衰减：24h 内评分权重 70%、30 天历史 30%。
    """
    now = int(time.time())
    hist = rec.get("score_history") or []
    now_ts = [h for h in hist if int(h.get("t", 0)) >= now - 86400]
    old_ts = [h for h in hist if now - 30 * 86400 <= int(h.get("t", 0))
              < now - 86400]
    mean = lambda xs: (sum(x.get("overall", 0) for x in xs) / len(xs)
                       if xs else 0.0)
    recent = mean(now_ts)
    history = mean(old_ts)
    score = CREDIT_BASE + (recent * TIME_DECAY["recent_24h"]
                           + history * TIME_DECAY["history_30d"]) \
        * CREDIT_SCORE_HISTORY_W
    # 恢复记录奖励（§14：恢复记录是信用的一部分）
    score += min(int(rec.get("recover_count", 0) or 0)
                 * CREDIT_RECOVER_BONUS, CREDIT_RECOVER_CAP)
    # 长期稳定奖励：大量成功且当前无连续失败
    if int(rec.get("success_count", 0) or 0) >= 30 \
            and int(rec.get("fail_streak", 0) or 0) == 0:
        score += CREDIT_LONG_TERM_BONUS
    return round(_clip(score), 1)


def compute_quality_score(rec, now=None):
    """计算 NodeQualityScore（含 breakdown，§28）。"""
    q = NodeQualityScore(
        stability=stability_score(rec),
        availability=availability_score(rec, now),
        latency=latency_score(rec),
        credit=credit_score(rec),
        windows={name: None for name in AVAILABILITY_WINDOWS},
    )
    q.compute_overall(SCORE_WEIGHTS)
    return q


def quality_score_of_rec(rec):
    """生命周期记录 → overall 分（空记录返回 0.0，用于排序兜底）。"""
    if not rec:
        return 0.0
    return compute_quality_score(rec).overall


def compute_service_score(rec, chatgpt_status=""):
    """ServiceScore（§8）：chatgpt 取本轮检测结果，其余服务取 probe 记录。

    无数据（UNKNOWN/None）的服务不参与；overall = 已知服务均值。
    """
    services = {}
    if chatgpt_status == ChatgptStatus.STRICT_PASS.value:
        services["chatgpt"] = SERVICE_STRICT_PASS
    elif chatgpt_status == ChatgptStatus.NETWORK_PASS.value:
        services["chatgpt"] = SERVICE_NETWORK_PASS
    elif chatgpt_status == ChatgptStatus.FAIL.value:
        services["chatgpt"] = 0.0
    probes = rec.get("probe_scores") or {}
    for name in ("github", "claude", "gemini", "web"):
        v = probes.get(name)
        if v is not None:
            services[name] = round(_clip(float(v)), 1)
    overall = round(sum(services.values()) / len(services), 1) \
        if services else 0.0
    return ServiceScore(overall=overall, services=services)


def capability_tags(service_score, threshold=SERVICE_ACTIVE_THRESHOLD):
    """§9：服务分 ≥ 阈值 → 能力标签。"""
    return sorted(name for name, v in service_score.services.items()
                  if v >= threshold)


def record_attempt(lc_rec, ok, latency_ms=None, ts=None):
    """追加一次检测记录（可用率窗口的数据来源，保留最近 ATTEMPTS_LIMIT）。"""
    attempts = lc_rec.setdefault("recent_attempts", [])
    attempts.append({"t": ts if ts is not None else int(time.time()),
                     "ok": bool(ok),
                     "latency_ms": latency_ms})
    lc_rec["recent_attempts"] = attempts[-ATTEMPTS_LIMIT:]


def record_score_history(lc_rec, score, ts=None):
    """追加评分历史（§14 score_history，保留最近 SCORE_HISTORY_LIMIT）。"""
    hist = lc_rec.setdefault("score_history", [])
    hist.append({"t": ts if ts is not None else int(time.time()),
                 "overall": score})
    lc_rec["score_history"] = hist[-SCORE_HISTORY_LIMIT:]
