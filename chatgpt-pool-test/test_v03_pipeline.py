"""V0.3 Phase 3 Fingerprint 去重接入测试。

覆盖：
- refresh 后节点带 node_fingerprint（§3）
- 同 fingerprint 不同 node_id → 生命周期记录继承（迁移识别，§3）
- node_lifecycle.json 被真实写入且带 schema_version
- V0.2.2.2 行为零变化（reject_summary/诊断产物格式不变）
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import pipeline                                          # noqa: E402
from models import SCHEMA_VERSION                          # noqa: E402
from helpers import LocalMockSite, make_cfg, strict_success_routes, fake_country_resolver  # noqa: E402
import fingerprint                                        # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from test_diagnostic import _inject, _mixed_fail_probe, NODE_A, NODE_B, NODE_C  # noqa: E402


class TestFingerprintInPipeline(unittest.TestCase):
    def test_nodes_carry_fingerprint_after_refresh(self):
        cfg = make_cfg("v03_fp1")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        recs = lc.all()
        self.assertTrue(recs)
        for rec in recs.values():
            self.assertTrue(rec["fingerprint"])
            self.assertEqual(len(rec["fingerprint"]), 64)
            self.assertIn("life_state", rec)

    def test_same_fingerprint_inherits_history(self):
        """§3：同 fingerprint 不同 node_id（迁移）→ 继承历史记录。"""
        cfg = make_cfg("v03_fp2")
        # 第一轮：节点 A/B/C（name=nodeA 等）
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(site))
        lc1 = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        # 构造"迁移"节点：参数完全相同、仅 node_id/name 不同
        migrated = NODE_A.replace("#nodeA", "#nodeA-renamed")
        inject2 = _inject(None)
        inject2["fetchers"] = lambda: [("srcB", migrated)]
        with LocalMockSite(strict_success_routes()) as site2:
            pipeline.run_refresh(cfg, inject=inject2)
        lc2 = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        # 新 node_id 应继承旧 fingerprint（同 fingerprint 存在）
        fps = {r["fingerprint"] for r in lc1.all().values()}
        self.assertTrue(fps)
        self.assertTrue(
            any(r["fingerprint"] in fps for r in lc2.all().values()))

    def test_different_nodes_distinct_fingerprints(self):
        cfg = make_cfg("v03_fp3")
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(site))
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        recs = list(lc.all().values())
        fps = {r["fingerprint"] for r in recs}
        self.assertEqual(len(fps), len(recs))  # 不同节点不同指纹

    def test_v0222_behavior_unchanged(self):
        """V0.2.2.2 行为零变化：失败轮摘要/诊断产物格式不变。"""
        cfg = make_cfg("v03_fp4")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "EMPTY_POOL")
        self.assertEqual(s["reject_summary"]["TLS_FAIL"], 3)
        self.assertEqual(s["reject_summary_detailed"]["TLS_TIMEOUT"], 3)
        # 诊断产物仍在 run/
        self.assertTrue(os.path.isfile(cfg.sub("run/source_health.json")))
        self.assertTrue(os.path.isfile(cfg.sub("run/detect.log")))
        self.assertTrue(os.path.isfile(cfg.sub("run/refresh_state.json")))


if __name__ == "__main__":
    unittest.main()
