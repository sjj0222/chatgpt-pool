"""V0.3 Phase 7 Scheduler 测试。

覆盖：任务周期（§23）、锁（§32）、tick 到期执行与持久化、崩溃恢复（§31）、
备份（§34）、资源限制压缩（§33）、服务探测（§22 L3）、CLI 子命令。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import scheduler                                          # noqa: E402
import probe_services                                     # noqa: E402
from lifecycle_store import NodeLifecycleStore            # noqa: E402
from helpers import make_cfg                              # noqa: E402


class TestSchedule(unittest.TestCase):
    def test_default_periods(self):
        self.assertEqual(scheduler.SCHEDULE["refresh"], 6 * 3600)
        self.assertEqual(scheduler.SCHEDULE["probe"], 30 * 60)
        self.assertEqual(scheduler.SCHEDULE["score"], 3600)
        self.assertEqual(scheduler.SCHEDULE["cleanup"], 24 * 3600)

    def test_probe_levels(self):
        self.assertEqual(scheduler.PROBE_LEVELS["refresh"], 1)
        self.assertEqual(scheduler.PROBE_LEVELS["score"], 2)
        self.assertEqual(scheduler.PROBE_LEVELS["probe"], 3)

    def test_due_initial_all(self):
        cfg = make_cfg("v03_sched1")
        due = scheduler.due_tasks(cfg, now=1_000_000)
        self.assertEqual(sorted(due), sorted(scheduler.SCHEDULE))

    def _all_runners(self):
        # V0.3.1：SCHEDULE 六任务（refresh/probe/score/cleanup/health_check/backup）
        return {name: (lambda: None) for name in scheduler.SCHEDULE}

    def test_due_after_tick_none(self):
        cfg = make_cfg("v03_sched2")
        scheduler.tick(cfg, self._all_runners(), now=1_000_000)
        due = scheduler.due_tasks(cfg, now=1_000_000 + 10)
        self.assertEqual(due, [])

    def test_due_refresh_after_6h(self):
        cfg = make_cfg("v03_sched3")
        scheduler.tick(cfg, self._all_runners(), now=1_000_000)
        due = scheduler.due_tasks(cfg, now=1_000_000 + 6 * 3600)
        self.assertIn("refresh", due)     # 6h 到期
        self.assertIn("probe", due)       # 30min 周期早已到期
        self.assertIn("score", due)       # 1h 周期早已到期
        self.assertNotIn("cleanup", due)  # 24h 未到期

    def test_tick_persists_last_run(self):
        cfg = make_cfg("v03_sched4")
        scheduler.tick(cfg, {"refresh": lambda: None}, now=1_000_000)
        state = json.load(open(cfg.sub("run/scheduler_state.json")))
        self.assertEqual(state["last_run"]["refresh"], 1_000_000)
        self.assertEqual(state["last_tick"], 1_000_000)


class TestLock(unittest.TestCase):
    def test_lock_exclusive(self):
        cfg = make_cfg("v03_lock1")
        a = scheduler.Lock(cfg, "refresh")
        b = scheduler.Lock(cfg, "refresh")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())     # 已持有
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_lock_context_manager(self):
        cfg = make_cfg("v03_lock2")
        with scheduler.Lock(cfg, "probe") as lock:
            self.assertTrue(lock.acquired)
            self.assertTrue(os.path.isfile(cfg.sub("run/locks/probe.lock")))
        self.assertFalse(os.path.isfile(cfg.sub("run/locks/probe.lock")))


class TestCrashRecover(unittest.TestCase):
    def test_stale_lock_cleaned(self):
        cfg = make_cfg("v03_recover1")
        os.makedirs(cfg.sub("run/locks"), exist_ok=True)
        with open(cfg.sub("run/locks/refresh.lock"), "w") as f:
            f.write("999999999")          # 不存在的 PID
        rec = scheduler.crash_recover(cfg)
        self.assertTrue(rec["stale_locks"])
        self.assertFalse(os.path.isfile(cfg.sub("run/locks/refresh.lock")))

    def test_recover_active_pool(self):
        cfg = make_cfg("v03_recover2")
        os.makedirs(cfg.sub("run/final"), exist_ok=True)
        with open(cfg.sub("run/final/current.json"), "w") as f:
            json.dump({"round_id": "r", "nodes": []}, f)
        rec = scheduler.crash_recover(cfg)
        self.assertTrue(rec["active_pool_exists"])


class TestBackupAndSize(unittest.TestCase):
    def test_backup_copies_targets(self):
        cfg = make_cfg("v03_backup1")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": "ACTIVE"})
        lc.save()
        base = scheduler.backup(cfg, dest_dir=cfg.sub("backup/test"))
        self.assertTrue(os.path.isfile(os.path.join(base, "lifecycle.json")))
        data = json.load(open(os.path.join(base, "lifecycle.json")))
        self.assertEqual(data["nodes"]["n1"]["life_state"], "ACTIVE")

    def test_enforce_size_limit_compresses(self):
        cfg = make_cfg("v03_size1")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": "ACTIVE",
                         "score_history": [{"t": i, "overall": 1.0}
                                           for i in range(60)],
                         "recent_attempts": [{"t": i, "ok": True}
                                             for i in range(300)]})
        lc.save()
        res = scheduler.enforce_size_limit(cfg, limit=0)  # 强制触发
        self.assertTrue(res["compressed"])
        lc2 = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        self.assertEqual(len(lc2.get("n1")["score_history"]),
                         scheduler.COMPRESS_SCORE_HISTORY)
        self.assertEqual(len(lc2.get("n1")["recent_attempts"]),
                         scheduler.COMPRESS_ATTEMPTS)


class TestServiceProbe(unittest.TestCase):
    def test_run_service_probe_with_injected_fn(self):
        cfg = make_cfg("v03_probe1")
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        lc.upsert("n1", {"life_state": "ACTIVE"})
        lc.upsert("n2", {"life_state": "STANDBY"})
        lc.save()

        def fake_probe(url):
            return 100.0 if "github" in url else 0.0

        res = probe_services.run_service_probe(cfg, node_ids=["n1", "n2"],
                                               probe_fn=fake_probe)
        self.assertEqual(res["n1"]["github"], 100.0)
        self.assertEqual(res["n1"]["web"], 0.0)
        lc2 = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        self.assertIn("probe_scores", lc2.get("n1"))

    def test_targets_cover_capabilities(self):
        targets = set(probe_services.SERVICE_TARGETS)
        self.assertEqual(targets, {"github", "claude", "gemini", "web"})


if __name__ == "__main__":
    unittest.main()
