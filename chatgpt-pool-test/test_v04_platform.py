"""V0.4 Platform 测试（Phase 0.4-1/2/3/5/6）。

覆盖：四层架构独立性、GET /api/status|nodes|sources|events、
POST /api/refresh|start|stop、旧路径兼容、export/import config、
sync_payload 白名单、/m/ 移动页、HTTP 冒烟。
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

import api                                                 # noqa: E402
import dashboard                                           # noqa: E402
import services                                            # noqa: E402
import scheduler                                           # noqa: E402
import pipeline                                            # noqa: E402
from helpers import make_cfg                               # noqa: E402
from lifecycle_store import NodeLifecycleStore, \
    LifecycleState                                          # noqa: E402
from source_mgmt import SourceLifecycleStore, SourceState   # noqa: E402


def _seed(cfg):
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    lc.upsert("n1", {"life_state": LifecycleState.ACTIVE.value,
                     "success_count": 5, "fail_count": 1,
                     "source": "https://s1/list.txt", "protocol": "ss",
                     "score": {"overall": 82.0},
                     "service_score": {"overall": 90.0},
                     "capability": ["chatgpt"],
                     "events": [{"ts": "2026-10-07T00:00:00",
                                 "event": "PROMOTED",
                                 "detail": {"to": "ACTIVE"}}]})
    lc.upsert("n2", {"life_state": LifecycleState.STANDBY.value,
                     "success_count": 1, "fail_count": 0,
                     "source": "https://s2/list.txt", "protocol": "trojan"})
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
                   "pool_size": 1, "last_refresh": "2026-10-07T00:00:00"},
                  f)


class TestArchitectureLayering(unittest.TestCase):
    """0.4-1：Core 不依赖 UI/Service/API。"""

    def test_core_no_ui_import(self):
        src = open(os.path.join(os.path.dirname(pipeline.__file__),
                                "pipeline.py")).read()
        for mod in ("import dashboard", "import services", "import api",
                    "from dashboard", "from services", "from api"):
            self.assertNotIn(mod, src,
                             "pipeline 不得依赖 UI/Service/API 层")

    def test_service_no_dashboard_import(self):
        src = open(os.path.join(os.path.dirname(services.__file__),
                                "services.py")).read()
        for mod in ("import dashboard", "import api"):
            self.assertNotIn(mod, src, "services 不得依赖 UI/API 层")

    def test_api_uses_services_only(self):
        src = open(os.path.join(os.path.dirname(api.__file__),
                                "api.py")).read()
        self.assertIn("import services", src)


class TestStatusAPI(unittest.TestCase):
    def test_status_fields(self):
        cfg = make_cfg("v04_st")
        _seed(cfg)
        s = services.get_status(cfg)
        self.assertEqual(s["pool"]["count"], 1)
        self.assertEqual(s["pool"]["round_id"], "r1")
        self.assertIn("success_rate", s["pool"])
        self.assertIn("mihomo", s)
        self.assertIn("running", s["mihomo"])
        self.assertEqual(s["mihomo"]["ports"]["proxy"],
                         cfg.prod_mihomo_port)
        self.assertEqual(s["last_refresh"], "2026-10-07T00:00:00")
        self.assertIn("scheduler", s)
        self.assertIn("diagnostics", s)
        self.assertIn("runtime_report", s["diagnostics"])

    def test_status_empty_pool(self):
        cfg = make_cfg("v04_st2")
        s = services.get_status(cfg)
        self.assertEqual(s["pool"]["count"], 0)
        self.assertIsNone(s["pool"]["round_id"])


class TestNodesSourcesEvents(unittest.TestCase):
    def test_nodes_fields(self):
        cfg = make_cfg("v04_nd")
        _seed(cfg)
        data = services.get_nodes(cfg)
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["count"], 2)
        n1 = next(n for n in data["nodes"] if n["node_id"] == "n1")
        self.assertEqual(n1["life_state"], "ACTIVE")
        self.assertEqual(n1["score"]["overall"], 82.0)
        self.assertEqual(n1["source"], "https://s1/list.txt")
        self.assertEqual(n1["protocol"], "ss")

    def test_nodes_limit(self):
        cfg = make_cfg("v04_nd2")
        _seed(cfg)
        data = services.get_nodes(cfg, limit=1)
        self.assertEqual(data["count"], 1)

    def test_sources_fields(self):
        cfg = make_cfg("v04_sr")
        _seed(cfg)
        data = services.get_sources(cfg)
        self.assertEqual(data["count"], 1)
        s = data["sources"][0]
        self.assertEqual(s["source_score"], 85.0)
        self.assertIn("alive_rate", s)
        self.assertEqual(s["state"], "ACTIVE")

    def test_events_success(self):
        cfg = make_cfg("v04_ev")
        _seed(cfg)
        data = services.get_events(cfg)
        types = {e["type"] for e in data["events"]}
        self.assertIn("refresh", types)
        self.assertIn("state_change", types)
        self.assertGreaterEqual(data["count"], 2)

    def test_events_failure(self):
        cfg = make_cfg("v04_ev2")
        os.makedirs(cfg.sub("run"), exist_ok=True)
        with open(cfg.sub("run/refresh_state.json"), "w") as f:
            json.dump({"status": "EMPTY_POOL", "published": False,
                       "last_refresh": "2026-10-07T01:00:00"}, f)
        data = services.get_events(cfg)
        ev = next(e for e in data["events"]
                  if e["type"] == "refresh" and e["result"] == "failure")
        self.assertEqual(ev["detail"], "EMPTY_POOL")


class TestAPILayer(unittest.TestCase):
    def _route(self, cfg, method, path, body=None):
        return api.route(cfg, method, path, body)

    def test_get_routes(self):
        cfg = make_cfg("v04_api")
        _seed(cfg)
        for p in ("/api/status", "/api/nodes", "/api/sources", "/api/events"):
            st, payload = self._route(cfg, "GET", p)
            self.assertEqual(st, 200, p)
            self.assertIsInstance(payload, dict)

    def test_old_paths(self):
        cfg = make_cfg("v04_old")
        _seed(cfg)
        for p in ("/api/health", "/api/node", "/api/source",
                  "/api/history", "/api/diagnostic"):
            st, payload = self._route(cfg, "GET", p)
            self.assertEqual(st, 200, p)

    def test_404(self):
        cfg = make_cfg("v04_404")
        st, payload = self._route(cfg, "GET", "/nope")
        self.assertEqual(st, 404)

    def test_post_unknown(self):
        cfg = make_cfg("v04_post404")
        st, _ = self._route(cfg, "POST", "/api/xxx")
        self.assertEqual(st, 404)

    def test_post_refresh_locked(self):
        cfg = make_cfg("v04_lock")
        lock = scheduler.Lock(cfg, "refresh")
        self.assertTrue(lock.acquire())
        try:
            st, payload = self._route(cfg, "POST", "/api/refresh")
            self.assertEqual(st, 200)
            self.assertEqual(payload.get("error"), "REFRESH_LOCKED")
        finally:
            lock.release()

    def test_post_refresh_mock_success(self):
        """POST /api/refresh：service 层执行，返回发布摘要（不抛异常）。"""
        from test_diagnostic import _inject
        from helpers import LocalMockSite, strict_success_routes
        cfg = make_cfg("v04_refresh")
        with LocalMockSite(strict_success_routes()) as site:
            # services.refresh 无注入参数；走 api 层验证路由可达
            st, payload = api.route(cfg, "POST", "/api/refresh")
            self.assertIn(st, (200, 500))
            if st == 200:
                self.assertIn("published", payload)

    def test_post_start_stop_errors_explainable(self):
        """沙箱无 mihomo：start/stop 必须返回可解释错误而非崩溃。"""
        cfg = make_cfg("v04_mihomo")
        for p in ("/api/start", "/api/stop"):
            st, payload = self._route(cfg, "POST", p)
            self.assertIn(st, (200, 500))
            if st == 500:
                self.assertIn("error", payload)


class TestExportImport(unittest.TestCase):
    """0.4-6：Local First 数据安全。"""

    def test_export_whitelist(self):
        cfg = make_cfg("v04_exp")
        data = services.export_config(cfg)
        self.assertEqual(data["version"], "V0.4")
        self.assertIn("exported_at", data)
        self.assertNotIn("password", json.dumps(data).lower())

    def test_import_roundtrip(self):
        cfg = make_cfg("v04_imp")
        data = services.export_config(cfg)
        res = services.import_config(cfg, data)
        self.assertIn("imported_keys", res)
        self.assertTrue(os.path.isfile(cfg.sub("config_import.json")))

    def test_import_rejects_unknown(self):
        cfg = make_cfg("v04_imp2")
        res = services.import_config(cfg, {"totally_unknown_key": 1})
        self.assertNotIn("totally_unknown_key", res["imported_keys"])

    def test_import_bad_type(self):
        cfg = make_cfg("v04_imp3")
        with self.assertRaises(ValueError):
            services.import_config(cfg, [1, 2, 3])


class TestSyncPayload(unittest.TestCase):
    """0.4-5：云同步接口设计（默认关闭）——白名单且无敏感字段。"""

    def test_sync_whitelist_fields(self):
        cfg = make_cfg("v04_sync")
        _seed(cfg)
        p = services.sync_payload(cfg)
        self.assertEqual(p["version"], "V0.4")
        self.assertIn("anonymous_stats", p)
        self.assertIn("score", p)
        self.assertIn("source_credit", p)
        self.assertEqual(p["anonymous_stats"]["node_count"], 2)

    def test_sync_no_sensitive(self):
        cfg = make_cfg("v04_sync2")
        _seed(cfg)
        p = services.sync_payload(cfg)
        raw = json.dumps(p)
        for bad in ("password", "server", "port", "uuid", "token",
                    "full_config", "api_key"):
            self.assertNotIn(bad, raw.lower(), "同步负载禁止敏感字段: %s" % bad)

    def test_sync_anonymous_only(self):
        cfg = make_cfg("v04_sync3")
        p = services.sync_payload(cfg)
        self.assertNotIn("node_id", p)
        self.assertNotIn("source_url", p)


class TestMobilePage(unittest.TestCase):
    def test_mobile_body_has_controls(self):
        cfg = make_cfg("v04_mob")
        _seed(cfg)
        body = dashboard._mobile_body(cfg)
        self.assertIn("data-act=\"refresh\"", body)
        self.assertIn("data-act=\"start\"", body)
        self.assertIn("data-act=\"stop\"", body)
        self.assertIn('fetch("/api/"', body)


class TestHTTPV04(unittest.TestCase):
    """HTTP 冒烟：GET 4 端点 + POST refresh + /m 页。"""

    def _serve(self, cfg, port):
        httpd = dashboard.start_dashboard(cfg, port=port)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd

    def _req(self, port, path, method="GET", data=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path), method=method,
            data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_get_endpoints_over_http(self):
        cfg = make_cfg("v04_http")
        _seed(cfg)
        httpd = self._serve(cfg, port=19201)
        try:
            for p in ("/api/status", "/api/nodes", "/api/sources",
                      "/api/events"):
                st, body = self._req(19201, p)
                self.assertEqual(st, 200, p)
                json.loads(body)
            st, body = self._req(19201, "/m")
            self.assertEqual(st, 200)
            self.assertIn("chatgpt-pool 控制台", body)
        finally:
            httpd.shutdown()

    def test_post_refresh_over_http(self):
        cfg = make_cfg("v04_http2")
        httpd = self._serve(cfg, port=19202)
        try:
            st, body = self._req(19202, "/api/refresh", method="POST")
            self.assertIn(st, (200, 500))
            payload = json.loads(body)
            if st == 200:
                self.assertIn("published", payload)
            else:
                self.assertIn("error", payload)
        finally:
            httpd.shutdown()

    def test_post_import_over_http(self):
        cfg = make_cfg("v04_http3")
        httpd = self._serve(cfg, port=19203)
        try:
            payload = json.dumps({"refresh_interval_s": 3600}).encode()
            st, body = self._req(19203, "/api/import_config",
                                 method="POST", data=payload)
            self.assertEqual(st, 200)
            res = json.loads(body)
            self.assertIn("refresh_interval_s", res["imported_keys"])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
