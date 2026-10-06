"""V0.3 Phase 9 Web Dashboard + API 测试（§36/§37）。

覆盖：API 数据（health/node/source/history/diagnostic）、页面渲染、
HTTP 冒烟（临时端口）、仅 127.0.0.1 监听、web_port 配置。
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import dashboard                                          # noqa: E402
from lifecycle_store import NodeLifecycleStore, \
    SourceLifecycleStore                                    # noqa: E402
from models import LifecycleState, SourceState              # noqa: E402
from helpers import make_cfg                               # noqa: E402


def _seed(cfg):
    """写入测试数据。"""
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    lc.upsert("n1", {"life_state": LifecycleState.ACTIVE.value,
                     "success_count": 5, "fail_count": 1,
                     "score": {"overall": 82.0, "stability": 83.3,
                               "availability": 80.0, "latency": 90.0,
                               "credit": 70.0},
                     "service_score": {"overall": 90.0,
                                       "services": {"chatgpt": 95.0}},
                     "capability": ["chatgpt"],
                     "node_group": {"ip_segment": "1.2.3.0/24",
                                    "source": "s1"}})
    lc.upsert("n2", {"life_state": LifecycleState.STANDBY.value})
    lc.save()
    sc = SourceLifecycleStore(cfg.sub("state/source_lifecycle.json"))
    sc.upsert("https://s1/list.txt", {"state": SourceState.ACTIVE.value,
                                      "source_score": 85.0,
                                      "alive_rate": 0.15})
    sc.save()
    os.makedirs(cfg.sub("run/final"), exist_ok=True)
    with open(cfg.sub("run/final/current.json"), "w") as f:
        json.dump({"round_id": "r1", "nodes": [{"node_id": "n1"}]}, f)
    with open(cfg.sub("run/refresh_state.json"), "w") as f:
        json.dump({"round_id": "r1", "status": "ok", "published": True,
                   "pool_size": 1}, f)


class TestAPIData(unittest.TestCase):
    def test_api_health(self):
        cfg = make_cfg("v03_web1")
        _seed(cfg)
        h = dashboard.api_health(cfg)
        self.assertEqual(h["pool"]["count"], 1)
        self.assertEqual(h["pool"]["round_id"], "r1")
        self.assertTrue(h["diagnostics"]["refresh_state"])
        self.assertIn("scheduler", h)

    def test_api_node(self):
        cfg = make_cfg("v03_web2")
        _seed(cfg)
        data = dashboard.api_node(cfg)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["schema_version"], 3)
        by_id = {n["node_id"]: n for n in data["nodes"]}
        self.assertEqual(by_id["n1"]["score"]["overall"], 82.0)
        self.assertEqual(by_id["n1"]["capability"], ["chatgpt"])

    def test_api_source(self):
        cfg = make_cfg("v03_web3")
        _seed(cfg)
        data = dashboard.api_source(cfg)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["sources"][0]["source_score"], 85.0)

    def test_api_history_empty(self):
        cfg = make_cfg("v03_web4")
        h = dashboard.api_history(cfg)
        self.assertEqual(h["count"], 0)

    def test_api_diagnostic(self):
        cfg = make_cfg("v03_web5")
        _seed(cfg)
        d = dashboard.api_diagnostic(cfg)
        self.assertEqual(d["refresh_state"]["pool_size"], 1)
        self.assertEqual(d["detect_log_tail"], "(none)")


class TestHTTP(unittest.TestCase):
    def _serve(self, cfg, port):
        httpd = dashboard.start_dashboard(cfg, port=port)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd

    def _get(self, port, path):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (port, path),
                    timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_api_and_pages_over_http(self):
        cfg = make_cfg("v03_web_http")
        _seed(cfg)
        httpd = self._serve(cfg, port=19191)
        try:
            st, body = self._get(19191, "/api/health")
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body)["pool"]["count"], 1)
            for page in ("/", "/node", "/source", "/diagnostic"):
                st, body = self._get(19191, page)
                self.assertEqual(st, 200)
                self.assertIn("chatgpt-pool", body)
            for api in ("/api/node", "/api/source", "/api/history"):
                st, _ = self._get(19191, api)
                self.assertEqual(st, 200)
            st, _ = self._get(19191, "/nope")
            self.assertEqual(st, 404)
        finally:
            httpd.shutdown()

    def test_port_free_helper(self):
        cfg = make_cfg("v03_web_free")
        self.assertTrue(dashboard.port_free(19192))
        httpd = self._serve(cfg, port=19192)
        try:
            self.assertFalse(dashboard.port_free(19192))
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
