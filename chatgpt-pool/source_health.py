"""数据源健康统计（V0.2.2 P0-05）。

run/source_health.json：
  {
    "source_url": {
      "fetch": true, "parsed": 300, "valid": 280,
      "alive": 5, "alive_rate": 0.017, "last_refresh": "..."
    }
  }

口径：
- fetch：该来源本轮是否抓取成功（出现在 raw_items 中）。
- parsed：该来源解析出的节点数。
- valid：去重后进入检测流程的节点数。
- alive：该来源进入最终池（FINAL）的节点数。
- alive_rate = alive / parsed（示例口径 5/300 ≈ 0.017；parsed=0 时为 0.0）。

区分：源不可用（fetch=false）≠ 节点不可用（parsed>0 但 alive=0）≠
检测失败（顶层失败原因已按节点记录，本文件只统计源维度）。
"""
import json
import logging
import os
import time

log = logging.getLogger("pool.source_health")


class SourceHealth:
    """run/source_health.json 的读写与每轮更新。"""

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
            log.warning("source_health unreadable, start empty: %s", self.path)
            return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def update_round(self, configured_sources, raw_sources, parsed_by_source,
                     valid_by_source, alive_by_source, round_id):
        """更新一轮的源健康数据。

        configured_sources: 配置的全部来源名（用于标 fetch=false 的源）。
        raw_sources: 本轮抓取成功的来源名集合。
        parsed_by_source / valid_by_source / alive_by_source: 按来源计数的 dict。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for src in configured_sources:
            rec = self.data.setdefault(src, {})
            rec["fetch"] = bool(src in raw_sources)
            rec["parsed"] = int(parsed_by_source.get(src, 0))
            rec["valid"] = int(valid_by_source.get(src, 0))
            rec["alive"] = int(alive_by_source.get(src, 0))
            parsed = rec["parsed"]
            rec["alive_rate"] = round(rec["alive"] / parsed, 3) if parsed else 0.0
            rec["last_refresh"] = now
            rec["last_round_id"] = round_id
        self.save()
        return self.data
