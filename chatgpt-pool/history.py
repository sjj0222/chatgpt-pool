"""节点历史与生命周期（V0.2.1 P1-01 / P1-02 / P1-03）。

- 生命周期健康状态：NEW → CHECKING → GOOD / DEGRADED / BAD → REMOVED（标记）。
  与检测状态机（Stage，§7）是两个独立维度：Stage 描述单轮检测阶段，
  health 描述节点跨轮历史健康度。
- 禁止一次失败永久删除：历史记录永不因单轮失败被删除；REMOVED 仅显式标记，
  节点可从 DEGRADED/BAD 恢复。
- 评分（P1-03）：score = 成功率×50 + 稳定性×30 + 速度×20，仅用于排序，
  绝不改变 V5.2 淘汰规则与准入条件。
"""
import json
import logging
import os
import time

log = logging.getLogger("pool.history")

RECENT_WINDOW = 5      # 健康度观察窗口（最近 5 次检测）
STABILITY_WINDOW = 3   # 稳定性窗口（最近 3 次）
HISTORY_LIMIT = 20     # 每节点保留的明细条数上限


class NodeHistory:
    """state/node_history.json 的读写与健康度派生。"""

    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            log.warning("node_history unreadable, start empty: %s", self.path)
            return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 生命周期写入 ----
    def mark_checking(self, node_id):
        """本轮开始检测：CHECKING（保留原有健康度上下文）。"""
        rec = self.data.setdefault(node_id, {})
        rec["status"] = "CHECKING"

    def record(self, node_id, ok, latency=None, ip="", country="", reason=""):
        """记录一次检测结果并派生健康状态；绝不删除历史（P1-01）。"""
        rec = self.data.setdefault(node_id, {
            "status": "NEW", "attempts": 0, "success": 0,
            "last_success": "", "last_failure": "",
            "ip": "", "country": "", "history": [],
        })
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["success"] = rec.get("success", 0) + (1 if ok else 0)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if ok:
            rec["last_success"] = now
            rec["ip"] = ip or rec.get("ip", "")
            rec["country"] = country or rec.get("country", "")
        else:
            rec["last_failure"] = now
        rec["history"] = (rec.get("history", []) + [{
            "ok": bool(ok), "at": now, "latency": latency,
            "reason": reason, "ip": ip, "country": country,
        }])[-HISTORY_LIMIT:]
        rec["status"] = self.health(rec)
        return rec

    def mark_removed(self, node_id):
        """显式标记 REMOVED（由未来淘汰策略调用；历史保留可恢复）。"""
        rec = self.data.setdefault(node_id, {"status": "NEW"})
        rec["status"] = "REMOVED"
        rec["removed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # ---- 派生 ----
    @staticmethod
    def health(rec):
        """最近 5 次：成功≥3 GOOD；1-2 DEGRADED；0 BAD。"""
        recent = [h["ok"] for h in rec.get("history", [])][-RECENT_WINDOW:]
        if not recent:
            return "NEW"
        ok_n = sum(recent)
        if ok_n >= 3:
            return "GOOD"
        if ok_n >= 1:
            return "DEGRADED"
        return "BAD"

    def success_rate(self, rec):
        recent = [h["ok"] for h in rec.get("history", [])][-RECENT_WINDOW:]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    def stability_rate(self, rec):
        recent = [h["ok"] for h in rec.get("history", [])][-STABILITY_WINDOW:]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    @staticmethod
    def _speed_factor(rec):
        lats = [h.get("latency") for h in rec.get("history", [])
                if h.get("latency") is not None]
        if not lats:
            return 0.5  # 无速度数据：中性
        avg = sum(lats) / len(lats)
        # 线性归一化：<=0.5s → 1.0，>=10s → 0.0
        return max(0.0, min(1.0, 1.0 - (avg - 0.5) / 9.5))

    def score(self, node_id):
        """P1-03：score = 成功率×50 + 稳定性×30 + 速度×20（仅排序用）。"""
        rec = self.data.get(node_id)
        if not rec:
            return 0.0
        return (self.success_rate(rec) * 50.0
                + self.stability_rate(rec) * 30.0
                + self._speed_factor(rec) * 20.0)
