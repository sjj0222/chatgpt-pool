"""V0.4.3 Diagnostic Persistence Final Fix 测试。

任何 refresh 结果（成功/失败/异常/提前退出）都必须有完整诊断记录：
1. ENVIRONMENT_ANOMALY → 五类诊断文件存在
2. BOOTSTRAP_FAILED → 五类诊断文件存在
3. INTERNAL_ERROR（模拟 RuntimeError）→ 异常情况下仍生成诊断文件
4. EMPTY_POOL → 不生成空 yaml + 完整诊断文件
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import pipeline                                              # noqa: E402
from helpers import make_cfg                                 # noqa: E402


def _diag_files(cfg, round_id):
    return {
        "source_health": cfg.sub("run/source_health.json"),
        "detect_log": cfg.sub("run/detect.log"),
        "refresh_state": cfg.sub("run/refresh_state.json"),
        "failed_snapshot": cfg.sub(f"run/failed/{round_id}.json"),
        "nodes_snapshot": cfg.sub(f"run/failed/nodes/{round_id}.jsonl"),
    }


def _assert_all_exist(cfg, round_id):
    paths = _diag_files(cfg, round_id)
    missing = {k: p for k, p in paths.items() if not os.path.isfile(p)}
    return missing


class TestEnvironmentAnomaly(unittest.TestCase):
    """测试1：ENVIRONMENT_ANOMALY → 五类诊断文件存在。"""

    def test_anomaly_writes_all_diag(self):
        cfg = make_cfg("v043_anomaly")
        # 全部节点 PROXY_START_FAIL → 环境异常（无真 mihomo 二进制）
        res = pipeline.run_refresh(cfg, inject={
            "fetchers": [("s1", "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:443#a"),
                         ("s2", "trojan://pass@2.3.4.5:443#b")],
            "country_resolver": lambda c: ({"US"}, "builtin", None)})
        missing = _assert_all_exist(cfg, res["round_id"])
        self.assertFalse(missing, f"missing: {missing}")


class TestBootstrapFailed(unittest.TestCase):
    """测试2：BOOTSTRAP_FAILED → 五类诊断文件存在。"""

    def test_bootstrap_failed_writes_all_diag(self):
        cfg = make_cfg("v043_boot")
        def _boom():
            raise RuntimeError("all sources down")
        res = pipeline.run_refresh(cfg, inject={"fetchers": [("s1", _boom)]})
        self.assertTrue(res.get("bootstrap_failed"))
        missing = _assert_all_exist(cfg, res["round_id"])
        self.assertFalse(missing, f"missing: {missing}")
        # 不生成空 yaml
        self.assertFalse(os.path.isfile(cfg.sub("chatgpt.yaml")))


class TestInternalError(unittest.TestCase):
    """测试3：模拟 RuntimeError → 异常情况下仍生成诊断文件。"""

    def test_exception_writes_diag_then_reraises(self):
        cfg = make_cfg("v043_exc")

        def _bad_resolver(c):
            raise RuntimeError("country list exploded")

        with self.assertRaises(RuntimeError):
            pipeline.run_refresh(cfg, inject={
                "fetchers": [("s1", "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:443#a")],
                "country_resolver": _bad_resolver})
        # 诊断文件仍必须存在
        self.assertTrue(os.path.isfile(cfg.sub("run/source_health.json")))
        self.assertTrue(os.path.isfile(cfg.sub("run/detect.log")))
        self.assertTrue(os.path.isfile(cfg.sub("run/refresh_state.json")))
        with open(cfg.sub("run/refresh_state.json")) as f:
            rs = json.load(f)
        self.assertIn("INTERNAL_ERROR", rs.get("last_failure_reason", ""))


class TestEmptyPool(unittest.TestCase):
    """测试4：EMPTY_POOL → 不生成空 yaml + 完整诊断文件。"""

    def test_empty_pool_no_yaml_but_diag(self):
        cfg = make_cfg("v043_empty")
        res = pipeline.run_refresh(cfg, inject={
            "fetchers": [("s1", "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:443#a")],
            "country_resolver": lambda c: ({"US"}, "builtin", None)})
        self.assertEqual(res["reason"], "EMPTY_POOL")
        # 不生成空 yaml
        self.assertFalse(os.path.isfile(cfg.sub("chatgpt.yaml")))
        # 诊断文件齐全
        missing = _assert_all_exist(cfg, res["round_id"])
        self.assertFalse(missing, f"missing: {missing}")

    def test_detect_log_mentions_round(self):
        """detect.log 含 round_id 与 Detection Result。"""
        cfg = make_cfg("v043_log")
        res = pipeline.run_refresh(cfg, inject={
            "fetchers": [("s1", "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:443#a")],
            "country_resolver": lambda c: ({"US"}, "builtin", None)})
        with open(cfg.sub("run/detect.log")) as f:
            text = f.read()
        self.assertIn(res["round_id"], text)
        self.assertIn("Detection Result", text)


if __name__ == "__main__":
    unittest.main()
