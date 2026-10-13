"""V0.4.4 检测准确性修复：403 误分类（地区封锁 / Cloudflare challenge / 普通 403）。

规则三 A-G：
A. 三端点均返回明确地区限制信息 → OPENAI_BLOCKED。
B. 三端点均 Cloudflare challenge 403 → 不得判定 OPENAI_BLOCKED。
C. 单端点 challenge 403 + 另两端正常 → 综合判断，不因单个 403 淘汰。
D. Backend 401 → BACKEND_AUTH_REQUIRED，不得单独严格通过。
E. HOME 未通过 + Backend 401 → 不得 STRICT_PASS。
F. TLS / DNS / TCP / 超时原有分类保持。
G. 稳定性：3 次完整严格检测，≥2 成功才通过。

全部 Mock，不访问真实网络，不写生产池。
"""
import unittest
from unittest import mock

import chatgpt_check
from helpers import err_probe, make_cfg
from models import ChatgptStatus, FailureReason, Node, SubReason
from probe import ProbeError, ProbeResult
from stability import run_stability

HOME_OK = ProbeResult(
    200, "https://chat.openai.com/", "text/html",
    "<html><title>ChatGPT</title><div id=__NEXT_DATA__>auth</div></html>")
AUTH_OK = ProbeResult(
    200, "https://chat.openai.com/api/auth/csrf", "application/json",
    '{"csrfToken":"mock-csrf"}')
BACKEND_OK = ProbeResult(
    200, "https://chat.openai.com/backend-api/me", "application/json",
    '{"ok":true}')
BACKEND_401 = ProbeResult(
    401, "https://chat.openai.com/backend-api/me", "application/json",
    '{"detail":"Unauthorized"}')

CH = {"cf-mitigated": "challenge"}          # Cloudflare 挑战证据头
GEO_BODY = "not available in your country"  # 明确地区限制文本


def _probe(routes):
    def fn(url):
        path = url.split(".com", 1)[-1] or "/"
        if path == "/" or path == "/":
            return routes["home"]
        if "csrf" in path:
            return routes["auth"]
        if "backend" in path:
            return routes["backend"]
        raise ProbeError(FailureReason.DNS_FAIL)
    return fn


def _check(routes):
    return chatgpt_check.run_strict_check(Node(), _probe(routes),
                                          make_cfg("chatgpt_403"))


class TestGeoBlocked(unittest.TestCase):
    """A：三端点均返回明确地区限制信息。"""

    def test_three_endpoints_geo_blocked(self):
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                GEO_BODY),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", GEO_BODY),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "application/json", '{"detail":"%s"}' % GEO_BODY),
        }
        r = _check(routes)
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        self.assertEqual(r["failure_sub"], SubReason.CHATGPT_BLOCKED)
        for ep in ("home", "auth", "backend"):
            self.assertTrue(r["detail"][ep]["blocked"])

    def test_geo_blocked_not_depending_on_403(self):
        """地区封锁证据来自 body 文本而非状态码：200 页面带文本同样判定。"""
        routes = {
            "home": ProbeResult(200, "https://chat.openai.com/", "text/html",
                                "<title>Error</title> %s" % GEO_BODY),
            "auth": ProbeResult(200, "https://chat.openai.com/api/auth/csrf",
                                "text/html", GEO_BODY),
            "backend": ProbeResult(200, "https://chat.openai.com/backend-api/me",
                                   "text/html", GEO_BODY),
        }
        r = _check(routes)
        self.assertEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)


class TestCloudflareChallenge(unittest.TestCase):
    """B：challenge 403 不得误报 OPENAI_BLOCKED。"""

    def test_three_endpoints_challenge_not_blocked(self):
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Just a moment...", headers=CH),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Just a moment...", headers=CH),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Just a moment...", headers=CH),
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        for ep in ("home", "auth", "backend"):
            self.assertTrue(r["detail"][ep]["challenge"])
            self.assertFalse(r["detail"][ep]["blocked"])
            self.assertTrue(r["detail"][ep]["result"].endswith("_CHALLENGE"))
        # 有响应 → 网络可达证据，未严格通过
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)

    def test_challenge_header_case_insensitive(self):
        routes = {
            "home": HOME_OK,
            "auth": AUTH_OK,
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Just a moment...",
                                   headers={"CF-Mitigated": "Challenge"}),
        }
        r = _check(routes)
        self.assertTrue(r["detail"]["backend"]["challenge"])
        self.assertFalse(r["detail"]["backend"]["blocked"])
        self.assertEqual(r["status"], ChatgptStatus.STRICT_PASS)


class TestSingleEndpoint403(unittest.TestCase):
    """C：单端点 403/challenge 不淘汰，综合判断。"""

    def test_backend_challenge_home_auth_ok(self):
        """challenge 落在非关键组合端点：HOME_OK + AUTH_OK → STRICT_PASS。"""
        routes = {
            "home": HOME_OK,
            "auth": AUTH_OK,
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Just a moment...", headers=CH),
        }
        r = _check(routes)
        self.assertEqual(r["status"], ChatgptStatus.STRICT_PASS)
        self.assertTrue(r["strict_pass"])
        self.assertTrue(r["detail"]["backend"]["challenge"])
        self.assertFalse(r["detail"]["backend"]["blocked"])

    def test_home_challenge_auth_backend_ok_not_eliminated(self):
        """challenge 落在 HOME：不淘汰、不 OPENAI_BLOCKED；HOME 未过 → 不严格通过。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Just a moment...", headers=CH),
            "auth": AUTH_OK,
            "backend": BACKEND_OK,
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        self.assertFalse(r["strict_pass"])  # STRICT_PASS 必须 HOME_OK
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)
        self.assertTrue(r["detail"]["home"]["challenge"])

    def test_backend_plain_403_home_auth_ok(self):
        """普通 403（无地区文本、无 challenge）同样不淘汰。"""
        routes = {
            "home": HOME_OK,
            "auth": AUTH_OK,
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Forbidden"),
        }
        r = _check(routes)
        self.assertEqual(r["status"], ChatgptStatus.STRICT_PASS)
        self.assertFalse(r["detail"]["backend"]["blocked"])
        self.assertFalse(r["detail"]["backend"]["challenge"])
        self.assertEqual(r["detail"]["backend"]["result"], "BACKEND_FORBIDDEN")

    def test_all_plain_403_not_blocked(self):
        """三端点普通 403（无地区文本/无 challenge）→ 不 OPENAI_BLOCKED。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Forbidden"),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Forbidden"),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Forbidden"),
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        for ep in ("home", "auth", "backend"):
            self.assertFalse(r["detail"][ep]["blocked"])
            self.assertFalse(r["detail"][ep]["challenge"])
            self.assertEqual(r["detail"][ep]["result"], ep.upper() + "_FORBIDDEN")


class TestBackend401(unittest.TestCase):
    """D/E：Backend 401 仅记录，不得单独严格通过。"""

    def test_backend_401_not_strict_alone(self):
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Forbidden"),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Forbidden"),
            "backend": BACKEND_401,
        }
        r = _check(routes)
        self.assertFalse(r["strict_pass"])
        self.assertNotEqual(r["status"], ChatgptStatus.STRICT_PASS)
        self.assertEqual(r["detail"]["backend"]["result"],
                         "BACKEND_AUTH_REQUIRED")
        self.assertEqual(r["detail"]["backend"]["kind"], "AUTH_REQUIRED")
        self.assertFalse(r["detail"]["backend"]["ok"])  # 401 不计 BACKEND_OK
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)

    def test_home_fail_backend401_no_strict(self):
        routes = {
            "home": ProbeResult(200, "https://chat.openai.com/", "text/html",
                                "<html>nothing here</html>"),  # 无特征 → 未过
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Forbidden"),
            "backend": BACKEND_401,
        }
        r = _check(routes)
        self.assertFalse(r["strict_pass"])
        self.assertEqual(r["detail"]["backend"]["result"],
                         "BACKEND_AUTH_REQUIRED")
        self.assertFalse(r["detail"]["backend"]["ok"])
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)


class TestChallengeVsGeoEvidence(unittest.TestCase):
    """P1：Cloudflare challenge 证据优先于正文文案（Access denied 不判地区封锁）。"""

    def test_three_challenge_with_access_denied_not_blocked(self):
        """三端点 challenge 403 + "Access denied" 正文 → 不误判 OPENAI_BLOCKED。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Access denied", headers=CH),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Access denied", headers=CH),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Access denied", headers=CH),
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        for ep in ("home", "auth", "backend"):
            self.assertTrue(r["detail"][ep]["challenge"])
            self.assertFalse(r["detail"][ep]["blocked"])
            self.assertTrue(r["detail"][ep]["result"].endswith("_CHALLENGE"))
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)
        self.assertEqual(r["failure_reason"], FailureReason.STRICT_CHECK_FAIL)

    def test_single_challenge_access_denied_others_ok(self):
        """单端点 challenge+"Access denied"，另两端正常 → 综合判断（不淘汰）。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Access denied", headers=CH),
            "auth": AUTH_OK,
            "backend": BACKEND_OK,
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        self.assertTrue(r["detail"]["home"]["challenge"])
        self.assertFalse(r["detail"]["home"]["blocked"])
        self.assertEqual(r["status"], ChatgptStatus.NETWORK_PASS)  # HOME 未过
        self.assertFalse(r["strict_pass"])

    def test_access_denied_alone_not_geo_evidence(self):
        """普通 403 + "Access denied"（无 challenge 头）→ 不按地区封锁判。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Access denied"),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Access denied"),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", "Access denied"),
        }
        r = _check(routes)
        self.assertNotEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)
        for ep in ("home", "auth", "backend"):
            self.assertFalse(r["detail"][ep]["blocked"])
            self.assertFalse(r["detail"][ep]["challenge"])
            self.assertEqual(r["detail"][ep]["result"], ep.upper() + "_FORBIDDEN")

    def test_geo_text_still_blocked_without_challenge(self):
        """明确地区限制文本（无 challenge 头）语义保持。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                GEO_BODY),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", GEO_BODY),
            "backend": ProbeResult(403, "https://chat.openai.com/backend-api/me",
                                   "text/html", GEO_BODY),
        }
        r = _check(routes)
        self.assertEqual(r["failure_reason"], FailureReason.OPENAI_BLOCKED)


class TestChallengeUnifiedStatusBranches(unittest.TestCase):
    """P2：challenge 识别在 200/3xx/4xx 分支统一，不得错误严格通过。"""

    def test_200_with_challenge_header_not_ok(self):
        """200 + challenge 头（页面特征齐全）→ 不得判 HOME_OK。"""
        routes = {
            "home": ProbeResult(
                200, "https://chat.openai.com/", "text/html",
                "<html><title>ChatGPT</title><div id=__NEXT_DATA__>auth</div></html>",
                headers=CH),
            "auth": AUTH_OK,
            "backend": BACKEND_OK,
        }
        r = _check(routes)
        self.assertFalse(r["detail"]["home"]["ok"])
        self.assertTrue(r["detail"]["home"]["challenge"])
        self.assertEqual(r["detail"]["home"]["result"], "HOME_CHALLENGE")
        self.assertFalse(r["strict_pass"])  # HOME 未过

    def test_200_auth_with_challenge_not_ok(self):
        routes = {
            "home": HOME_OK,
            "auth": ProbeResult(200, "https://chat.openai.com/api/auth/csrf",
                                "application/json", '{"csrfToken":"x"}',
                                headers=CH),
            "backend": BACKEND_OK,
        }
        r = _check(routes)
        self.assertFalse(r["detail"]["auth"]["ok"])
        self.assertEqual(r["detail"]["auth"]["result"], "AUTH_CHALLENGE")
        # STRICT_PASS = HOME_OK AND (AUTH_OK OR BACKEND_OK)：auth 未过但 backend OK
        self.assertTrue(r["strict_pass"])

    def test_200_backend_with_challenge_not_ok(self):
        routes = {
            "home": HOME_OK,
            "auth": AUTH_OK,
            "backend": ProbeResult(200, "https://chat.openai.com/backend-api/me",
                                   "application/json", '{"ok":true}',
                                   headers=CH),
        }
        r = _check(routes)
        self.assertFalse(r["detail"]["backend"]["ok"])
        self.assertEqual(r["detail"]["backend"]["result"], "BACKEND_CHALLENGE")
        self.assertTrue(r["strict_pass"])  # HOME+AUTH OK 仍可严格通过

    def test_3xx_with_challenge_header(self):
        """3xx + challenge 头 → 不按 2xx 成功条件判定。"""
        routes = {
            "home": ProbeResult(302, "https://chat.openai.com/", "text/html",
                                "<html>redirect</html>", headers=CH),
            "auth": ProbeResult(302, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "<html>redirect</html>",
                                headers=CH),
            "backend": ProbeResult(302, "https://chat.openai.com/backend-api/me",
                                   "text/html", "<html>redirect</html>",
                                   headers=CH),
        }
        r = _check(routes)
        for ep in ("home", "auth", "backend"):
            self.assertFalse(r["detail"][ep]["ok"])
            self.assertTrue(r["detail"][ep]["challenge"])
            self.assertTrue(r["detail"][ep]["result"].endswith("_CHALLENGE"))
        self.assertFalse(r["strict_pass"])

    def test_backend_401_with_challenge_keeps_auth_required(self):
        """Backend 401 + challenge 头 → 仍 BACKEND_AUTH_REQUIRED（401 语义优先）。"""
        routes = {
            "home": ProbeResult(403, "https://chat.openai.com/", "text/html",
                                "Forbidden"),
            "auth": ProbeResult(403, "https://chat.openai.com/api/auth/csrf",
                                "text/html", "Forbidden"),
            "backend": ProbeResult(401, "https://chat.openai.com/backend-api/me",
                                   "application/json", '{"detail":"Unauthorized"}',
                                   headers=CH),
        }
        r = _check(routes)
        self.assertEqual(r["detail"]["backend"]["result"],
                         "BACKEND_AUTH_REQUIRED")
        self.assertEqual(r["detail"]["backend"]["kind"], "AUTH_REQUIRED")
        self.assertFalse(r["strict_pass"])
        self.assertFalse(r["detail"]["backend"]["ok"])


class TestTransportPreserved(unittest.TestCase):
    """F：传输层失败分类保持。"""

    def test_tls(self):
        r = chatgpt_check.run_strict_check(Node(), err_probe(FailureReason.TLS_FAIL),
                                           make_cfg("chatgpt_tls"))
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.TLS_FAIL)

    def test_dns(self):
        r = chatgpt_check.run_strict_check(Node(), err_probe(FailureReason.DNS_FAIL),
                                           make_cfg("chatgpt_dns"))
        self.assertEqual(r["failure_reason"], FailureReason.DNS_FAIL)

    def test_tcp_refused(self):
        r = chatgpt_check.run_strict_check(Node(),
                                           err_probe(FailureReason.PROXY_ERROR),
                                           make_cfg("chatgpt_tcp"))
        self.assertEqual(r["failure_reason"], FailureReason.PROXY_ERROR)

    def test_timeout(self):
        r = chatgpt_check.run_strict_check(Node(), err_probe(FailureReason.TIMEOUT),
                                           make_cfg("chatgpt_timeout"))
        self.assertEqual(r["status"], ChatgptStatus.FAIL)
        self.assertEqual(r["failure_reason"], FailureReason.TIMEOUT)
        self.assertEqual(r["failure_sub"], SubReason.CHATGPT_TIMEOUT)


class TestStabilityIntegration(unittest.TestCase):
    """G：稳定性每次复测执行完整严格检测，≥2/3 通过。"""

    def test_3_runs_full_strict_check_2of3(self):
        cfg = make_cfg("chatgpt_stab")
        seq = [
            # 第 1、2 次：完整 STRICT_PASS
            _probe({"home": HOME_OK, "auth": AUTH_OK, "backend": BACKEND_OK}),
            _probe({"home": HOME_OK, "auth": AUTH_OK, "backend": BACKEND_OK}),
            # 第 3 次：home challenge → 非 STRICT_PASS
            _probe({"home": ProbeResult(403, "https://chat.openai.com/",
                                        "text/html", "Just a moment...",
                                        headers=CH),
                    "auth": AUTH_OK, "backend": BACKEND_OK}),
        ]
        def check_fn():
            i = check_fn.i
            check_fn.i += 1
            return chatgpt_check.run_strict_check(Node(), seq[i], cfg)
        check_fn.i = 0
        results, ok = run_stability(check_fn, cfg)
        self.assertEqual(results, [True, True, False])
        self.assertTrue(ok)  # 2/3 通过

    def test_1of3_fails(self):
        cfg = make_cfg("chatgpt_stab1")
        seq = [
            _probe({"home": HOME_OK, "auth": AUTH_OK, "backend": BACKEND_OK}),
            _probe({"home": ProbeResult(403, "https://chat.openai.com/",
                                        "text/html", "Just a moment...",
                                        headers=CH),
                    "auth": AUTH_OK, "backend": BACKEND_OK}),
            _probe({"home": ProbeResult(403, "https://chat.openai.com/",
                                        "text/html", "Just a moment...",
                                        headers=CH),
                    "auth": AUTH_OK, "backend": BACKEND_OK}),
        ]
        def check_fn():
            i = check_fn.i
            check_fn.i += 1
            return chatgpt_check.run_strict_check(Node(), seq[i], cfg)
        check_fn.i = 0
        results, ok = run_stability(check_fn, cfg)
        self.assertEqual(results, [True, False, False])
        self.assertFalse(ok)  # 1/3 → 不通过


if __name__ == "__main__":
    unittest.main()
