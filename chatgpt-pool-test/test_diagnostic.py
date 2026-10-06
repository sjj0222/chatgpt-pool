"""V0.2.2 Diagnostic Enhancement 测试。

覆盖：P0-01 细分原因（14 枚举映射）/ P0-02 失败节点明细（≤1000）/ P0-03
detect.log / P0-04 协议统计 / P0-05 源健康 / P1-01 上限 / P1-02 status
增强 / P1-03 refresh 分阶段统计。

原则：顶层 failure_reason（§16）驱动准入与优先级不变（V5.2 权威），
细分原因（SubReason）作为诊断层（failure_stage + failure_detail.sub_reason）。
"""
import json
import logging
import os
import socket
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

from config import Config                                # noqa: E402
from helpers import (                                    # noqa: E402
    LocalMockSite, make_cfg, strict_success_routes, fake_country_resolver,
    chatgpt_probe, write_old_pool)
import chatgpt_check                                     # noqa: E402
import egress                                            # noqa: E402
import mihomo_svc                                        # noqa: E402
import pipeline                                          # noqa: E402
import poolstore                                         # noqa: E402
from models import DiagnosticStage, FailureReason, Node, Stage, SubReason  # noqa: E402
from probe import ProbeError, ProbeResult, classify_exception  # noqa: E402

SOURCE_A = "https://src-a.example/list.txt"
SOURCE_B = "https://src-b.example/list.txt"
SOURCE_DEAD = "https://dead.example/list.txt"

NODE_A = ("ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4NDQz\n"   # ss 节点
          "#nodeA\n")
NODE_B = ("trojan://pass@example.com:443?security=tls#nodeB\n")
NODE_C = ("vmess://eyJ2IjoiMiIsInBzIjoibm9kZUMiLCJhZGQiOiJleGFtcGxlLmNvbSIs"
          "InBvcnQiOiI4NDQzIiwiYWlkIjoiMCIsImlkIjoiMDAwMDAwMDAtMDAwMC0wMDAw"
          "LTAwMDAtMDAwMDAwMDAwMDAwIiwibmV0IjoidGNwIiwidHlwZSI6Im5vbmUifQ==\n")


def _mixed_fail_probe(site):
    """geoip 成功（US），chatgpt 三个 endpoint 全 TLS_TIMEOUT → TLS 阶段失败。"""
    def fn(url):
        if "ip-api.com" in url:
            return ProbeResult(200, url, "application/json",
                               '{"query":"203.0.113.9","countryCode":"US"}')
        raise ProbeError(FailureReason.TLS_FAIL,
                         sub=SubReason.TLS_TIMEOUT,
                         detail={"type": "socket.timeout",
                                 "elapsed_ms": 3500, "url": url})
    return fn


def _inject(site, probe_fn=None):
    return {
        "fetchers": lambda: [(SOURCE_A, NODE_A + NODE_B + NODE_C),
                             (SOURCE_B, NODE_A + NODE_B)],
        "country_resolver": fake_country_resolver(["US", "JP"]),
        "engine_factory": lambda node: _OkEngine(),
        "probe_factory": lambda port: (probe_fn or chatgpt_probe(site)),
        "alloc_port": lambda: 19160,
    }


class _OkEngine:
    """成功启动的假引擎。"""
    def start(self, node_dict, mixed_port):
        return mixed_port or 19160

    def stop(self):
        pass


class TestClassifyException(unittest.TestCase):
    """P0-01：14 个细分原因映射（V0.2.2 统一枚举）。"""

    def test_tcp_timeout_http(self):
        top, sub, d = classify_exception(socket.timeout(), "http://x/")
        self.assertEqual(top, FailureReason.TIMEOUT)
        self.assertEqual(sub, SubReason.TCP_TIMEOUT)

    def test_tcp_refused(self):
        top, sub, _ = classify_exception(ConnectionRefusedError(), "http://x/")
        self.assertEqual(top, FailureReason.PROXY_ERROR)
        self.assertEqual(sub, SubReason.TCP_REFUSED)

    def test_tcp_error_reset(self):
        top, sub, _ = classify_exception(ConnectionResetError(), "https://x/")
        self.assertEqual(top, FailureReason.PROXY_ERROR)
        self.assertEqual(sub, SubReason.TCP_ERROR)

    def test_proxy_handshake_fail(self):
        from probe import ProxyProbe
        cfg = make_cfg("d_proxy")
        # 探测一个必然关闭的端口
        p = ProxyProbe(cfg, 1)  # port 1 无服务
        with self.assertRaises(ProbeError) as cm:
            p("https://chat.openai.com/")
        self.assertEqual(cm.exception.sub, SubReason.PROXY_HANDSHAKE_FAIL)
        self.assertIn("proxy_port", cm.exception.detail)

    def test_tls_timeout_https(self):
        top, sub, _ = classify_exception(socket.timeout(), "https://x/")
        self.assertEqual(top, FailureReason.TLS_FAIL)
        self.assertEqual(sub, SubReason.TLS_TIMEOUT)

    def test_tls_cert_fail(self):
        e = ssl.SSLCertVerificationError("certificate verify failed")
        top, sub, _ = classify_exception(e, "https://x/")
        self.assertEqual(top, FailureReason.TLS_FAIL)
        self.assertEqual(sub, SubReason.TLS_CERT_FAIL)

    def test_tls_sni_fail(self):
        e = ssl.SSLError("hostname 'x' doesn't match '*.y'")
        top, sub, _ = classify_exception(e, "https://x/")
        self.assertEqual(top, FailureReason.TLS_FAIL)
        self.assertEqual(sub, SubReason.TLS_SNI_FAIL)

    def test_tls_handshake_fail(self):
        e = ssl.SSLError("WRONG_VERSION_NUMBER: tls handshake failure")
        top, sub, _ = classify_exception(e, "https://x/")
        self.assertEqual(top, FailureReason.TLS_FAIL)
        self.assertEqual(sub, SubReason.TLS_HANDSHAKE_FAIL)

    def test_dns_fail_unknown_sub(self):
        e = socket.gaierror(8, "Name or service not known")
        top, sub, d = classify_exception(e, "https://x/")
        self.assertEqual(top, FailureReason.DNS_FAIL)
        self.assertEqual(sub, SubReason.UNKNOWN_FAIL)
        self.assertIn("dns_error", d)

    def test_unknown_fail_fallback(self):
        top, sub, _ = classify_exception(ValueError("weird"), "https://x/")
        self.assertEqual(top, FailureReason.PROXY_ERROR)
        self.assertEqual(sub, SubReason.UNKNOWN_FAIL)


class TestEgressDiagnostics(unittest.TestCase):
    """P0-01：egress 阶段细分（NO_EGRESS_IP / EGRESS_TIMEOUT）。"""

    def _cfg(self):
        return make_cfg("d_egress")

    def test_no_egress_ip_on_missing_query(self):
        cfg = self._cfg()

        def probe(url):
            return ProbeResult(200, url, "application/json", '{"foo":1}')
        ip, iso, reason, sub, detail = egress.get_egress(probe, cfg)
        self.assertIsNone(ip)
        self.assertEqual(reason, FailureReason.NO_EGRESS_IP)
        self.assertEqual(sub, SubReason.NO_EGRESS_IP)
        self.assertIn("body_snippet", detail)

    def test_egress_timeout_mapping(self):
        cfg = self._cfg()

        def probe(url):
            raise ProbeError(FailureReason.TLS_FAIL,
                             sub=SubReason.TLS_TIMEOUT,
                             detail={"elapsed_ms": 25000})
        ip, iso, reason, sub, detail = egress.get_egress(probe, cfg)
        self.assertEqual(reason, FailureReason.TIMEOUT)
        self.assertEqual(sub, SubReason.EGRESS_TIMEOUT)
        self.assertEqual(detail["probe_sub"], "TLS_TIMEOUT")


class TestChatgptDiagnostics(unittest.TestCase):
    """P0-01：ChatGPT 检测细分（CHATGPT_BLOCKED / CHATGPT_TIMEOUT）。"""

    def test_blocked_all(self):
        cfg = make_cfg("d_blocked")
        routes = {p: (403, {}, b"blocked") for p in
                  ("/", "/auth", "/backend")}
        with LocalMockSite(routes) as site:
            res = chatgpt_check.run_strict_check(
                Node(), chatgpt_probe(site), cfg)
        self.assertEqual(res["failure_reason"], FailureReason.OPENAI_BLOCKED)
        self.assertEqual(res["failure_sub"], SubReason.CHATGPT_BLOCKED)

    def test_chatgpt_timeout_all_transport(self):
        cfg = make_cfg("d_ctimeout")

        def probe(url):
            raise ProbeError(FailureReason.TIMEOUT,
                             sub=SubReason.TCP_TIMEOUT,
                             detail={"elapsed_ms": 25000})
        res = chatgpt_check.run_strict_check(Node(), probe, cfg)
        self.assertEqual(res["failure_reason"], FailureReason.TIMEOUT)
        self.assertEqual(res["failure_sub"], SubReason.CHATGPT_TIMEOUT)

    def test_tls_sub_preserved_when_all_transport_fail(self):
        cfg = make_cfg("d_tlssub")
        res = chatgpt_check.run_strict_check(Node(), _mixed_fail_probe(None), cfg)
        self.assertEqual(res["failure_reason"], FailureReason.TLS_FAIL)
        self.assertEqual(res["failure_sub"], SubReason.TLS_TIMEOUT)


class TestFailedNodesOutput(unittest.TestCase):
    """P0-02 + P1-01：失败节点明细 jsonl（≤1000，保留最近）。"""

    def _rejected_node(self, i, proto="ss", source=SOURCE_A,
                       stage="TLS", sub="TLS_TIMEOUT"):
        return Node(node_id=f"n{i}", protocol=proto, server=f"{i}.2.3.4",
                    port=443, source=source, stage=Stage.REJECTED.value,
                    failure_reason=FailureReason.TLS_FAIL.value,
                    failure_stage=DiagnosticStage.TLS.value,
                    failure_detail={"sub_reason": sub,
                                    "elapsed_ms": 3500, "server": f"{i}.2.3.4",
                                    "port": 443})

    def test_fields_written(self):
        cfg = make_cfg("d_fnodes")
        poolstore.ensure_dirs(cfg)
        path = poolstore.write_failed_nodes(
            cfg, "20261007000001",
            [self._rejected_node(1), self._rejected_node(2, sub="TLS_SNI_FAIL")])
        self.assertTrue(path)
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        self.assertEqual(len(lines), 2)
        row = json.loads(lines[0])
        for key in ("node_id", "protocol", "server", "port", "source",
                    "stage", "failure_reason", "failure_stage",
                    "failure_detail", "sub_reason"):
            self.assertIn(key, row)
        self.assertEqual(row["node_id"], "n1")
        self.assertEqual(row["failure_stage"], "TLS")
        self.assertEqual(row["sub_reason"], "TLS_TIMEOUT")

    def test_limit_1000_keeps_recent(self):
        cfg = make_cfg("d_fnodes_limit")
        poolstore.ensure_dirs(cfg)
        nodes = [self._rejected_node(i) for i in range(1005)]
        path = poolstore.write_failed_nodes(cfg, "20261007000002", nodes)
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        self.assertEqual(len(lines), 1000)
        self.assertEqual(json.loads(lines[0])["node_id"], "n5")
        self.assertEqual(json.loads(lines[-1])["node_id"], "n1004")

    def test_none_when_no_rejected(self):
        cfg = make_cfg("d_fnodes_none")
        poolstore.ensure_dirs(cfg)
        n = Node(node_id="ok1", stage=Stage.FINAL.value)
        self.assertIsNone(poolstore.write_failed_nodes(cfg, "r1", [n]))


class TestDiagnosticStats(unittest.TestCase):
    """P0-04 + P1-03：细化统计（detailed / by_protocol / stage）。"""

    def _results(self):
        return [
            self._rej("a1", "ss", DiagnosticStage.TLS, "TLS_TIMEOUT"),
            self._rej("a2", "ss", DiagnosticStage.TLS, "TLS_TIMEOUT"),
            self._rej("a3", "ss", DiagnosticStage.EGRESS, "NO_EGRESS_IP"),
            self._rej("b1", "trojan", DiagnosticStage.TLS, "TLS_SNI_FAIL"),
            Node(node_id="ok1", protocol="ss", stage=Stage.FINAL.value),
        ]

    @staticmethod
    def _rej(nid, proto, dstage, sub):
        return Node(node_id=nid, protocol=proto, stage=Stage.REJECTED.value,
                    failure_reason=FailureReason.TLS_FAIL.value,
                    failure_stage=dstage.value,
                    failure_detail={"sub_reason": sub})

    def test_reject_summary_detailed(self):
        s = pipeline._build_reject_summary_detailed(self._results())
        self.assertEqual(s["TLS_TIMEOUT"], 2)
        self.assertEqual(s["NO_EGRESS_IP"], 1)
        self.assertEqual(s["TLS_SNI_FAIL"], 1)

    def test_reject_summary_by_protocol(self):
        s = pipeline._build_reject_summary_by_protocol(self._results())
        self.assertEqual(s["ss"]["total"], 4)
        self.assertEqual(s["ss"]["failed"], 3)
        self.assertEqual(s["ss"]["TLS_TIMEOUT"], 2)
        self.assertEqual(s["trojan"]["total"], 1)
        self.assertEqual(s["trojan"]["failed"], 1)
        self.assertEqual(s["trojan"]["TLS_SNI_FAIL"], 1)

    def test_stage_summary(self):
        s = pipeline._build_stage_summary(self._results())
        self.assertEqual(s["TLS"], 3)
        self.assertEqual(s["EGRESS"], 1)

    def test_top_level_reason_preserved(self):
        """V5.2 §16 顶层枚举不受影响（准入/优先级语义不变）。"""
        s = pipeline._build_reject_summary(self._results())
        self.assertEqual(s[FailureReason.TLS_FAIL.value], 4)


class TestSourceHealth(unittest.TestCase):
    """P0-05：源健康统计。"""

    def test_written_and_fields(self):
        cfg = make_cfg("d_sh")
        poolstore.ensure_dirs(cfg)
        nodes = [Node(node_id=f"n{i}", source=SOURCE_A) for i in range(3)] + \
                [Node(node_id="n3", source=SOURCE_B)]
        final_nodes = [nodes[0]]
        sh_path = cfg.sub("state/source_health.json")
        from source_health import SourceHealth
        sh = SourceHealth(sh_path)
        data = sh.update_round(
            [SOURCE_A, SOURCE_B, SOURCE_DEAD],
            {SOURCE_A, SOURCE_B},
            {SOURCE_A: 3, SOURCE_B: 1},
            {SOURCE_A: 3, SOURCE_B: 1},
            {SOURCE_A: 1},
            "r1")
        rec = data[SOURCE_A]
        self.assertTrue(rec["fetch"])
        self.assertEqual(rec["parsed"], 3)
        self.assertEqual(rec["valid"], 3)
        self.assertEqual(rec["alive"], 1)
        self.assertAlmostEqual(rec["alive_rate"], 1 / 3, places=3)
        self.assertTrue(data[SOURCE_DEAD]["fetch"] is False)

    def test_pipeline_writes_source_health(self):
        cfg = make_cfg("d_sh_pipe")
        cfg.node_source_urls = [SOURCE_A, SOURCE_B, SOURCE_DEAD]
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        self.assertIn("source_health", s)
        sh = s["source_health"]
        self.assertEqual(sum(r["parsed"] for r in sh.values()), 5)
        # 配置的全部来源都在统计里（含抓取失败的，fetch=false）
        for url in (SOURCE_A, SOURCE_B, SOURCE_DEAD):
            self.assertIn(url, sh)


class TestDetectLog(unittest.TestCase):
    """P0-03：分阶段检测日志。"""

    def test_detect_log_records_stages(self):
        cfg = make_cfg("d_detect")
        os.makedirs(cfg.sub("logs"), exist_ok=True)
        dt = logging.getLogger("pool.detect")
        for h in list(dt.handlers):
            dt.removeHandler(h)
        dt.setLevel(logging.INFO)
        dt.propagate = False
        log_path = cfg.sub("logs/detect.log")
        dt.addHandler(logging.FileHandler(log_path, encoding="utf-8"))

        def probe(url):
            if "ip-api.com" in url:
                return ProbeResult(200, url, "application/json",
                                   '{"query":"203.0.113.9","countryCode":"US"}')
            raise ProbeError(FailureReason.TLS_FAIL,
                             sub=SubReason.TLS_TIMEOUT, detail={})
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(site, probe_fn=probe))
        text = open(log_path, encoding="utf-8").read()
        self.assertIn("PROXY START", text)
        self.assertIn("PROXY END OK", text)
        self.assertIn("EGRESS START", text)
        self.assertIn("EGRESS END OK", text)
        self.assertIn("CHATGPT START", text)
        self.assertIn("CHATGPT END FAIL", text)


class TestPipelineDiagnosticFields(unittest.TestCase):
    """P0-01：节点失败三要素被真实写入；FINAL 节点不携带。"""

    def test_mixed_failures_set_stage_and_sub(self):
        cfg = make_cfg("d_ndiag")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "EMPTY_POOL")
        self.assertEqual(s["reject_summary_detailed"]["TLS_TIMEOUT"], 3)
        self.assertEqual(s["stage_summary"]["TLS"], 3)
        # 顶层原因仍为 TLS_FAIL（V5.2 权威）
        self.assertEqual(s["reject_summary"][FailureReason.TLS_FAIL.value], 3)

    def test_final_node_has_no_diagnostic(self):
        cfg = make_cfg("d_finalclean")
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        nodes = poolstore.read_current_final(cfg)["nodes"]
        self.assertTrue(nodes)
        for n in nodes:
            self.assertEqual(n["stage"], "FINAL")
            self.assertEqual(n["failure_reason"], "")
            self.assertEqual(n["failure_stage"], "")
            self.assertEqual(n["failure_detail"], {})


class TestStatusEnhanced(unittest.TestCase):
    """P1-02：status 增 last_refresh/last_success/last_failure_reason/current_pool_age。"""

    def _svc(self, name):
        cfg = make_cfg(name)
        os.makedirs(cfg.sub("run/prod"), exist_ok=True)
        svc = mihomo_svc.ProductionMihomo(cfg)
        mock.patch.object(svc, "_mihomo_version", return_value="v1.19.31").start()
        self.addCleanup(mock.patch.stopall)
        return cfg, svc

    def test_status_refresh_fields(self):
        cfg, svc = self._svc("d_status")
        poolstore.write_refresh_state(cfg, {
            "last_refresh": "2026-10-07T10:00:00",
            "last_round_id": "20261007100000",
            "published": False,
            "last_success": "",
            "last_failure_reason": "EMPTY_POOL",
        })
        st = svc.status()
        self.assertEqual(st["last_refresh"], "2026-10-07T10:00:00")
        self.assertEqual(st["last_failure_reason"], "EMPTY_POOL")
        self.assertIsNone(st["last_success"])
        self.assertIsNone(st["current_pool_age"])  # 无 current.json

    def test_status_pool_age(self):
        cfg, svc = self._svc("d_age")
        n = Node(node_id="ok1", protocol="ss", stage=Stage.FINAL.value)
        poolstore.write_current_final(cfg, "20261007000001", [n])
        st = svc.status()
        self.assertEqual(st["active_pool"]["count"], 1)
        self.assertIsNotNone(st["current_pool_age"])
        self.assertGreaterEqual(st["current_pool_age"], 0)


class TestRefreshState(unittest.TestCase):
    """P1-02：refresh 后 state/refresh_state.json 更新。"""

    def test_success_writes_state(self):
        cfg = make_cfg("d_rs_ok")
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(site))
        st = poolstore.read_refresh_state(cfg)
        self.assertTrue(st["published"])
        self.assertEqual(st["last_success"], st["last_round_id"])
        self.assertEqual(st["last_failure_reason"], "")

    def test_failure_writes_state(self):
        cfg = make_cfg("d_rs_fail")
        with LocalMockSite(strict_success_routes()) as site:
            pipeline.run_refresh(cfg, inject=_inject(
                site, probe_fn=_mixed_fail_probe(site)))
        st = poolstore.read_refresh_state(cfg)
        self.assertFalse(st["published"])
        self.assertEqual(st["last_failure_reason"], "EMPTY_POOL")


if __name__ == "__main__":
    unittest.main()
