"""V0.3 Phase 6 Source 管理测试。

覆盖：源评分（§18）、生命周期降级/恢复（§17/§20）、源隔离（§24）、
黑白名单过滤（§21）、目标源数量（§19）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

from lifecycle_store import SourceLifecycleStore       # noqa: E402
from models import SourceState                          # noqa: E402
import source_mgmt                                     # noqa: E402
from helpers import make_cfg                           # noqa: E402


def _health(fetch=True, parsed=300, valid=280, alive=5, alive_rate=0.017):
    return {"fetch": fetch, "parsed": parsed, "valid": valid,
            "alive": alive, "alive_rate": alive_rate}


class TestSourceScore(unittest.TestCase):
    """§18：有效节点数(40) + alive_rate(40) + 更新频率(10) + 历史质量(10)。"""

    def test_full_score_without_history(self):
        rec = {"valid_nodes": 100, "alive_rate": 0.5, "fetch": True,
               "parsed": 300, "score_history": []}
        # 40(valid) + 40(alive_rate) + 10(frequency) = 90；历史 0
        self.assertEqual(source_mgmt.compute_source_score(rec), 90.0)

    def test_max_score_with_history(self):
        rec = {"valid_nodes": 100, "alive_rate": 0.5, "fetch": True,
               "parsed": 300, "score_history": [{"source_score": 100}]}
        self.assertEqual(source_mgmt.compute_source_score(rec), 100.0)

    def test_zero_source(self):
        rec = {"valid_nodes": 0, "alive_rate": 0.0, "fetch": False,
               "parsed": 0, "score_history": []}
        self.assertEqual(source_mgmt.compute_source_score(rec), 0.0)

    def test_partial(self):
        rec = {"valid_nodes": 25, "alive_rate": 0.05, "fetch": True,
               "parsed": 100, "score_history": []}
        # 25/50*40=20 + 0.05/0.2*40=10 + 10 = 40
        self.assertEqual(source_mgmt.compute_source_score(rec), 40.0)

    def test_history_quality(self):
        rec = {"valid_nodes": 0, "alive_rate": 0.0, "fetch": False,
               "parsed": 0,
               "score_history": [{"source_score": 100}, {"source_score": 50}]}
        self.assertEqual(source_mgmt.compute_source_score(rec), 7.5)


class TestSourceLifecycle(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg("v03_src_mgmt")

    def test_healthy_source_stays_active(self):
        source_mgmt.update_source_lifecycle(
            self.cfg, {"https://a/list.txt": _health(alive_rate=0.2)})
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        rec = sc.get("https://a/list.txt")
        self.assertEqual(rec["state"], SourceState.ACTIVE.value)
        self.assertFalse(rec["quarantine"])

    def test_consecutive_low_degrades_after_7(self):
        """§20：连续 7 次 alive_rate<5% → DEGRADED。"""
        for _ in range(source_mgmt.CONSECUTIVE_LOW_LIMIT):
            source_mgmt.update_source_lifecycle(
                self.cfg, {"https://bad/list.txt":
                           _health(valid=10, alive=0, alive_rate=0.0)})
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        self.assertEqual(sc.get("https://bad/list.txt")["state"],
                         SourceState.DEGRADED.value)

    def test_recover_after_good_round(self):
        """§17：恢复 → ACTIVE。"""
        for _ in range(source_mgmt.CONSECUTIVE_LOW_LIMIT):
            source_mgmt.update_source_lifecycle(
                self.cfg, {"https://r/list.txt":
                           _health(valid=10, alive=0, alive_rate=0.0)})
        source_mgmt.update_source_lifecycle(
            self.cfg, {"https://r/list.txt": _health(alive_rate=0.2)})
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        self.assertEqual(sc.get("https://r/list.txt")["state"],
                         SourceState.ACTIVE.value)
        self.assertEqual(sc.get("https://r/list.txt")["consecutive_low"], 0)

    def test_quarantine_when_all_nodes_failed(self):
        """§24：源本轮全部节点失败（alive=0 且 valid>0）→ 隔离该源。"""
        source_mgmt.update_source_lifecycle(
            self.cfg, {"https://q/list.txt": _health(valid=50, alive=0,
                                                     alive_rate=0.0)})
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        rec = sc.get("https://q/list.txt")
        self.assertTrue(rec["quarantine"])
        self.assertEqual(rec["state"], SourceState.DEGRADED.value)

    def test_score_history_recorded(self):
        source_mgmt.update_source_lifecycle(
            self.cfg, {"https://h/list.txt": _health(alive_rate=0.2)})
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        rec = sc.get("https://h/list.txt")
        self.assertEqual(len(rec["score_history"]), 1)
        self.assertIn("source_score", rec["score_history"][0])


class TestPolicy(unittest.TestCase):
    """§21 黑白名单。"""

    def setUp(self):
        self.cfg = make_cfg("v03_src_policy")

    def test_deny_blocks_source(self):
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        sc.upsert("https://blocked/list.txt",
                  {"policy": "deny", "state": SourceState.ACTIVE.value})
        sc.save()
        self.assertFalse(source_mgmt.is_allowed(
            self.cfg, "https://blocked/list.txt"))

    def test_disabled_blocks_source(self):
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        sc.upsert("https://off/list.txt", {"state": SourceState.DISABLED.value})
        sc.save()
        self.assertFalse(source_mgmt.is_allowed(self.cfg, "https://off/list.txt"))

    def test_unknown_allowed(self):
        self.assertTrue(source_mgmt.is_allowed(self.cfg, "https://new/list.txt"))

    def test_filter_allowed_keeps_only_allowed(self):
        sc = SourceLifecycleStore(self.cfg.sub("state/source_lifecycle.json"))
        sc.upsert("https://blocked/list.txt",
                  {"policy": "deny", "state": SourceState.ACTIVE.value})
        sc.save()
        fetchers = [("https://blocked/list.txt", "x"),
                    ("https://good/list.txt", "y")]
        out = source_mgmt.filter_allowed(self.cfg, fetchers)
        self.assertEqual([n for n, _ in out], ["https://good/list.txt"])


class TestSourceCount(unittest.TestCase):
    def test_count_state(self):
        cfg = make_cfg("v03_src_count")
        sc = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
        sc.upsert("s1", {"state": SourceState.ACTIVE.value})
        sc.upsert("s2", {"state": SourceState.DEGRADED.value})
        sc.upsert("s3", {"state": SourceState.DISABLED.value})
        sc.save()
        st = source_mgmt.source_count_state(cfg)
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["active"], 1)
        self.assertEqual(st["degraded"], 1)
        self.assertEqual(st["disabled"], 1)
        self.assertEqual(st["target_range"],
                         [source_mgmt.TARGET_SOURCE_MIN,
                          source_mgmt.TARGET_SOURCE_MAX])


if __name__ == "__main__":
    unittest.main()
