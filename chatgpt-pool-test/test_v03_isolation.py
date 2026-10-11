"""V0.3 Phase 8 异常隔离测试。

覆盖：环境异常发布守卫（§16）、node_group（§25）、快照/回滚（§30）、
过期清理（§13）、异常不删除节点。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import isolation                                          # noqa: E402
import pipeline                                          # noqa: E402
from lifecycle import FAILED_EXPIRY_SECONDS               # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from models import Node, LifecycleState, FailureReason, SubReason  # noqa: E402
from probe import ProbeError, ProbeResult                    # noqa: E402
from helpers import LocalMockSite, make_cfg, strict_success_routes  # noqa: E402
from test_diagnostic import _inject                       # noqa: E402


class TestNodeGroup(unittest.TestCase):
    def test_ip_segment(self):
        g = isolation.node_group({"server": "1.2.3.4", "source": "s1"})
        self.assertEqual(g["ip_segment"], "1.2.3.0/24")
        self.assertEqual(g["source"], "s1")

    def test_non_ip_server(self):
        g = isolation.node_group({"server": "proxy.example.com",
                                  "source": "s1"})
        self.assertEqual(g["ip_segment"], "")

    def test_group_stats_counts(self):
        cfg = make_cfg("v03_iso1")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": LifecycleState.ACTIVE.value,
                         "node_group": {"ip_segment": "1.2.3.0/24",
                                        "source": "s1"}})
        lc.upsert("n2", {"life_state": LifecycleState.FAILED.value,
                         "node_group": {"ip_segment": "1.2.3.0/24",
                                        "source": "s1"}})
        lc.save()
        st = isolation.group_stats(cfg)
        self.assertEqual(len(st["groups"]), 1)
        g = st["groups"][0]
        self.assertEqual(g["count"], 2)
        self.assertEqual(g["failed"], 1)
        self.assertEqual(g["fail_ratio"], 0.5)


class TestSnapshotRollback(unittest.TestCase):
    def test_snapshot_and_rollback(self):
        cfg = make_cfg("v03_iso2")
        os.makedirs(cfg.sub("run/final"), exist_ok=True)
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "r1", "nodes": [{"node_id": "a"}]}, f)
        snap = isolation.snapshot_pool(cfg, round_id="r1")
        self.assertTrue(os.path.isfile(snap))
        # 覆盖 current.json 模拟损坏
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "bad", "nodes": []}, f)
        chosen = isolation.rollback_pool(cfg)
        self.assertIsNotNone(chosen)
        with open(cfg.sub("run/final/current.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["nodes"][0]["node_id"], "a")

    def test_rollback_requires_nonempty(self):
        cfg = make_cfg("v03_iso3")
        self.assertIsNone(isolation.rollback_pool(cfg))  # 无快照


class TestCleanupExpired(unittest.TestCase):
    def test_expired_failed_removed(self):
        cfg = make_cfg("v03_iso4")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        now = 10_000
        lc.upsert("old", {"life_state": LifecycleState.FAILED.value,
                          "last_failure": now - FAILED_EXPIRY_SECONDS - 1})
        lc.upsert("fresh", {"life_state": LifecycleState.FAILED.value,
                            "last_failure": now - 100})
        lc.upsert("active", {"life_state": LifecycleState.ACTIVE.value})
        lc.save()
        removed = isolation.cleanup_expired(cfg, now=now)
        self.assertEqual(removed, ["old"])
        lc2 = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        self.assertIsNone(lc2.get("old"))
        self.assertIsNotNone(lc2.get("fresh"))
        self.assertIsNotNone(lc2.get("active"))


class TestAnomalyGuard(unittest.TestCase):
    def test_guard_blocks_publish(self):
        cfg = make_cfg("v03_iso5")
        summary = {"environment_anomaly": {"detected": True, "stage": "TLS",
                                           "failed": 20, "ratio": 1.0},
                   "published": False, "reason": None}
        blocked = isolation.apply_anomaly_guard(cfg, summary)
        self.assertTrue(blocked)
        self.assertEqual(summary["reason"], "ENVIRONMENT_ANOMALY")

    def test_guard_passes_clean_round(self):
        cfg = make_cfg("v03_iso6")
        summary = {"environment_anomaly": None, "reason": None}
        blocked = isolation.apply_anomaly_guard(cfg, summary)
        self.assertFalse(blocked)
        self.assertIsNone(summary["reason"])

    def test_pipeline_anomaly_round_preserves_old_pool(self):
        """§16：环境异常轮 → published=False + ENVIRONMENT_ANOMALY；
        节点不被批量删除。"""
        cfg = make_cfg("v03_iso7")
        # 12 个节点全部 TLS 失败 → 同阶段 ≥80% 且失败 ≥10
        raw_nodes = "\n".join(
            "ss://base64:%d@1.2.3.%d:8388#n%d" % (1000 + i, i % 200 + 1, i)
            for i in range(12))

        def all_fail_probe(site):
            # probe_fn 接收 URL：GeoIP 阶段应成功，ChatGPT 端点统一模拟 TLS 超时。
            # 这样测试的是环境异常守卫，而不是探针签名不匹配。
            def fn(url):
                if "ipwho.is" in url or "ip-api.com" in url:
                    return ProbeResult(
                        200, url, "application/json",
                        '{"ip":"203.0.113.9","country_code":"US","success":true}')
                raise ProbeError(
                    FailureReason.TLS_FAIL,
                    sub=SubReason.TLS_TIMEOUT,
                    detail={"type": "socket.timeout", "elapsed_ms": 3500,
                            "url": url})
            return fn

        with LocalMockSite(strict_success_routes()) as site:
            inject = _inject(site, probe_fn=all_fail_probe(site))
            inject["fetchers"] = lambda: [("src", raw_nodes)]
            s = pipeline.run_refresh(cfg, inject=inject)
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "ENVIRONMENT_ANOMALY")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        self.assertGreaterEqual(len(lc.all()), 12)  # 节点保留，未批量删除


if __name__ == "__main__":
    unittest.main()
