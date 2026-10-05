"""场景 D/E/F 及 §10-§14 / P1-01 / P1-03：ChatGPT 严格检测。"""
import unittest

import chatgpt_check
from helpers import (LocalMockSite, chatgpt_probe, err_probe, make_cfg,
                     strict_success_routes)
from models import ChatgptStatus, FailureReason, Node
from probe import ProbeResult


def run_with_routes(routes, geoip_body=None, probe=None):
    cfg = make_cfg("chatgpt_check")
    with LocalMockSite(routes) as site:
        p = probe or chatgpt_probe(site, geoip_body)
        return chatgpt_check.run_strict_check(Node(), p, cfg)


class TestChatgptCheck(unittest.TestCase):

    def test_scenario_a_strict_pass(self):
        r = run_with_routes(strict_success_routes())
        self.assertEqual(r["status"], ChatgptStatus.STRICT_PASS)
        self.assertTrue(r["strict_pass"])
        self.assertIsNone(r["failure_reason"])
        self.assertEqual(r["detail"]["backend"]["result"], "BACKEND_OK")
        self.assertTrue(r["detail"]["home"]["ok"])
        self.assertTrue(r["detail"]["auth"]["ok"])
        # §14：WebSocket 辅助检测不构成否决
        self.assertTrue(r["detail"]["ws"]["auxiliary"])

    def test_backend_401_does_not_block_strict_via_auth(self):
        routes = strict_success_routes()
        routes["/backend"] = (401, {"Content-Type": "application/json"},
                              b'{"detail":"Unauthorized"}')
        r = run_with_routes(routes)
        # HOME_OK + AUTH_OK → STRICT_PASS；BACKEND 401 仅记录明细（P1-01）
        self.assertEqual(r["status"], ChatgptStatus.STRICT_PASS)
        self.assertEqual(r["detail"]["backend"]["result"],
                         "BACKEND_AUTH_REQUIRED")
        self.assertEqual(r["detail"]["backend"]["kind"], "AUTH_REQUIRED")

    def test_p1_01_backend401_alone_not_strict(self):
        # HOME_OK + AUTH 403 + BACKEND 401 → 只有 NETWORK_PASS
        routes = {
            "/": (200, {"Content-Type": "text/html"},
                  b"<html><title>ChatGPT</title>__NEXT_DATA__</html>"),
            "/auth": (403, {"Content-Type": "text/html"},
                      b"not available in your country"),
            "/backend": (401, {"Content-Type": "application/json"},
                         b'{"detail":"Unauthorized"}'),
        }
        r = run_with_routes(routes)
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)
        self.assertFalse(r["strict_pass"])
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)
        # 401 不得计入 BACKEND_OK（P1-01）
        self.assertFalse(r["detail"]["backend"]["ok"])

    def test_scenario_d_single_403_not_fatal(self):
        # HOME 403 + AUTH 200 + BACKEND 200 → 不判 OPENAI_BLOCKED，继续综合判断
        routes = {
            "/": (403, {"Content-Type": "text/html"},
                  b"not available in your country"),
            "/auth": (200, {"Content-Type": "application/json"},
                      b'{"csrfToken":"x"}'),
            "/backend": (200, {"Content-Type": "application/json"},
                         b'{"ok":true}'),
        }
        r = run_with_routes(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)
        self.assertFalse(r["strict_pass"])  # HOME 不过 → 不严格通过
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)

    def test_scenario_e_all_blocked(self):
        routes = {
            "/": (403, {"Content-Type": "text/html"},
                  b"not available in your country"),
            "/auth": (403, {"Content-Type": "text/html"},
                      b"not available in your country"),
            "/backend": (403, {"Content-Type": "application/json"},
                         b'{"detail":"not available in your country"}'),
        }
        r = run_with_routes(routes)
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)

    def test_transport_tls_fail(self):
        r = run_with_routes({}, probe=err_probe(FailureReason.TLS_FAIL))
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.TLS_FAIL)

    def test_transport_timeout(self):
        r = run_with_routes({}, probe=err_probe(FailureReason.TIMEOUT))
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.TIMEOUT)

    def test_mixed_error_and_response(self):
        # HOME 传输失败 + AUTH/BACKEND 正常 → 有响应 → STRICT_CHECK_FAIL
        def probe(url):
            if "chat.openai.com/" == url or url.endswith("/"):
                raise __import__("probe").ProbeError(FailureReason.TLS_FAIL)
            if "csrf" in url:
                return ProbeResult(200, url, "application/json",
                                   '{"csrfToken":"x"}')
            return ProbeResult(200, url, "application/json", '{"ok":true}')

        r = run_with_routes({}, probe=probe)
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)
        self.assertNotEqual(r["failure_reason"], FailureReason.TLS_FAIL)

    def test_home_feature_required(self):
        # 200 但页面无特征 → 不算 HOME_OK
        routes = {
            "/": (200, {"Content-Type": "text/html"}, b"<html>nothing</html>"),
            "/auth": (200, {"Content-Type": "application/json"},
                      b'{"csrfToken":"x"}'),
            "/backend": (200, {"Content-Type": "application/json"},
                         b'{"ok":true}'),
        }
        r = run_with_routes(routes)
        self.assertFalse(r["detail"]["home"]["ok"])
        self.assertFalse(r["strict_pass"])
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)


if __name__ == "__main__":
    unittest.main()
