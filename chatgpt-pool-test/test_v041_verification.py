"""V0.4.1 Production Verification 测试。

只验证/统计/报告，不改核心：
- Phase 2：runtime_report 扩展字段（start_time/average_score/source_count）
  + 历史归档不覆盖
- Phase 3：service_quality.json（成功率/avg_latency/fail_reason 累积）
- Phase 4：生命周期端到端（ACTIVE→DEGRADED→COOLDOWN→恢复 ACTIVE；
  cooldown 时间正确；恢复不丢信用；BLACKLIST 不可恢复）
- Phase 5：source_quality_report.json（LOW_QUALITY/EXPIRED 只读标记）
- Phase 8：故障模拟（mihomo 异常不中断 / 源失效降级 / current.json 删除恢复 /
  重启恢复）
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import lifecycle                                              # noqa: E402
import runtime_monitor                                        # noqa: E402
import service_quality                                        # noqa: E402
import source_quality_report                                  # noqa: E402
import scheduler                                              # noqa: E402
from lifecycle_store import NodeLifecycleStore, \
    LifecycleState, default_node_lifecycle                    # noqa: E402
from helpers import make_cfg                                 # noqa: E402


class TestRuntimeReportV041(unittest.TestCase):
    """Phase 2：扩展字段 + 历史归档不覆盖。"""

    def test_extended_fields(self):
        cfg = make_cfg("v041_rt")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": "ACTIVE", "success_count": 8,
                         "fail_count": 2, "score": {"overall": 80.0}})
        lc.save()
        os.makedirs(cfg.sub("run"), exist_ok=True)
        with open(cfg.sub("run/refresh_state.json"), "w") as f:
            json.dump({"last_refresh": "2026-10-07T00:00:00",
                       "first_start": "2026-10-06T08:00:00"}, f)
        r = runtime_monitor.build_runtime_report(cfg)
        for k in ("uptime_s", "start_time", "pool_size", "active_nodes",
                  "success_rate", "average_score", "source_count",
                  "error_count"):
            self.assertIn(k, r)
        self.assertEqual(r["start_time"], "2026-10-06T08:00:00")
        self.assertEqual(r["average_score"], 80.0)

    def test_history_archived(self):
        cfg = make_cfg("v041_rt2")
        p1 = runtime_monitor.write_runtime_report(cfg)
        p2 = runtime_monitor.write_runtime_report(cfg)
        self.assertEqual(p1, p2)
        hist_dir = cfg.sub("run/history/runtime")
        files = os.listdir(hist_dir)
        self.assertEqual(len(files), 2)          # 不覆盖历史


class TestServiceQuality(unittest.TestCase):
    """Phase 3：服务质量累积统计。"""

    def test_record_and_report(self):
        cfg = make_cfg("v041_sq")
        service_quality.record(cfg, "chatgpt", True, latency_ms=120)
        service_quality.record(cfg, "chatgpt", True, latency_ms=200)
        service_quality.record(cfg, "chatgpt", False, fail_reason="TLS_FAIL")
        service_quality.record(cfg, "github", True, latency_ms=50)
        rep = service_quality.get_report(cfg)
        c = rep["services"]["chatgpt"]
        self.assertEqual(c["success_rate"], 0.6667)
        self.assertEqual(c["avg_latency"], 160.0)
        self.assertIn("TLS_FAIL", c["fail_reason"])
        self.assertEqual(rep["services"]["github"]["samples"], 1)

    def test_report_file_exists(self):
        cfg = make_cfg("v041_sq2")
        service_quality.record(cfg, "claude", False, fail_reason="TIMEOUT")
        self.assertTrue(os.path.isfile(
            cfg.sub("run/service_quality.json")))


class TestLifecycleEndToEnd(unittest.TestCase):
    """Phase 4：ACTIVE→DEGRADED→COOLDOWN→恢复 ACTIVE。"""

    def _fail(self, eng, rec, n=1):
        for _ in range(n):
            state, _ev, _d = eng.apply_result(
                rec, probe_success=False, score_ok=True,
                probation_pass=True)
            rec["life_state"] = state

    def _ok(self, eng, rec):
        state, _ev, _d = eng.apply_result(
            rec, probe_success=True, score_ok=True, probation_pass=True)
        rec["life_state"] = state

    def test_degrade_cooldown_recover(self):
        import time as _t
        eng = lifecycle.LifecycleEngine()
        rec = default_node_lifecycle(fingerprint="fp1", source="s1",
                                      protocol="ss")
        rec["life_state"] = LifecycleState.ACTIVE.value
        rec["fail_streak"] = 0
        rec["cooldown_count"] = 0
        rec["success_count"] = 5
        # 连续 3 次失败 → DEGRADED
        self._fail(eng, rec, n=3)
        self.assertEqual(rec["life_state"], LifecycleState.DEGRADED.value)
        # 再 3 次失败 → COOLDOWN
        self._fail(eng, rec, n=3)
        self.assertEqual(rec["life_state"], LifecycleState.COOLDOWN.value)
        # cooldown 时间正确（第一次 600s）
        self.assertEqual(lifecycle.cooldown_seconds(1), 600)
        self.assertGreater(rec["cooldown_until"], _t.time())
        # 信用不丢：恢复后 success_count 保留（评分/历史不重置）
        self._ok(eng, rec)
        self.assertIn(rec["life_state"],
                      (LifecycleState.ACTIVE.value,
                       LifecycleState.RECOVERING.value))
        self.assertEqual(rec["success_count"], 5)
        self.assertEqual(rec["cooldown_count"], 0)   # 冷却计数已清

    def test_blacklist_never_recovers(self):
        eng = lifecycle.LifecycleEngine()
        rec = default_node_lifecycle(fingerprint="fp2", source="s1",
                                     protocol="ss")
        rec["life_state"] = LifecycleState.BLACKLIST.value
        state, _ev, _d = eng.apply_result(
            rec, probe_success=True, score_ok=True, probation_pass=True)
        self.assertEqual(state, LifecycleState.BLACKLIST.value)


class TestSourceQualityReport(unittest.TestCase):
    """Phase 5：source_quality_report.json 只读聚合 + 标记。"""

    def test_report_fields(self):
        cfg = make_cfg("v041_sqr")
        os.makedirs(cfg.sub("run"), exist_ok=True)
        with open(cfg.sub("run/source_health.json"), "w") as f:
            json.dump({"https://good/list.txt": {"valid": 50, "alive": 10,
                                                 "alive_rate": 0.2},
                       "https://bad/list.txt": {"valid": 10, "alive": 0,
                                                "alive_rate": 0.0}}, f)
        os.makedirs(cfg.sub("state"), exist_ok=True)
        with open(cfg.sub("state/source_lifecycle.json"), "w") as f:
            json.dump({"sources": {
                "https://good/list.txt": {"state": "ACTIVE",
                                          "consecutive_low_count": 0},
                "https://bad/list.txt": {"state": "DEGRADED",
                                         "consecutive_low_count": 7}}}, f)
        rep = source_quality_report.build_report(cfg)
        rows = {r["source"]: r for r in rep["sources"]}
        self.assertEqual(rows["https://good/list.txt"]["status"], "ACTIVE")
        self.assertIsNone(rows["https://good/list.txt"]["flag"])
        # 连续 7 轮 alive_rate<5% → LOW_QUALITY
        self.assertEqual(rows["https://bad/list.txt"]["flag"],
                         "LOW_QUALITY")

    def test_write_report_file(self):
        cfg = make_cfg("v041_sqr2")
        p = source_quality_report.write_report(cfg)
        self.assertTrue(os.path.isfile(p))


class TestFaultSimulation(unittest.TestCase):
    """Phase 8：故障模拟。"""

    def test_mihomo_down_does_not_crash_probe(self):
        """mihomo 异常：probe 跳过不中断。"""
        from probe_services import run_service_probe
        cfg = make_cfg("v041_f1")
        # 端口不存在 → 全部跳过，不抛异常
        res = run_service_probe(cfg, proxy_port=59999, probe_fn=None)
        self.assertEqual(res, {})

    def test_source_degrades_when_alive_zero(self):
        """源失效：连续低质 → DEGRADED（只读验证 lifecycle 更新逻辑）。"""
        from source_mgmt import SourceLifecycleStore, update_source_lifecycle
        cfg = make_cfg("v041_f2")
        for _ in range(7):
            update_source_lifecycle(cfg, {"https://dead/list.txt": {
                "valid": 10, "alive": 0, "alive_rate": 0.0}})
        sc2 = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
        self.assertEqual(
            (sc2.get("https://dead/list.txt") or {}).get("state"),
            "DEGRADED")

    def test_current_json_missing_recover(self):
        """run/final/current.json 被删 → rollback 快照可恢复。"""
        import isolation
        cfg = make_cfg("v041_f3")
        os.makedirs(cfg.sub("run/final"), exist_ok=True)
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "r_good", "nodes": [{"node_id": "n1"}]},
                      f)
        isolation.snapshot_pool(cfg, round_id="r_good")
        os.remove(cfg.sub("run/final/current.json"))
        ok = isolation.rollback_pool(cfg)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(cfg.sub("run/final/current.json")))

    def test_restart_recover(self):
        """手机重启：陈旧锁清理 + tick 恢复。"""
        cfg = make_cfg("v041_f4")
        lock = scheduler.Lock(cfg, "refresh")
        lock.acquire()
        with open(lock.path, "w") as f:
            f.write("999999999")
        rec = scheduler.crash_recover(cfg)
        self.assertTrue(rec["recovered"])
        ran = scheduler.tick(cfg, {"refresh": (lambda: True)}, now=5_000_000)
        self.assertTrue(ran["refresh"])


if __name__ == "__main__":
    unittest.main()
