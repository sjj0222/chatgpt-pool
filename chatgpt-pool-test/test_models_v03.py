"""V0.3 Phase 1 数据模型扩展测试。

覆盖：
- 生命周期枚举完整性（§4/§5：DISCOVERED→…→ACTIVE/DEGRADED/COOLDOWN/RECOVERING/
  FAILED/BLACKLIST）
- 源生命周期枚举（§17 ACTIVE/DEGRADED/DISABLED）
- 能力标签（§9）与事件类型（§27）
- 评分结构（§7 权重 40/30/20/10；§28 breakdown 可展示；§8 service_score 多维）
- 指纹（§3：稳定/区分/迁移识别；§35 不保存明文）
- Node 新字段向后兼容（旧 dict 缺 node_fingerprint 可读）
- schema_version（§26：新数据带版本；旧数据无版本兼容；只增字段）
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

from models import (                                     # noqa: E402
    SCHEMA_VERSION, Capability, LifecycleState,
    Node, NodeEvent, NodeEventType, NodeQualityScore,
    SCORE_WEIGHTS, ServiceScore, SourceState)
import fingerprint                                      # noqa: E402
from lifecycle_store import (                           # noqa: E402
    NodeLifecycleStore, SourceLifecycleStore, default_node_lifecycle)
from helpers import make_cfg                             # noqa: E402

EXPECTED_LIFECYCLE = [
    "DISCOVERED", "VALIDATED", "PROBING", "PROBATION", "STANDBY", "ACTIVE",
    "DEGRADED", "COOLDOWN", "RECOVERING", "FAILED", "BLACKLIST",
]


class TestV03Models(unittest.TestCase):
    """枚举与评分结构。"""

    def test_lifecycle_states_complete(self):
        self.assertEqual(
            sorted(LifecycleState.__members__),
            sorted(EXPECTED_LIFECYCLE))
        # 状态机关键迁移（V0.3 §4）
        self.assertEqual(
            LifecycleState.PROBATION.value, "PROBATION")

    def test_source_states(self):
        self.assertEqual(SourceState.ACTIVE.value, "ACTIVE")
        self.assertEqual(SourceState.DEGRADED.value, "DEGRADED")
        self.assertEqual(SourceState.DISABLED.value, "DISABLED")

    def test_capabilities(self):
        caps = {c.value for c in Capability}
        self.assertEqual(caps, {"chatgpt", "github", "claude", "gemini", "web"})

    def test_event_types(self):
        for ev in ("DISCOVERED", "PROMOTED", "DEGRADED", "RECOVERED",
                   "COOLDOWN", "EXPIRED", "DELETED"):
            self.assertIn(ev, NodeEventType.__members__)

    def test_score_weights(self):
        self.assertEqual(SCORE_WEIGHTS,
                         {"stability": 0.40, "availability": 0.30,
                          "latency": 0.20, "credit": 0.10})

    def test_quality_score_compute(self):
        s = NodeQualityScore(stability=90, availability=80, latency=70,
                             credit=60)
        self.assertEqual(s.compute_overall(),
                         round(90 * .40 + 80 * .30 + 70 * .20 + 60 * .10, 1))
        self.assertEqual(s.overall, 80.0)
        # breakdown 必须可单独展示（§28）
        d = s.to_dict()
        for k in ("overall", "stability", "availability", "latency",
                  "credit"):
            self.assertIn(k, d)

    def test_service_score_multidimensional(self):
        """§8：禁止只有总分；服务维度独立。"""
        ss = ServiceScore(overall=85, services={"chatgpt": 95, "github": 90})
        self.assertEqual(ss.services["chatgpt"], 95)
        d = ss.to_dict()
        self.assertEqual(d["services"], {"chatgpt": 95, "github": 90})

    def test_node_event_serialization(self):
        ev = NodeEvent(ts="2026-10-07T00:00:00", event="PROMOTED",
                       node_id="abc", detail={"to": "ACTIVE"})
        d = ev.to_dict()
        self.assertEqual(d["event"], "PROMOTED")
        self.assertEqual(NodeEvent.from_dict(d).detail, {"to": "ACTIVE"})


class TestFingerprint(unittest.TestCase):
    """§3：稳定、区分、迁移识别；§35 不保存明文。"""

    def test_stable_same_node(self):
        f1 = fingerprint.generate_fingerprint(
            "ss", "1.2.3.4", 8388, password="p")
        f2 = fingerprint.generate_fingerprint(
            "ss", "1.2.3.4", 8388, password="p")
        self.assertEqual(f1, f2)

    def test_differs_on_credentials(self):
        f1 = fingerprint.generate_fingerprint(
            "ss", "1.2.3.4", 8388, password="p1")
        f2 = fingerprint.generate_fingerprint(
            "ss", "1.2.3.4", 8388, password="p2")
        self.assertNotEqual(f1, f2)

    def test_differs_on_server_port(self):
        a = fingerprint.generate_fingerprint("trojan", "a.com", 443)
        b = fingerprint.generate_fingerprint("trojan", "b.com", 443)
        c = fingerprint.generate_fingerprint("trojan", "a.com", 8443)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_migration_ignores_name_and_source(self):
        """仅名称/来源变化 → fingerprint 不变（节点迁移识别）。"""
        from models import Node
        n1 = Node(node_id="id1", protocol="vmess", server="x.com", port=443,
                  uuid="u", name="US-1", source="srcA")
        n2 = Node(node_id="id2", protocol="vmess", server="x.com", port=443,
                  uuid="u", name="US-2", source="srcB")
        self.assertEqual(fingerprint.fingerprint_for_node(n1),
                         fingerprint.fingerprint_for_node(n2))
        self.assertNotEqual(n1.node_id, n2.node_id)

    def test_params_normalized(self):
        """TLS/网络参数参与指纹且顺序无关。"""
        f1 = fingerprint.generate_fingerprint(
            "trojan", "x.com", 443, params={"tls": "1", "sni": "a.com",
                                            "alpn": "h2"})
        f2 = fingerprint.generate_fingerprint(
            "trojan", "x.com", 443, params={"alpn": "h2", "sni": "a.com",
                                            "tls": "1"})
        self.assertEqual(f1, f2)
        # 空参数不产生差异
        f3 = fingerprint.generate_fingerprint("trojan", "x.com", 443)
        f4 = fingerprint.generate_fingerprint(
            "trojan", "x.com", 443, params={"": "", "none": None})
        self.assertEqual(f3, f4)

    def test_no_plaintext_in_fingerprint(self):
        """§35：指纹是哈希，不包含明文凭据。"""
        fp = fingerprint.generate_fingerprint(
            "ss", "1.2.3.4", 8388, password="secret123")
        self.assertNotIn("secret123", fp)
        self.assertEqual(len(fp), 64)


class TestNodeBackwardCompat(unittest.TestCase):
    """Node 新字段向后兼容（V0.3 §26：只增字段，禁止破坏历史数据）。"""

    def test_new_field_default(self):
        n = Node()
        self.assertEqual(n.node_fingerprint, "")
        d = n.to_dict()
        self.assertIn("node_fingerprint", d)

    def test_from_dict_old_data(self):
        """旧数据（无 node_fingerprint 键）仍可读。"""
        old = {"node_id": "abc", "protocol": "ss", "stage": "FINAL"}
        n = Node.from_dict(old)
        self.assertEqual(n.node_id, "abc")
        self.assertEqual(n.node_fingerprint, "")   # 新字段默认值


class TestNodeLifecycleStore(unittest.TestCase):
    """node_lifecycle 存储：schema 版本、兼容、只增字段、事件。"""

    def setUp(self):
        self.cfg = make_cfg("v03_lc")
        self.path = self.cfg.sub("state/node_lifecycle.json")

    def test_schema_version_written(self):
        store = NodeLifecycleStore(self.path)
        store.upsert("n1", {"life_state": "ACTIVE", "fingerprint": "fp1"})
        store.save()
        raw = json.load(open(self.path))
        self.assertEqual(raw["schema_version"], SCHEMA_VERSION)
        self.assertEqual(raw["nodes"]["n1"]["life_state"], "ACTIVE")

    def test_read_old_data_without_schema_version(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"nodes": {"n1": {"life_state": "STANDBY"}}}, f)
        store = NodeLifecycleStore(self.path)
        self.assertEqual(store.get("n1")["life_state"], "STANDBY")
        # 兼容读后保存 → 补写 schema_version
        store.upsert("n1", {"fail_count": 1})
        store.save()
        self.assertEqual(json.load(open(self.path))["schema_version"],
                         SCHEMA_VERSION)

    def test_upsert_keeps_unknown_fields(self):
        """§26：只增字段，禁止破坏历史数据。"""
        store = NodeLifecycleStore(self.path)
        store.upsert("n1", {"life_state": "ACTIVE", "future_field": "x"})
        store.save()
        store2 = NodeLifecycleStore(self.path)
        store2.upsert("n1", {"fail_count": 2})
        store2.save()
        rec = json.load(open(self.path))["nodes"]["n1"]
        self.assertEqual(rec["future_field"], "x")       # 未知字段保留
        self.assertEqual(rec["fail_count"], 2)

    def test_events_appended_with_limit(self):
        store = NodeLifecycleStore(self.path)
        for i in range(60):
            store.add_event("n1", "PROMOTED", {"i": i})
        store.save()
        store2 = NodeLifecycleStore(self.path)
        self.assertEqual(len(store2.get("n1")["events"]), 50)  # 保留最近 50

    def test_default_lifecycle(self):
        rec = default_node_lifecycle(fingerprint="fp", source="src",
                                     protocol="ss")
        self.assertEqual(rec["life_state"], "DISCOVERED")
        self.assertEqual(rec["success_count"], 0)
        self.assertIn("events", rec)


class TestSourceLifecycleStore(unittest.TestCase):
    """source_lifecycle 存储。"""

    def setUp(self):
        self.cfg = make_cfg("v03_src")
        self.path = self.cfg.sub("state/source_lifecycle.json")

    def test_schema_version_and_upsert(self):
        store = SourceLifecycleStore(self.path)
        store.upsert("https://src-a/list.txt",
                     {"state": "ACTIVE", "source_score": 82.0,
                      "alive_rate": 0.15, "policy": "allow"})
        store.save()
        raw = json.load(open(self.path))
        self.assertEqual(raw["schema_version"], SCHEMA_VERSION)
        rec = raw["sources"]["https://src-a/list.txt"]
        self.assertEqual(rec["state"], "ACTIVE")
        self.assertEqual(rec["source_score"], 82.0)
        self.assertEqual(rec["policy"], "allow")

    def test_old_format_top_level_map(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"https://src-a/list.txt": {"state": "DEGRADED"}}, f)
        store = SourceLifecycleStore(self.path)
        self.assertEqual(store.get("https://src-a/list.txt")["state"],
                         "DEGRADED")


if __name__ == "__main__":
    unittest.main()
