"""V0.3 Phase 10 完整验收测试（§38/§40）。

故障模拟：全节点失败 / 源失效 / 数据损坏 / Termux 重启；
压力测试：10000 节点解析-去重-指纹（纯 CPU 路径）。
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import pipeline                                          # noqa: E402
import scheduler                                         # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from helpers import LocalMockSite, make_cfg, strict_success_routes  # noqa: E402
from test_diagnostic import _inject                       # noqa: E402
import isolation                                         # noqa: E402


class TestFullFailPreservesOldPool(unittest.TestCase):
    """场景 J（V5.2 §35J + §30）：本轮全失败 → 旧池保留。"""

    def test_old_pool_survives_empty_round(self):
        cfg = make_cfg("v03_acc1")
        with LocalMockSite(strict_success_routes()) as site:
            s1 = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s1["published"])
        pool_before = open(cfg.sub("run/final/current.json")).read()
        # 全失败轮
        with LocalMockSite(strict_success_routes()) as site:
            from test_diagnostic import _mixed_fail_probe
            s2 = pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        self.assertFalse(s2["published"])
        self.assertEqual(s2["reason"], "EMPTY_POOL")
        pool_after = open(cfg.sub("run/final/current.json")).read()
        self.assertEqual(pool_after, pool_before)   # 旧池未被清空


class TestSourceFailureDiagnostics(unittest.TestCase):
    """源全部失效 → INTERNAL_ERROR，五项诊断产物必在（V0.2.2.2 要求）。"""

    def test_source_failure_keeps_diagnostics(self):
        cfg = make_cfg("v03_acc2")
        with LocalMockSite(strict_success_routes()) as site:
            inject = _inject(site)

            def _down():
                raise OSError("network down")
            inject["fetchers"] = lambda: [("srcA", _down)]  # 全部源失效
            s = pipeline.run_refresh(cfg, inject=inject)
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "INTERNAL_ERROR: all node sources failed")
        for p in ("run/source_health.json", "run/detect.log",
                  "run/refresh_state.json"):
            self.assertTrue(os.path.isfile(cfg.sub(p)), p)
        # failed round 与 failed nodes 目录存在
        self.assertTrue(os.path.isdir(cfg.sub("run/failed/nodes")))


class TestCorruptedPoolRollback(unittest.TestCase):
    """§30：数据损坏 → 快照回滚。"""

    def test_rollback_after_corruption(self):
        cfg = make_cfg("v03_acc3")
        os.makedirs(cfg.sub("run/final"), exist_ok=True)
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "r1", "nodes": [{"node_id": "good"}]}, f)
        isolation.snapshot_pool(cfg, round_id="r1")
        # 模拟损坏：写入垃圾
        with open(cfg.sub("run/final/current.json"), "w") as f:
            f.write("{broken json")
        chosen = isolation.rollback_pool(cfg)
        self.assertIsNotNone(chosen)
        data = json.load(open(cfg.sub("run/final/current.json")))
        self.assertEqual(data["nodes"][0]["node_id"], "good")


class TestTermuxRestartRecover(unittest.TestCase):
    """§31：Termux 重启/进程被杀 → 启动恢复。"""

    def test_recover_after_restart(self):
        cfg = make_cfg("v03_acc4")
        # 模拟上次运行留下陈旧锁
        os.makedirs(cfg.sub("run/locks"), exist_ok=True)
        with open(cfg.sub("run/locks/refresh.lock"), "w") as f:
            f.write("99999999")
        rec = scheduler.crash_recover(cfg)
        self.assertTrue(rec["recovered"])
        self.assertTrue(rec["stale_locks"])          # 检测到并清理
        self.assertFalse(os.path.isfile(
            cfg.sub("run/locks/refresh.lock")))


class TestStress10000Nodes(unittest.TestCase):
    """§38 压力测试：10000 节点解析-去重-指纹（不崩溃、指纹唯一）。"""

    def _ss_line(self, i):
        import base64
        payload = base64.b64encode(
            ("user%d:pass%d@10.%d.%d.%d:8388"
             % (i, i, i // 2500, (i // 100) % 250, i % 250)).encode()
        ).decode()
        return "ss://%s#node%d\n" % (payload, i)

    def test_stress_parse_dedup_fingerprint(self):
        import dedup
        import fingerprint
        import parser as parser_mod
        n = 10000
        raw = "".join(self._ss_line(i) for i in range(n))
        items = [("stress", raw)]
        parsed, invalid = parser_mod.parse_all(items)
        self.assertGreaterEqual(len(parsed), n)      # 全部可解析
        nodes, dupes = dedup.dedup(parsed)
        self.assertEqual(dupes, 0)
        self.assertEqual(len(nodes), n)
        fps = {fingerprint.fingerprint_for_node(x) for x in nodes}
        self.assertEqual(len(fps), n)               # 指纹全部唯一

    def test_stress_lifecycle_sync(self):
        """5000 节点写入 lifecycle（容量与唯一性）。"""
        import dedup
        import fingerprint
        import parser as parser_mod
        from lifecycle_store import NodeLifecycleStore, \
            default_node_lifecycle
        n = 5000
        items = [("stress", "".join(self._ss_line(i) for i in range(n)))]
        parsed, _ = parser_mod.parse_all(items)
        nodes, _ = dedup.dedup(parsed)
        lc = NodeLifecycleStore(make_cfg("v03_acc5")
                                .sub("state/node_lifecycle.json"))
        for x in nodes:
            x.node_fingerprint = fingerprint.fingerprint_for_node(x)
            lc.upsert(x.node_id, default_node_lifecycle(
                fingerprint=x.node_fingerprint, source="stress",
                protocol=x.protocol))
        lc.save()
        self.assertEqual(len(lc.all()), n)


if __name__ == "__main__":
    unittest.main()
