"""V0.3 生命周期数据存储（Phase 1 数据模型）。

- NodeLifecycleStore：state/node_lifecycle.json，保存节点生命周期状态、质量评分、
  服务能力、历史信用字段、事件（V0.3 §4/§7/§8/§14/§27）。
- SourceLifecycleStore：state/source_lifecycle.json，保存源生命周期/评分/策略
  （V0.3 §17/§18/§21）。

数据版本管理（§26）：写入带 schema_version（SCHEMA_VERSION）；只增字段，
禁止破坏历史数据；读取旧文件（无 schema_version）兼容。
"""
import json
import logging
import os
import time

from models import SCHEMA_VERSION, LifecycleState, SourceState

log = logging.getLogger("pool.lifecycle")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        log.warning("lifecycle data unreadable, start empty: %s", path)
        return None


class NodeLifecycleStore:
    """state/node_lifecycle.json：node_id → 生命周期记录。

    记录结构（schema_version=3）：
      {
        "fingerprint": "...",
        "life_state": "DISCOVERED|...",
        "source": "...", "protocol": "...",
        "score": {"overall":0,"stability":0,...},       # NodeQualityScore
        "service_score": {"overall":0,"services":{}},   # ServiceScore
        "capability": ["chatgpt", ...],
        "success_count": 0, "fail_count": 0,
        "last_success": "", "last_failure": "",
        "score_history": [...], "events": [...],
        "updated_at": ""
      }
    """

    def __init__(self, path, schema_version=SCHEMA_VERSION):
        self.path = path
        self.schema_version = schema_version
        self.data = self._load()

    def _load(self):
        raw = _load_json(self.path)
        if raw is None:
            return {"schema_version": self.schema_version,
                    "updated_at": _now(), "nodes": {}}
        nodes = raw.get("nodes", raw)  # 旧版容错：顶层即映射时视作 nodes
        if not isinstance(nodes, dict):
            nodes = {}
        return {"schema_version": raw.get("schema_version",
                                          self.schema_version),
                "updated_at": raw.get("updated_at", _now()),
                "nodes": nodes}

    def save(self):
        self.data["schema_version"] = self.schema_version
        self.data["updated_at"] = _now()
        _atomic_write(self.path, self.data)

    def get(self, node_id):
        return self.data["nodes"].get(node_id)

    def all(self):
        return self.data["nodes"]

    def upsert(self, node_id, patch):
        """新增/更新记录；未知字段保留（只增字段，§26）。"""
        rec = self.data["nodes"].setdefault(node_id, {})
        rec.update(patch)
        rec["updated_at"] = _now()
        return rec

    def add_event(self, node_id, event, detail=None):
        """追加节点事件（§27，保留最近 50 条）。"""
        rec = self.data["nodes"].setdefault(node_id, {})
        events = rec.setdefault("events", [])
        events.append({"ts": _now(), "event": event,
                       "node_id": node_id, "detail": detail or {}})
        rec["events"] = events[-50:]
        rec["updated_at"] = _now()
        return rec

    def remove(self, node_id):
        self.data["nodes"].pop(node_id, None)


class SourceLifecycleStore:
    """state/source_lifecycle.json：source_url → 源生命周期记录。

    记录结构（schema_version=3）：
      {
        "state": "ACTIVE|DEGRADED|DISABLED",
        "source_score": 0.0,
        "alive_rate": 0.0,
        "valid_nodes": 0, "alive_nodes": 0,
        "consecutive_low": 0,           # 连续低质量刷新次数（§20）
        "policy": "allow|deny",         # 黑白名单（§21）
        "history": [...], "updated_at": ""
      }
    """

    def __init__(self, path, schema_version=SCHEMA_VERSION):
        self.path = path
        self.schema_version = schema_version
        self.data = self._load()

    def _load(self):
        raw = _load_json(self.path)
        if raw is None:
            return {"schema_version": self.schema_version,
                    "updated_at": _now(), "sources": {}}
        sources = raw.get("sources", raw)
        if not isinstance(sources, dict):
            sources = {}
        return {"schema_version": raw.get("schema_version",
                                          self.schema_version),
                "updated_at": raw.get("updated_at", _now()),
                "sources": sources}

    def save(self):
        self.data["schema_version"] = self.schema_version
        self.data["updated_at"] = _now()
        _atomic_write(self.path, self.data)

    def get(self, source):
        return self.data["sources"].get(source)

    def all(self):
        return self.data["sources"]

    def upsert(self, source, patch):
        rec = self.data["sources"].setdefault(source, {})
        rec.update(patch)
        rec["updated_at"] = _now()
        return rec

    def remove(self, source):
        self.data["sources"].pop(source, None)


def default_node_lifecycle(fingerprint="", source="", protocol=""):
    """新节点默认生命周期记录（DISCOVERED，V0.3 §4/§5）。"""
    return {
        "fingerprint": fingerprint,
        "life_state": LifecycleState.DISCOVERED.value,
        "source": source,
        "protocol": protocol,
        "success_count": 0,
        "fail_count": 0,
        "last_success": "",
        "last_failure": "",
        "score_history": [],
        "events": [],
    }
