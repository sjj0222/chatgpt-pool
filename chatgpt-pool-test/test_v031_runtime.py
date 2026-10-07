"""V0.3.1 Runtime Verification 测试（Phase 0.3.1-2/3/4）。

覆盖：health_check/backup 任务、锁防 refresh 并发、日志 rotate、资源监控、
runtime_report 字段（uptime/pool_size/active_nodes/score_average/
source_health/error_count）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import runtime_monitor                                     # noqa: E402
import scheduler                                          # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from helpers import make_cfg                              # noqa: E402


class TestRuntimeMonitor(unittest.TestCase):
    def test_resource_stats_fields(self):
        cfg = make_cfg("v031_rs")
        st = runtime_monitor.resource_stats(cfg)
        for k in ("memory_rss_kb", "disk_workdir_bytes", "log_size_bytes",
                  "data_size_bytes", "log_limit_bytes"):
            self.assertIn(k, st)
        self.assertEqual(st["log_limit_bytes"],
                         runtime_monitor.LOG_ROTATE_BYTES)

    def test_log_rotate(self):
        cfg = make_cfg("v031_rot")
        os.makedirs(cfg.sub("logs"), exist_ok=True)
        p = cfg.sub("logs/pool.log")
        with open(p, "w") as f:
            f.write("x" * (runtime_monitor.LOG_ROTATE_BYTES + 100))
        rotated = runtime_monitor.rotate_if_needed(p)
        self.assertTrue(rotated)
        self.assertTrue(os.path.isfile(p + ".1"))
        # 原文件已轮转（不存在或为空，由 logging handler 重建）
        self.assertTrue(not os.path.isfile(p) or os.path.getsize(p) == 0)

    def test_rotate_all_logs(self):
        cfg = make_cfg("v031_rot2")
        os.makedirs(cfg.sub("logs"), exist_ok=True)
        for f in ("pool.log", "reject.log"):
            with open(cfg.sub("logs/" + f), "w") as fh:
                fh.write("x" * (runtime_monitor.LOG_ROTATE_BYTES + 1))
        rotated = runtime_monitor.rotate_all_logs(cfg)
        self.assertEqual(len(rotated), 2)


class TestRuntimeReport(unittest.TestCase):
    def test_report_fields(self):
        cfg = make_cfg("v031_rpt")
        os.makedirs(cfg.sub("run/final"), exist_ok=True)
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "r1", "nodes": [{"node_id": "n1"}]}, f)
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": "ACTIVE",
                         "success_count": 5, "fail_count": 1,
                         "score": {"overall": 82.0}})
        lc.upsert("n2", {"life_state": "FAILED", "success_count": 0,
                         "fail_count": 3, "score": {"overall": 40.0}})
        lc.save()
        report = runtime_monitor.build_runtime_report(cfg, uptime_s=600)
        for k in ("uptime_s", "pool_size", "active_nodes", "success_rate",
                  "average_score", "source_count", "source_health",
                  "error_count"):
            self.assertIn(k, report)
        self.assertEqual(report["pool_size"], 1)
        self.assertEqual(report["active_nodes"], 1)
        self.assertEqual(report["average_score"], 82.0)
        self.assertGreaterEqual(report["success_rate"], 0.5)

    def test_write_runtime_report(self):
        cfg = make_cfg("v031_rpt2")
        path = runtime_monitor.write_runtime_report(
            cfg, runtime_monitor.build_runtime_report(cfg, uptime_s=10))
        self.assertTrue(os.path.isfile(path))
        data = json.load(open(path))
        self.assertEqual(data["uptime_s"], 10)


class TestSchedulerV031(unittest.TestCase):
    def test_new_tasks_in_schedule(self):
        self.assertIn("health_check", scheduler.SCHEDULE)
        self.assertIn("backup", scheduler.SCHEDULE)
        self.assertEqual(scheduler.SCHEDULE["health_check"], 3600)
        self.assertEqual(scheduler.SCHEDULE["backup"], 12 * 3600)

    def test_tick_uses_lock_for_refresh(self):
        """0.3.1-2：refresh 任务在锁内执行（禁止 refresh/refresh 并发）。"""
        cfg = make_cfg("v031_lock")
        ran = {"refresh": False}
        state = {"locked_seen": False}

        def _refresh():
            # 锁内运行时，再次获取应失败 → 证明已被锁保护
            state["locked_seen"] = not scheduler.Lock(cfg, "refresh").acquire()
            ran["refresh"] = True

        scheduler.tick(cfg, {"refresh": _refresh}, now=1_000_000)
        self.assertTrue(ran["refresh"])
        self.assertTrue(state["locked_seen"])
        # 锁已释放
        self.assertTrue(scheduler.Lock(cfg, "refresh").acquire())

    def test_health_check_runner_works(self):
        """health_check 生成 runtime_report + 资源统计。"""
        import main
        cfg = make_cfg("v031_hc")
        cfg.test_web_port = 18999
        cfg.web_port = 18999
        rc = main.cmd_scheduler(cfg, "health_check")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(
            cfg.sub("run/runtime_report.json")))

    def test_backup_runner_works(self):
        import main
        cfg = make_cfg("v031_bk")
        rc = main.cmd_scheduler(cfg, "backup")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
