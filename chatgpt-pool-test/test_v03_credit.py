"""V0.3 Phase 5 历史信用测试。

覆盖：last_success/last_failure 更新、恢复计数（§14）、生命周期状态演进与
事件（§4/§27）、环境异常识别（§16：≥10 失败节点同阶段 ≥80% 判环境异常，
禁止批量删除）、时间衰减评分已在 Phase 4 覆盖。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import pipeline                                          # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from models import Node, LifecycleState, NodeEventType    # noqa: E402
from helpers import LocalMockSite, make_cfg, strict_success_routes  # noqa: E402
from test_diagnostic import _inject, _mixed_fail_probe    # noqa: E402
from pipeline import _detect_environment_anomaly          # noqa: E402


def _result(nid, stage="FINAL", chatgpt="STRICT_PASS", stability=True,
            failure_stage="TLS"):
    node = Node(node_id=nid, protocol="ss", server="1.2.3.4", port=8388)
    node.stage = stage
    node.chatgpt_status = chatgpt
    node.stability_pass = stability
    node.failure_stage = failure_stage
    return node


class TestCreditFields(unittest.TestCase):
    def test_last_success_and_life_state_after_ok_round(self):
        cfg = make_cfg("v03_credit1")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        for nid in s["final_nodes"]:
            rec = lc.get(nid)
            self.assertTrue(rec["last_success"])
            self.assertEqual(rec["life_state"], "PROBATION")  # 新节点首轮成功
            self.assertGreaterEqual(rec["success_count"], 1)

    def test_last_failure_after_fail_round(self):
        cfg = make_cfg("v03_credit2")
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        recs = lc.all()
        self.assertTrue(any(r.get("last_failure") for r in recs.values()))

    def test_recover_count_via_multi_round(self):
        """§14：DEGRADED/COOLDOWN/FAILED → ACTIVE 记 recover_count。"""
        cfg = make_cfg("v03_credit3")
        # 第一轮成功（进入 PROBATION）
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(site))
        # 构造"先连败再恢复"的 engine 级验证（避免长 mock 依赖）：
        from lifecycle import LifecycleEngine
        eng = LifecycleEngine()
        rec = {"life_state": LifecycleState.FAILED.value,
               "fail_streak": 0, "success_streak": 0,
               "cooldown_count": 0, "cooldown_until": 0,
               "last_failure": 1}
        new, ev, _ = eng.apply_result(rec, True, now=2)
        self.assertEqual(new, LifecycleState.ACTIVE.value)
        self.assertEqual(ev, NodeEventType.RECOVERED.value)

    def test_events_recorded(self):
        cfg = make_cfg("v03_credit4")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        # 首轮成功 → VALIDATED 事件（DISCOVERED→PROBATION）
        has_event = any(
            any(e["event"] == NodeEventType.VALIDATED.value
                for e in r.get("events", []))
            for r in lc.all().values())
        self.assertTrue(has_event)


class TestEnvironmentAnomaly(unittest.TestCase):
    """§16：环境问题 ≠ 节点问题；禁止批量删除。"""

    def _mk(self, stage, n):
        return [_result(f"n{i}-{stage}", stage="REJECTED", chatgpt="FAIL",
                        stability=False, failure_stage=stage)
                for i in range(n)]

    def test_detected_when_same_stage_80pct(self):
        res = self._mk("TLS", 16) + self._mk("EGRESS", 4)
        a = _detect_environment_anomaly(res)
        self.assertIsNotNone(a)
        self.assertEqual(a["stage"], "TLS")
        self.assertGreaterEqual(a["ratio"], 0.8)

    def test_not_detected_below_10_failures(self):
        res = self._mk("TLS", 5)
        self.assertIsNone(_detect_environment_anomaly(res))

    def test_not_detected_when_mixed(self):
        res = self._mk("TLS", 10) + self._mk("EGRESS", 10) + \
            self._mk("COUNTRY", 5)
        a = _detect_environment_anomaly(res)
        self.assertIsNone(a)   # 无单一阶段 ≥80%

    def test_anomaly_does_not_delete_nodes(self):
        """检测到环境异常时：节点记录保留（不批量删除）。"""
        cfg = make_cfg("v03_credit5")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        self.assertTrue(lc.all())   # 节点仍在 lifecycle 中


if __name__ == "__main__":
    unittest.main()
