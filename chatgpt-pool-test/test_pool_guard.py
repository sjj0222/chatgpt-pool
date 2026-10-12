"""场景 H/I/J + §27-§30 / P1-05 / P1-12 / P1-13：旧池保护、首次运行、原子替换。"""
import base64
import json
import os
import unittest

import pipeline
from helpers import (LocalMockSite, chatgpt_probe, fake_country_resolver,
                     fake_engine_factory, make_cfg, strict_success_routes,
                     write_old_pool)

SS_ONE = ("ss://" + base64.b64encode(b"aes-256-gcm:pass1").decode()
          + "@10.0.0.1:8388#US")
SS_TWO = ("ss://" + base64.b64encode(b"aes-256-gcm:pass2").decode()
          + "@10.0.0.2:8388#JP")
SOURCE = SS_ONE + "\n" + SS_TWO + "\n"
SOURCE_CN = ("ss://" + base64.b64encode(b"aes-256-gcm:pass9").decode()
             + "@10.0.0.9:8388#CN") + "\n"


def _inject(site, country=None, engine_ok=True, geoip_body=None):
    return {
        "fetchers": lambda: [("mock", SOURCE)],
        "country_resolver": fake_country_resolver(country or ["US", "JP"]),
        "engine_factory": fake_engine_factory(start_ok=engine_ok),
        "probe_factory": lambda port: chatgpt_probe(site, geoip_body),
        "alloc_port": lambda: 19100,
    }


def _unavailable_inject():
    return {
        "fetchers": lambda: [("mock", SOURCE)],
        "country_resolver": fake_country_resolver(None,
                                                  warning="COUNTRY_LIST_UNAVAILABLE"),
        "engine_factory": fake_engine_factory(),
        "probe_factory": lambda port: chatgpt_probe(LocalMockSite({})),
        "alloc_port": lambda: 19100,
    }


class TestPoolGuard(unittest.TestCase):

    def test_country_unavailable_keeps_old_pool(self):
        cfg = make_cfg("guard_unavail")
        old = write_old_pool(cfg)
        old_text = open(old).read()
        s = pipeline.run_refresh(cfg, inject=_unavailable_inject())
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "COUNTRY_LIST_UNAVAILABLE")
        self.assertEqual(open(cfg.pool_path).read(), old_text)  # §28：旧池原样保留

    def test_first_run_country_unavailable_no_empty_yaml(self):
        cfg = make_cfg("guard_bootstrap")
        s = pipeline.run_refresh(cfg, inject=_unavailable_inject())
        self.assertTrue(s["bootstrap_failed"])
        self.assertFalse(os.path.exists(cfg.pool_path))  # §29：不生成空 YAML

    def test_all_nodes_fail_keeps_old_pool(self):
        cfg = make_cfg("guard_allfail")
        old = write_old_pool(cfg)
        old_text = open(old).read()
        with LocalMockSite(strict_success_routes()) as site:
            inj = _inject(site, engine_ok=False)
            s = pipeline.run_refresh(cfg, inject=inj)
        self.assertFalse(s["published"])
        self.assertEqual(s["reason"], "EMPTY_POOL")
        self.assertEqual(open(cfg.pool_path).read(), old_text)  # §30：空池绝不覆盖旧池

    def test_success_atomic_replace(self):
        cfg = make_cfg("guard_success")
        write_old_pool(cfg)
        with LocalMockSite(strict_success_routes()) as site:
            s = pipeline.run_refresh(cfg, inject=_inject(site))
        self.assertTrue(s["published"])
        self.assertEqual(s["new_pool_count"], 2)
        text = open(cfg.pool_path).read()
        self.assertIn("10.0.0.1", text)
        self.assertIn("10.0.0.2", text)
        self.assertFalse(os.path.exists(cfg.tmp_pool_path))  # 原子替换完成无残留
        # results 保留（§24）
        results_dir = cfg.sub("results")
        files = [f for f in os.listdir(results_dir) if f.endswith(".jsonl")]
        self.assertTrue(files)
        with open(os.path.join(results_dir, files[-1])) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        fin = [r for r in rows if r["stage"] == "FINAL"]
        self.assertEqual(len(fin), 2)
        self.assertTrue(all(r["chatgpt_status"] == "STRICT_PASS" for r in fin))
        self.assertTrue(all(not r["failure_reason"] for r in fin))  # §21：FINAL 无失败原因
        # raw 保留（§24）
        self.assertTrue(any("raw" in d for d in os.listdir(cfg.base_dir)))

    def test_unsupported_country_rejected_and_preserved(self):
        cfg = make_cfg("guard_country")
        # 公网 IP（V0.4 EGRESS 校验拒绝非公网/保留地址，203.0.113.x 会先触发 NO_EGRESS_IP）
        geo = json.dumps({"query": "8.8.8.8", "countryCode": "CN"})
        with LocalMockSite(strict_success_routes()) as site:
            inj = {
                "fetchers": lambda: [("mock", SOURCE_CN)],
                "country_resolver": fake_country_resolver(["US", "JP"]),
                "engine_factory": fake_engine_factory(),
                "probe_factory": lambda port: chatgpt_probe(site, geo),
                "alloc_port": lambda: 19100,
            }
            s = pipeline.run_refresh(cfg, inject=inj)
        self.assertEqual(s["new_pool_count"], 0)
        self.assertEqual(s["reason"], "EMPTY_POOL")
        results_dir = cfg.sub("results")
        files = [f for f in os.listdir(results_dir) if f.endswith(".jsonl")]
        with open(os.path.join(results_dir, files[-1])) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(rows[0]["stage"], "REJECTED")          # §21：REJECTED
        self.assertEqual(rows[0]["failure_reason"], "UNSUPPORTED_COUNTRY")  # §18：节点级
        self.assertFalse(os.path.exists(cfg.pool_path))         # 首次运行失败不生成空池


if __name__ == "__main__":
    unittest.main()
