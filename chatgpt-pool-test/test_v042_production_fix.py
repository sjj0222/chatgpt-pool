"""V0.4.2 Production Fix 测试。

P0 诊断持久化：任何 refresh 失败路径必须生成 5 个诊断文件
（source_health/detect.log/refresh_state/failed/{round}.json/failed/nodes/{round}.jsonl），
覆盖 ENVIRONMENT_ANOMALY / COUNTRY_LIST_UNAVAILABLE / INTERNAL_ERROR(全源失败) /
EMPTY_POOL / 全节点失败。
P1 mihomo stop：PID 不存在 → already_stopped，不因残留端口失败。
P3 health_check 必生成 runtime_report.json 及必需字段。
P4 POST refresh/start/stop API dispatch 可路由。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import pipeline                                              # noqa: E402
import runtime_monitor                                        # noqa: E402
from helpers import make_cfg                                 # noqa: E402

DIAG_FILES = [
    "run/source_health.json",
    "run/detect.log",
    "run/refresh_state.json",
]


def _all_diag_exist(cfg, round_id):
    paths = [cfg.sub(p) for p in DIAG_FILES]
    paths += [cfg.sub(f"run/failed/{round_id}.json"),
              cfg.sub(f"run/failed/nodes/{round_id}.jsonl")]
    return {p: os.path.isfile(p) for p in paths}


def _boom():
    raise RuntimeError("boom")


class TestDiagPersistenceP0(unittest.TestCase):
    """P0：失败路径诊断产物齐全。"""

    def _run(self, cfg, inject):
        return pipeline.run_refresh(cfg, inject=inject)

    def test_all_sources_failed(self):
        """INTERNAL_ERROR：全源失败 → 5 个诊断文件。"""
        cfg = make_cfg("v042_intern")
        res = self._run(cfg, {"fetchers": [("s1", _boom)]})
        self.assertEqual(res["reason"],
                         "INTERNAL_ERROR: all node sources failed")
        d = _all_diag_exist(cfg, res["round_id"])
        self.assertTrue(all(d.values()), d)

    def test_country_list_unavailable(self):
        """COUNTRY_LIST_UNAVAILABLE → 5 个诊断文件。"""
        cfg = make_cfg("v042_country")
        res = self._run(cfg, {
            "fetchers": [("s1", "ss://AAA@1.2.3.4:443#n")],
            "country_resolver": lambda cfg: (None, None, None),
        })
        self.assertEqual(res["reason"], "COUNTRY_LIST_UNAVAILABLE")
        d = _all_diag_exist(cfg, res["round_id"])
        self.assertTrue(all(d.values()), d)

    def test_empty_pool_all_nodes_fail(self):
        """EMPTY_POOL：节点全部被淘汰 → 5 个诊断文件。"""
        cfg = make_cfg("v042_empty")
        res = self._run(cfg, {
            "fetchers": [("s1", "ss://AAA@1.2.3.4:443#n")],
            "country_resolver": lambda cfg: ({"US"}, "builtin", None),
        })
        # 沙箱无真 mihomo → 节点 PROXY_START_FAIL → EMPTY_POOL
        self.assertEqual(res["reason"], "EMPTY_POOL")
        d = _all_diag_exist(cfg, res["round_id"])
        self.assertTrue(all(d.values()), d)

    def test_bootstrap_failed_no_empty_yaml(self):
        """首次失败：BOOTSTRAP_FAILED，不生成空 chatgpt.yaml。"""
        cfg = make_cfg("v042_boot")
        res = self._run(cfg, {"fetchers": [("s1", _boom)]})
        self.assertTrue(res.get("bootstrap_failed"))
        self.assertFalse(os.path.isfile(cfg.sub("chatgpt.yaml"))
                         or os.path.isfile(cfg.sub("run/chatgpt.yaml")))

    def test_failed_nodes_jsonl_has_records(self):
        """全节点失败：failed/nodes/*.jsonl 有记录（每行一个节点）。"""
        cfg = make_cfg("v042_jsonl")
        res = self._run(cfg, {
            "fetchers": [("s1", "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:443#a"),
                         ("s2", "trojan://pass@2.3.4.5:443#b")],
            "country_resolver": lambda cfg: ({"US"}, "builtin", None),
        })
        p = cfg.sub(f"run/failed/nodes/{res['round_id']}.jsonl")
        self.assertTrue(os.path.isfile(p))
        with open(p) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertIn("failure_reason", r)
            self.assertIn("failure_stage", r)

    def test_refresh_state_has_fields(self):
        """refresh_state.json 字段完整。"""
        cfg = make_cfg("v042_rs")
        self._run(cfg, {"fetchers": [("s1", _boom)]})
        with open(cfg.sub("run/refresh_state.json")) as f:
            rs = json.load(f)
        for k in ("round_id", "published", "pool_size", "status",
                  "last_refresh", "last_failure_reason"):
            self.assertIn(k, rs)

    def test_failed_summary_reason_present(self):
        """failed/{round}.json 摘要含 reason。"""
        cfg = make_cfg("v042_sum")
        res = self._run(cfg, {"fetchers": [("s1", _boom)]})
        with open(cfg.sub(f"run/failed/{res['round_id']}.json")) as f:
            s = json.load(f)
        self.assertIn("reason", s)

    def test_detect_log_mentions_reason(self):
        """detect.log 含本轮失败原因。"""
        cfg = make_cfg("v042_log")
        res = self._run(cfg, {"fetchers": [("s1", _boom)]})
        with open(cfg.sub("run/detect.log")) as f:
            text = f.read()
        self.assertIn(res["round_id"], text)
        self.assertIn("INTERNAL_ERROR", text)


class TestStopAlreadyStoppedP1(unittest.TestCase):
    """P1：PID 不存在 → already_stopped，不做端口检测。"""

    def test_stop_no_pid(self):
        from mihomo_svc import ProductionMihomo
        cfg = make_cfg("v042_stop1")
        svc = ProductionMihomo(cfg)
        out = svc.stop()
        self.assertEqual(out["status"], "already_stopped")
        self.assertTrue(out["stopped"])

    def test_stop_dead_pid(self):
        from mihomo_svc import ProductionMihomo
        cfg = make_cfg("v042_stop2")
        svc = ProductionMihomo(cfg)
        os.makedirs(os.path.dirname(svc.pid_path), exist_ok=True)
        with open(svc.pid_path, "w") as f:
            f.write("999999")          # 已退出的 PID
        out = svc.stop()
        self.assertEqual(out["status"], "already_stopped")


class TestRuntimeReportP3(unittest.TestCase):
    """P3：health_check 后必生成 runtime_report.json + 必需字段。"""

    def test_health_check_writes_report(self):
        import main as _main
        cfg = make_cfg("v042_hc")
        rc = _main.cmd_scheduler(cfg, "health_check")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(cfg.sub("run/runtime_report.json")))
        with open(cfg.sub("run/runtime_report.json")) as f:
            r = json.load(f)
        for k in ("uptime_s", "pool_size", "active_nodes", "success_rate",
                  "average_score", "source_health", "error_count"):
            self.assertIn(k, r)

    def test_history_archive_dir_exists(self):
        cfg = make_cfg("v042_hc2")
        runtime_monitor.write_runtime_report(cfg)
        self.assertTrue(os.path.isdir(cfg.sub("run/history/runtime")))
        self.assertGreaterEqual(
            len(os.listdir(cfg.sub("run/history/runtime"))), 1)


class TestAPIPostP4(unittest.TestCase):
    """P4：POST refresh/start/stop dispatch 可路由（不 404）。"""

    def test_post_refresh_routes(self):
        from api import dispatch
        cfg = make_cfg("v042_api1")
        body, status = dispatch(cfg, "POST", "/api/refresh")
        self.assertNotEqual(status, 404)

    def test_post_start_stop_routes(self):
        from api import dispatch
        cfg = make_cfg("v042_api2")
        _, s1 = dispatch(cfg, "POST", "/api/start")
        self.assertNotEqual(s1, 404)
        _, s2 = dispatch(cfg, "POST", "/api/stop")
        self.assertNotEqual(s2, 404)

    def test_get_status_route(self):
        from api import dispatch
        cfg = make_cfg("v042_api3")
        body, status = dispatch(cfg, "GET", "/api/status")
        self.assertNotEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
