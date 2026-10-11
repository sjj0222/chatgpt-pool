"""Regression tests for strict public egress-IP validation."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import egress  # noqa: E402
from helpers import make_cfg  # noqa: E402
from models import FailureReason, SubReason  # noqa: E402
from probe import ProbeResult  # noqa: E402


class TestPublicEgressIP(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg("egress_public_ip")

    def check_ip(self, ip):
        def probe(url):
            body = json.dumps({"ip": ip, "country_code": "US", "success": True})
            return ProbeResult(200, url, "application/json", body)
        return egress.get_egress(probe, self.cfg)

    def test_accepts_public_ipv4(self):
        self.assertEqual(self.check_ip("8.8.8.8"), ("8.8.8.8", "US", None, None, None))

    def test_accepts_public_ipv6(self):
        self.assertEqual(
            self.check_ip("2001:4860:4860::8888"),
            ("2001:4860:4860::8888", "US", None, None, None))

    def test_rejects_non_public_ranges(self):
        blocked = [
            "10.0.0.1",             # private
            "127.0.0.1",            # loopback
            "169.254.1.1",          # link-local
            "192.0.2.1",            # documentation
            "198.51.100.8",         # documentation
            "203.0.113.9",          # documentation
            "100.64.0.1",           # shared address space
            "224.0.0.1",            # multicast
            "0.0.0.0",              # unspecified
            "240.0.0.1",            # reserved
            "::1",                  # IPv6 loopback
            "fc00::1",              # IPv6 unique-local
            "fe80::1",              # IPv6 link-local
            "2001:db8::1",          # IPv6 documentation
            "ff02::1",              # IPv6 multicast
        ]
        for ip in blocked:
            with self.subTest(ip=ip):
                result = self.check_ip(ip)
                self.assertIsNone(result[0])
                self.assertIsNone(result[1])
                self.assertEqual(result[2], FailureReason.NO_EGRESS_IP)
                self.assertEqual(result[3], SubReason.NO_EGRESS_IP)
                self.assertEqual(result[4]["response_kind"], "non_public_ip")

    def test_invalid_ip_keeps_invalid_ip_diagnostic(self):
        result = self.check_ip("not-an-ip")
        self.assertIsNone(result[0])
        self.assertEqual(result[4]["response_kind"], "invalid_ip")


if __name__ == "__main__":
    unittest.main()
