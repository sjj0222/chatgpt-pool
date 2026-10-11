"""端到端：场景 A 全流程（获取→解析→去重→引擎→出口IP→国家→严格检测→
稳定性→FINAL→原子替换发布），全部 Mock，不产生真实网络与进程。"""
import base64
import json
import os
import unittest

import pipeline
from helpers import (LocalMockSite, chatgpt_probe, fake_country_resolver,
                     fake_engine_factory, make_cfg, strict_success_routes)

SS = ("ss://" + base64.b64encode(b"aes-256-gcm:pass1").decode()
      + "@10.0.0.1:8388#US")
VMESS = ("vmess://" + base64.b64encode(json.dumps({
    "v": "2", "ps": "JP-1", "add": "10.0.0.2", "port": "443",
    "id": "uuid-x1", "aid": "0", "net": "tcp", "type": "none",
    "tls": "tls"}).encode()).decode())


class TestPipelineE2E(unittest.TestCase):

    def test_scenario_a_full_flow(self):
        cfg = make_cfg("e2e_a")
        source = SS + "\n" + VMESS + "\n" + SS + "\n"  # 含重复行
        with LocalMockSite(strict_success_routes()) as site:
            inject = {
                "fetchers": lambda: [("mock", source)],
                "country_resolver": fake_country_resolver(["US", "JP"]),
                "engine_factory": fake_engine_factory(),
                "probe_factory": lambda port: chatgpt_probe(site),
                "alloc_port": lambda: 19123,
            }
            s = pipeline.run_refresh(cfg, inject=inject)

        self.assertTrue(s["published"], s)
        self.assertEqual(s["new_pool_count"], 2)   # 去重后 2 个节点全部 FINAL
        self.assertEqual(s["dupes"], 1)            # 重复行被去重（§23）
        with open(cfg.pool_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("proxies:", text)
        self.assertIn("10.0.0.1", text)
        self.assertIn("10.0.0.2", text)
        # 各阶段产物齐全（§24）
        for sub in ("raw", "normalized", "results", "final", "state"):
            self.assertTrue(os.path.isdir(cfg.sub(sub)), sub)
        raw_files = os.listdir(cfg.sub("raw"))
        self.assertTrue(raw_files)
        with open(os.path.join(cfg.sub("raw"), raw_files[-1])) as f:
            self.assertIn("source: mock", f.read())
        # results 中 FINAL + STRICT_PASS + stability_pass（§19/P1-11）
        res_files = sorted(os.listdir(cfg.sub("results")))
        with open(os.path.join(cfg.sub("results"), res_files[-1])) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(all(r["stage"] == "FINAL" for r in rows))
        self.assertTrue(all(r["chatgpt_status"] == "STRICT_PASS" for r in rows))
        self.assertTrue(all(r["stability_pass"] for r in rows))
        self.assertTrue(all(r["stability_results"] == [True, True, True]
                            for r in rows))
        # 最终池与 chatgpt.yaml 一致（V0.2.1 P0-02 / V0.2.2.1：run/final/current.json）
        with open(os.path.join(cfg.sub("run/final"), "current.json")) as f:
            current = json.load(f)
        self.assertEqual(len(current["nodes"]), 2)
        self.assertTrue(current["round_id"])
        self.assertTrue(os.path.isfile(cfg.sub("run/final/current.json")))
        # 成功轮次不落 failed/（目录为 ensure_dirs 预建，断言无摘要与明细）
        self.assertEqual([f for f in os.listdir(cfg.sub("run/failed"))
                          if f.endswith(".json")], [])
        self.assertEqual(os.listdir(cfg.sub("run/failed/nodes")), [])
        # 发布门（P1-13）：published=True 且旧池文件被原子替换（本次无旧池）
        self.assertFalse(os.path.exists(cfg.tmp_pool_path))


if __name__ == "__main__":
    unittest.main()
