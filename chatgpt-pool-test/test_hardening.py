"""V0.2.1 production hardening 测试（P0-01..P0-05 + P1-01..P1-04 核心）。

覆盖：reject_summary 真实统计、run/final/current.json 隔离、start 空池保护与双端口
就绪、status 增强（active_pool/ports/version）、refresh 失败恢复、节点历史/
健康度/评分、reject.log。全部 Mock：不启动真实进程、不触碰生产目录。
"""
import base64
import json
import logging
import os
import unittest
from unittest import mock

import mihomo_svc
import pipeline
import poolstore
from history import NodeHistory
from helpers import (FakeEngine, LocalMockSite, chatgpt_probe,
                     fake_country_resolver, fake_engine_factory, make_cfg,
                     strict_success_routes, write_old_pool)

SS_ONE = ("ss://" + base64.b64encode(b"aes-256-gcm:pass1").decode()
          + "@10.0.0.1:8388#US")
SS_TWO = ("ss://" + base64.b64encode(b"aes-256-gcm:pass2").decode()
          + "@10.0.0.2:8388#JP")
SOURCE = SS_ONE + "\n" + SS_TWO + "\n"
GEO_CN = json.dumps({"query": "203.0.113.9", "countryCode": "CN"})


def _inject(site, country=None, engine_ok=True, geoip_body=None):
    return {
        "fetchers": lambda: [("mock", SOURCE)],
        "country_resolver": fake_country_resolver(country or ["US", "JP"]),
        "engine_factory": fake_engine_factory(start_ok=engine_ok),
        "probe_factory": lambda port: chatgpt_probe(site, geoip_body),
        "alloc_port": lambda: 19150,
    }


def _mixed_engine_inject(site, geoip_body=GEO_CN):
    """节点 A(10.0.0.1) 引擎启动失败 → PROXY_START_FAIL；
    节点 B 到达出口国家判定 → UNSUPPORTED_COUNTRY。真实混合失败。"""

    def factory(node):
        return FakeEngine(start_ok=node.server != "10.0.0.1")

    return {
        "fetchers": lambda: [("mock", SOURCE)],
        "country_resolver": fake_country_resolver(["US", "JP"]),
        "engine_factory": factory,
        "probe_factory": lambda port: chatgpt_probe(site, geoip_body),
        "alloc_port": lambda: 19151,
    }


def _current_payload(round_id="20261005000000", nodes=None):
    return {"round_id": round_id,
            "nodes": nodes or [{"name": "a", "protocol": "ss",
                                "server": "1.1.1.1", "port": 8388,
                                "password": "p", "params": {}}]}


class TestRejectSummary(unittest.TestCase):
    """P0-01：reject_summary 必须来自本轮真实 pipeline 结果。"""

    def test_reject_summary_mixed_reasons(self):
        cfg = make_cfg("h_reject")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_mixed_engine_inject(site))
        self.assertFalse(s["published"])
        self.assertEqual(s["reject_summary"], {
            "PROXY_START_FAIL": 1,
            "UNSUPPORTED_COUNTRY": 1,
        })

    def test_reject_summary_empty_on_success(self):
        cfg = make_cfg("h_reject_ok")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        self.assertEqual(s["reject_summary"], {})

    def test_engine_factory_failure_is_node_level(self):
        """引擎构造失败（如二进制缺失）→ 节点级 PROXY_START_FAIL，不拖垮整轮。"""
        cfg = make_cfg("h_engfail")

        def boom_factory(node):
            raise RuntimeError("mihomo binary not found")

        inject = {
            "fetchers": lambda: [("mock", SOURCE)],
            "country_resolver": fake_country_resolver(["US", "JP"]),
            "engine_factory": boom_factory,
            "probe_factory": lambda port: chatgpt_probe(
                LocalMockSite(strict_success_routes())),
            "alloc_port": lambda: 19153,
        }
        s = pipeline.run_refresh(cfg, inject=inject)
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "EMPTY_POOL")
        self.assertEqual(s["reject_summary"], {"PROXY_START_FAIL": 2})
        self.assertFalse(os.path.exists(cfg.pool_path))  # 首次失败不生成空池


class TestFinalIsolation(unittest.TestCase):
    """P0-02：成功 → current.json 替换；失败 → failed/{round_id}.json，绝不覆盖 current。"""

    def test_success_writes_current_final(self):
        cfg = make_cfg("h_cur_ok")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        with open(cfg.sub("run/final/current.json")) as f:
            cur = json.load(f)
        self.assertEqual(cur["round_id"], s["round_id"])
        self.assertEqual(len(cur["nodes"]), 2)
        # 成功轮次不落 failed/（目录为 ensure_dirs 预建，断言无摘要文件与明细）
        self.assertEqual([f for f in os.listdir(cfg.sub("run/failed"))
                          if f.endswith(".json")], [])
        self.assertEqual(os.listdir(cfg.sub("run/failed/nodes")), [])

    def test_failed_round_isolated_keeps_current(self):
        cfg = make_cfg("h_cur_keep")
        with LocalMockSite(strict_success_routes()) as site:
            ok = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(ok["published"])
        cur_before = open(cfg.sub("run/final/current.json")).read()
        pool_before = open(cfg.pool_path).read()

        with LocalMockSite(strict_success_routes()) as site:
            bad = pipeline.run_refresh(cfg, inject=_inject(site, engine_ok=False))
        self.assertFalse(bad["published"])
        self.assertEqual(bad["reason"], "EMPTY_POOL")
        # 失败轮次落 failed/，current.json 与 chatgpt.yaml 原样保留
        failed_path = cfg.sub(f"run/failed/{bad['round_id']}.json")
        self.assertTrue(os.path.isfile(failed_path))
        self.assertEqual(open(cfg.sub("run/final/current.json")).read(), cur_before)
        self.assertEqual(open(cfg.pool_path).read(), pool_before)

    def test_system_fail_writes_failed_round(self):
        cfg = make_cfg("h_sysfail")
        with LocalMockSite(strict_success_routes()) as site:
            ok = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(ok["published"])
        cur_before = open(cfg.sub("run/final/current.json")).read()
        bad = pipeline.run_refresh(cfg, inject={
            "fetchers": lambda: [("mock", SOURCE)],
            "country_resolver": fake_country_resolver(
                None, warning="COUNTRY_LIST_UNAVAILABLE"),
            "engine_factory": fake_engine_factory(),
            "probe_factory": lambda port: chatgpt_probe(
                LocalMockSite({})),
            "alloc_port": lambda: 19152,
        })
        self.assertEqual(bad["reason"], "COUNTRY_LIST_UNAVAILABLE")
        self.assertTrue(os.path.isfile(
            cfg.sub(f"run/failed/{bad['round_id']}.json")))
        self.assertEqual(open(cfg.sub("run/final/current.json")).read(), cur_before)

    def test_failed_round_contains_reject_summary(self):
        cfg = make_cfg("h_fail_diag")
        with LocalMockSite(strict_success_routes()) as site:
            bad = pipeline.run_refresh(cfg, inject=_inject(site, engine_ok=False))
        with open(cfg.sub(f"run/failed/{bad['round_id']}.json")) as f:
            diag = json.load(f)
        self.assertEqual(diag["reason"], "EMPTY_POOL")
        self.assertEqual(diag["reject_summary"], {"PROXY_START_FAIL": 2})
        self.assertEqual(diag["round_id"], bad["round_id"])

    def test_refresh_summary_pool_size(self):
        cfg = make_cfg("h_poolsize")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        self.assertEqual(s["pool_size"], 2)
        self.assertEqual(s["new_pool_count"], 2)
        self.assertTrue(s["round_id"])


class TestStartProtection(unittest.TestCase):
    """P0-03：Mihomo 绝不启动空池；代理+API 双端口就绪才成功。"""

    def setUp(self):
        self.cfg = make_cfg("h_start")
        self.cfg.mihomo_start_timeout = 0.4
        self.svc = mihomo_svc.ProductionMihomo(self.cfg)
        os.makedirs(self.svc.rundir, exist_ok=True)
        os.makedirs(self.cfg.sub("run/final"), exist_ok=True)

    def test_start_empty_active_pool(self):
        with open(self.cfg.sub("run/final/current.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"round_id": "x", "nodes": []}, f)
        summary = self.svc.start()
        self.assertFalse(summary["started"])
        self.assertEqual(summary["reason"], "NO_ACTIVE_POOL")

    def test_start_requires_both_ports(self):
        with open(self.cfg.sub("run/final/current.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_current_payload(), f)

        def open_7890_only(port, host="127.0.0.1", timeout=0.5):
            return port == 7890

        class FakePopen:
            def __init__(self, *a, **kw):
                self.pid = 4242
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                pass

            def kill(self):
                pass

        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open", open_7890_only):
            with self.assertRaises(mihomo_svc.ProxyStartError):
                self.svc.start()
        # 双端口都开 → 成功
        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open",
                        lambda port, host="127.0.0.1", timeout=0.5:
                        port in (7890, 9090)):
            summary = self.svc.start()
        self.assertTrue(summary["started"])
        self.assertEqual(summary["proxy_port"], 7890)
        self.assertEqual(summary["api_port"], 9090)


class TestStatusEnhanced(unittest.TestCase):
    """P0-04：status 提供 active_pool / ports / mihomo_version。"""

    def setUp(self):
        self.cfg = make_cfg("h_status")
        self.svc = mihomo_svc.ProductionMihomo(self.cfg)
        os.makedirs(self.svc.rundir, exist_ok=True)
        os.makedirs(self.cfg.sub("run/final"), exist_ok=True)

    def test_status_active_pool_ports_version(self):
        with open(self.cfg.sub("run/final/current.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_current_payload(round_id="20261006120000"), f)
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with mock.patch("mihomo_svc.is_port_open", return_value=True), \
             mock.patch("mihomo_svc.subprocess.check_output",
                        return_value=b"Clash Meta v1.19.31 android arm64"):
            st = self.svc.status()
        self.assertTrue(st["running"])
        self.assertEqual(st["active_pool"],
                         {"count": 1, "round_id": "20261006120000"})
        self.assertEqual(st["ports"]["proxy"], 7890)
        self.assertEqual(st["ports"]["api"], 9090)
        self.assertTrue(st["ports"]["proxy_open"])
        self.assertTrue(st["ports"]["api_open"])
        self.assertEqual(st["mihomo_version"], "v1.19.31")

    def test_status_no_pool_version_unknown(self):
        with mock.patch("mihomo_svc.subprocess.check_output",
                        side_effect=FileNotFoundError("no mihomo")):
            st = self.svc.status()
        self.assertFalse(st["running"])
        self.assertIsNone(st["active_pool"])
        self.assertEqual(st["mihomo_version"], "unknown")

    def test_mihomo_version_parse_and_fallback(self):
        with mock.patch("mihomo_svc.subprocess.check_output",
                        return_value=b"v1.19.31"):
            self.assertEqual(self.svc._mihomo_version(), "v1.19.31")
        with mock.patch("mihomo_svc.subprocess.check_output",
                        return_value=b"mihomo alpha 1.18.0"):
            self.assertEqual(self.svc._mihomo_version(), "1.18.0")
        with mock.patch("mihomo_svc.subprocess.check_output",
                        side_effect=OSError("boom")):
            self.assertEqual(self.svc._mihomo_version(), "unknown")


class TestRefreshRecovery(unittest.TestCase):
    """刷新失败：旧池保留 + 完整诊断（P0-05 / §28 / §30）。"""

    def test_failure_keeps_old_pool_with_diagnostics(self):
        cfg = make_cfg("h_recovery")
        write_old_pool(cfg)
        old_text = open(cfg.pool_path).read()
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site, engine_ok=False))
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "EMPTY_POOL")
        self.assertEqual(s["reject_summary"], {"PROXY_START_FAIL": 2})
        self.assertEqual(open(cfg.pool_path).read(), old_text)
        self.assertEqual(s["pool_size"], 0)


class TestNodeHistory(unittest.TestCase):
    """P1-01/02/03：跨轮历史、健康度派生、评分排序（不改淘汰规则）。"""

    def _tmp_history(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="hist_")
        return NodeHistory(os.path.join(d, "node_history.json"))

    def test_record_health_and_no_delete(self):
        h = self._tmp_history()
        for _ in range(3):
            h.record("n1", ok=True, latency=0.5)
        self.assertEqual(h.data["n1"]["status"], "GOOD")
        self.assertEqual(h.data["n1"]["attempts"], 3)
        for _ in range(2):
            h.record("n1", ok=False, latency=1.0, reason="TIMEOUT")
        # 最近 5 次 [T,T,T,F,F] → 3/5 → GOOD
        self.assertEqual(h.data["n1"]["status"], "GOOD")
        self.assertEqual(h.data["n1"]["last_failure"] != "", True)
        # 历史保留（禁止一次失败永久删除）
        self.assertEqual(len(h.data["n1"]["history"]), 5)
        h.record("n1", ok=False)   # [T,T,F,F,F] → 2/5 → DEGRADED
        self.assertEqual(h.data["n1"]["status"], "DEGRADED")
        h.record("n1", ok=False)   # [T,F,F,F,F] → 1/5 → DEGRADED
        h.record("n1", ok=False)   # [F,F,F,F,F] → 0/5 → BAD
        self.assertEqual(h.data["n1"]["status"], "BAD")
        self.assertEqual(len(h.data["n1"]["history"]), 8)  # 失败也记录，不删除
        h.record("n2", ok=False)
        h.record("n2", ok=False)
        self.assertEqual(h.data["n2"]["status"], "BAD")
        h.mark_removed("n2")
        self.assertEqual(h.data["n2"]["status"], "REMOVED")
        self.assertTrue(h.data["n2"]["history"])  # 标记后历史仍在

    def test_score_ordering(self):
        h = self._tmp_history()
        for _ in range(3):
            h.record("good", ok=True, latency=0.3)
        h.record("bad", ok=True, latency=0.3)
        h.record("bad", ok=False, latency=9.0)
        self.assertGreater(h.score("good"), h.score("bad"))
        self.assertGreater(h.score("bad"), 0.0)

    def test_history_persisted_by_pipeline(self):
        cfg = make_cfg("h_hist")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        hist_path = cfg.sub("state/node_history.json")
        self.assertTrue(os.path.isfile(hist_path))
        with open(hist_path) as f:
            data = json.load(f)
        self.assertTrue(data)  # 每节点都有记录
        self.assertTrue(all(r["attempts"] >= 1 for r in data.values()))


class TestRejectLog(unittest.TestCase):
    """P1-04：REJECTED 明细写入 logs/reject.log。"""

    def test_reject_log_written(self):
        cfg = make_cfg("h_rejectlog")
        os.makedirs(cfg.sub("logs"), exist_ok=True)
        rj = logging.getLogger("pool.reject")
        rj.handlers[:] = [logging.FileHandler(
            cfg.sub("logs/reject.log"), encoding="utf-8")]
        rj.setLevel(logging.INFO)
        rj.propagate = False
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site, engine_ok=False))
        rj.handlers.clear()
        self.assertFalse(s["published"])
        log_path = cfg.sub("logs/reject.log")
        self.assertTrue(os.path.isfile(log_path))
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("REJECT", text)
        self.assertIn("PROXY_START_FAIL", text)


if __name__ == "__main__":
    unittest.main()
