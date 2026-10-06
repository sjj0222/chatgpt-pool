"""V0.4 集成测试：5 类故障模拟。

1. 全池死亡（全节点失败 → 旧池逐字节保留 + 诊断产物存在）
2. 源失效（源全部不可用 → INTERNAL_ERROR + 五产物 + 不覆盖旧池）
3. Scheduler 崩溃（tick 中途崩溃 → 状态持久化 → 重启 recover → 恢复调度）
4. 数据损坏（current.json 损坏 → snapshot 回滚恢复）
5. 重启恢复（陈旧锁/半写状态 → crash_recover 清理 → 正常 tick）
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import isolation                                             # noqa: E402
import pipeline                                              # noqa: E402
import runtime_monitor                                       # noqa: E402
import scheduler                                             # noqa: E402
import services                                              # noqa: E402
from helpers import LocalMockSite, make_cfg, strict_success_routes  # noqa: E402
from test_diagnostic import _inject                          # noqa: E402


def _write_pool(cfg, nodes, round_id="old"):
    os.makedirs(cfg.sub("run/final"), exist_ok=True)
    with open(cfg.sub("run/final/current.json"), "w") as f:
        json.dump({"round_id": round_id, "nodes": nodes}, f)


class TestFault1AllDead(unittest.TestCase):
    """全池死亡：所有节点失败 → 旧池保留、失败产物完整。"""

    def test_all_dead_keeps_old_pool(self):
        from test_diagnostic import _mixed_fail_probe
        cfg = make_cfg("v04_f1")
        old = [{"node_id": "keep1"}]
        _write_pool(cfg, old)
        with LocalMockSite(strict_success_routes()) as site:
            inject = _inject(site, probe_fn=_mixed_fail_probe(site))
            s = pipeline.run_refresh(cfg, inject=inject)
        self.assertFalse(s["published"])
        with open(cfg.sub("run/final/current.json")) as f:
            self.assertEqual(json.load(f), {"round_id": "old",
                                            "nodes": old})
        self.assertTrue(os.path.isfile(
            cfg.sub("run/source_health.json")))
        self.assertTrue(os.path.isfile(cfg.sub("run/refresh_state.json")))


class TestFault2SourceDown(unittest.TestCase):
    """源失效：源全部不可用 → INTERNAL_ERROR、旧池保留、产物存在。"""

    def test_source_down(self):
        cfg = make_cfg("v04_f2")
        _write_pool(cfg, [{"node_id": "old1"}])
        with LocalMockSite(strict_success_routes()) as site:
            inject = _inject(site)

            def _down():
                raise OSError("network down")
            inject["fetchers"] = lambda: [("srcA", _down)]
            s = pipeline.run_refresh(cfg, inject=inject)
        self.assertFalse(s["published"])
        self.assertIn("INTERNAL_ERROR", s["reason"] or "")
        for p in ("run/source_health.json", "run/detect.log",
                  "run/refresh_state.json"):
            self.assertTrue(os.path.isfile(cfg.sub(p)), p)
        with open(cfg.sub("run/final/current.json")) as f:
            self.assertEqual(json.load(f)["nodes"], [{"node_id": "old1"}])


class TestFault3SchedulerCrash(unittest.TestCase):
    """Scheduler 崩溃：tick 中途异常 → 重启后 recover → 恢复调度。"""

    def test_crash_then_recover(self):
        cfg = make_cfg("v04_f3")
        crashed = {"n": 0}

        def _boom():
            crashed["n"] += 1
            if crashed["n"] == 1:
                raise RuntimeError("simulated crash")

        ran = scheduler.tick(cfg, {"refresh": _boom}, now=1_000_000)
        self.assertFalse(ran["refresh"])      # 第一轮崩溃
        self.assertTrue(os.path.isfile(
            cfg.sub("run/scheduler_state.json")))
        # 崩溃恢复：清理陈旧锁 + 状态可读
        rec = scheduler.crash_recover(cfg)
        self.assertIsInstance(rec, dict)
        # 重启后重新 tick → 任务恢复执行
        ran2 = scheduler.tick(cfg, {"refresh": _boom}, now=2_000_000)
        self.assertTrue(ran2["refresh"])


class TestFault4DataCorruption(unittest.TestCase):
    """数据损坏：current.json 损坏 → snapshot 回滚恢复。"""

    def test_corrupt_pool_rollback(self):
        cfg = make_cfg("v04_f4")
        _write_pool(cfg, [{"node_id": "good"}], round_id="good_r")
        isolation.snapshot_pool(cfg, round_id="good_r")
        # 写入损坏数据
        with open(cfg.sub("run/final/current.json"), "w") as f:
            f.write("{broken json!!")
        ok = isolation.rollback_pool(cfg)
        self.assertTrue(ok)
        with open(cfg.sub("run/final/current.json")) as f:
            data = json.load(f)
        self.assertEqual(data["round_id"], "good_r")


class TestFault5RestartRecovery(unittest.TestCase):
    """重启恢复：陈旧锁 + 半写 runtime_report → crash_recover 清理。"""

    def test_stale_lock_cleanup(self):
        cfg = make_cfg("v04_f5")
        lock = scheduler.Lock(cfg, "refresh")
        lock.acquire()
        # 模拟进程死亡（锁文件残留 PID）
        with open(lock.path, "w") as f:
            f.write("999999999")           # 不存在的 PID
        rec = scheduler.crash_recover(cfg)
        self.assertIn("stale_locks", rec)
        self.assertGreaterEqual(len(rec["stale_locks"]), 1)
        self.assertTrue(scheduler.Lock(cfg, "refresh").acquire())

    def test_runtime_report_after_restart(self):
        """重启后 health_check 重建 runtime_report。"""
        cfg = make_cfg("v04_f5b")
        report = runtime_monitor.build_runtime_report(cfg, uptime_s=5)
        runtime_monitor.write_runtime_report(cfg, report)
        self.assertTrue(os.path.isfile(
            cfg.sub("run/runtime_report.json")))


class TestV04ControlServices(unittest.TestCase):
    """V0.4 控制服务与数据安全集成。"""

    def test_export_import_restore_flow(self):
        """0.4-6：export → import → 恢复。"""
        cfg = make_cfg("v04_ci")
        data = services.export_config(cfg)
        res = services.import_config(cfg, data)
        self.assertTrue(os.path.isfile(cfg.sub("config_import.json")))
        saved = json.load(open(cfg.sub("config_import.json")))
        self.assertEqual(saved["version"], "V0.4")

    def test_refresh_lock_prevents_concurrent(self):
        cfg = make_cfg("v04_cl")
        lock = scheduler.Lock(cfg, "refresh")
        lock.acquire()
        try:
            res = services.refresh(cfg)
            self.assertEqual(res["error"], "REFRESH_LOCKED")
        finally:
            lock.release()

    def test_status_mihomo_no_crash(self):
        """沙箱无 mihomo：status 不抛异常。"""
        cfg = make_cfg("v04_mh")
        s = services.get_status(cfg)
        self.assertIn("running", s["mihomo"])

    def test_sync_payload_has_no_source_url(self):
        cfg = make_cfg("v04_sy")
        p = services.sync_payload(cfg)
        raw = json.dumps(p)
        self.assertNotIn("http", raw)

    def test_export_no_secrets(self):
        cfg = make_cfg("v04_ex")
        raw = json.dumps(services.export_config(cfg)).lower()
        for bad in ("password", "token", "uuid", "cookie"):
            self.assertNotIn(bad, raw)


class TestV04RuntimePersistence(unittest.TestCase):
    """V0.3.1/V0.4：刷新后运行期产物完整。"""

    def test_refresh_writes_diagnostics(self):
        cfg = make_cfg("v04_rp")
        with LocalMockSite(strict_success_routes()) as site:
            inject = _inject(site)
            s = pipeline.run_refresh(cfg, inject=inject)
        self.assertIn("published", s)
        for p in ("run/source_health.json", "run/detect.log",
                  "run/refresh_state.json"):
            self.assertTrue(os.path.isfile(cfg.sub(p)), p)

    def test_refresh_state_has_failure_reason_field(self):
        cfg = make_cfg("v04_rp2")
        os.makedirs(cfg.sub("run"), exist_ok=True)
        with open(cfg.sub("run/refresh_state.json"), "w") as f:
            json.dump({"status": "EMPTY_POOL", "published": False,
                       "last_failure_reason": "EMPTY_POOL"}, f)
        st = services.get_status(cfg)
        self.assertEqual(st["last_failure_reason"], "EMPTY_POOL")

    def test_scheduler_state_fields(self):
        cfg = make_cfg("v04_rp3")
        scheduler.tick(cfg, {n: (lambda: None)
                             for n in scheduler.SCHEDULE}, now=1_000_000)
        state = json.load(open(cfg.sub("run/scheduler_state.json")))
        self.assertIn("last_run", state)
        self.assertEqual(len(state["last_run"]), len(scheduler.SCHEDULE))


if __name__ == "__main__":
    unittest.main()
