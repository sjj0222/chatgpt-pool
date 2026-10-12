"""V0.4.4：国家代码有效性校验 Mock 测试（fix/geoip-ipwhois）。

覆盖：ISO 3166-1 alpha-2 白名单、在线列表逐项验证、缓存脏数据拒绝、
三级来源降级、系统级失败语义。全部 mock，不访问真实网络，
不写入生产池数据（make_cfg 用临时测试目录）。
"""
import json
import os
import unittest
from unittest import mock

import countries
from helpers import make_cfg


class _R:
    def __init__(self, status, body):
        self.status = status
        self.body = body


def _fake_probe(body):
    """返回一个固定响应体的 probe 工厂。"""
    def factory(_cfg):
        def probe(_url):
            return _R(200, body)
        return probe
    return factory


class TestIsoSet(unittest.TestCase):
    """ISO 3166-1 alpha-2 白名单本身。"""

    def test_set_is_complete_and_unique(self):
        self.assertEqual(len(countries.ISO3166_ALPHA2), 249)
        self.assertEqual(len(set(countries.ISO3166_ALPHA2)),
                         len(countries.ISO3166_ALPHA2))

    def test_all_codes_two_ascii_uppercase(self):
        for c in countries.ISO3166_ALPHA2:
            self.assertEqual(len(c), 2)
            self.assertTrue(c.isascii() and c.isupper() and c.isalpha())

    def test_builtin_countries_all_valid(self):
        """内置列表 60 项必须全部在白名单内（兼容策略：不删除任何业务代码）。"""
        cfg = make_cfg("country_valid_builtin")
        for c in cfg.builtin_countries:
            self.assertTrue(countries.is_valid_code(c),
                            f"builtin code {c} not in ISO set")
        self.assertEqual(len(cfg.builtin_countries), len(set(cfg.builtin_countries)))

    def test_known_valid_codes(self):
        for c in ("US", "JP", "GB", "CA", "AU", "DE", "CN", "HK", "TW", "MO"):
            self.assertTrue(countries.is_valid_code(c))

    def test_invalid_codes_rejected(self):
        for c in ("ZZ", "XX", "QQ", "AA", "UK", "EU", "USA", "us", "U1", "US ",
                  "", "A", "AAA"):
            self.assertFalse(countries.is_valid_code(c))

    def test_non_string_rejected(self):
        for v in (123, None, True, 2.0, ["US"], {"US"}):
            self.assertFalse(countries.is_valid_code(v))


class TestOnlineValidity(unittest.TestCase):
    """在线列表必须逐项验证，不能仅凭两位大写数量达标就接受。"""

    def test_online_accepts_valid_codes(self):
        cfg = make_cfg("country_ov")
        body = " ".join(sorted(countries.ISO3166_ALPHA2))
        with mock.patch("probe.NoProxyProbe",
                        side_effect=_fake_probe(body)):
            got = countries.fetch_online_countries(cfg)
        self.assertIsNotNone(got)
        self.assertEqual(len(got), 249)
        self.assertTrue(all(countries.is_valid_code(c) for c in got))

    def test_online_filters_noise_codes(self):
        """页面混入 EU/UK 等非 ISO 词：剔除后有效数达标即可接受，噪声不得进入。"""
        cfg = make_cfg("country_noise")
        valid = ["US", "JP", "GB", "CA", "AU", "DE", "FR", "IT", "ES", "NL",
                 "SE", "NO", "DK", "FI", "PL", "IE", "AT", "BE", "CH", "PT",
                 "KR", "SG", "IN"]
        body = " ".join(valid + ["EU", "UK", "ZZ"])
        with mock.patch("probe.NoProxyProbe",
                        side_effect=_fake_probe(body)):
            got = countries.fetch_online_countries(cfg)
        self.assertIsNotNone(got)
        self.assertNotIn("EU", got)
        self.assertNotIn("UK", got)
        self.assertNotIn("ZZ", got)
        self.assertTrue(all(countries.is_valid_code(c) for c in got))

    def test_online_rejects_when_valid_below_min(self):
        """大量无效代码混入：剔除后有效数不足 → 不得当作完全有效接受。"""
        cfg = make_cfg("country_dirty")
        cfg.min_online_country_codes = 20
        body = " ".join(["ZZ"] * 20 + ["US", "JP", "GB", "CA", "AU"])
        with mock.patch("probe.NoProxyProbe",
                        side_effect=_fake_probe(body)):
            self.assertIsNone(countries.fetch_online_countries(cfg))

    def test_online_rejects_few_codes(self):
        cfg = make_cfg("country_few2")
        cfg.min_online_country_codes = 20
        with mock.patch("probe.NoProxyProbe",
                        side_effect=_fake_probe("US JP GB")):
            self.assertIsNone(countries.fetch_online_countries(cfg))

    def test_online_http_error_returns_none(self):
        cfg = make_cfg("country_http_err")
        with mock.patch("probe.NoProxyProbe",
                        side_effect=lambda c: (lambda u: _R(500, ""))):
            self.assertIsNone(countries.fetch_online_countries(cfg))


class TestCacheValidity(unittest.TestCase):
    """缓存必须与在线同一有效性规则；脏数据整份拒绝。"""

    def _write_cache(self, cfg, data):
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_cache_accepts_valid(self):
        cfg = make_cfg("country_cv")
        self._write_cache(cfg, ["US", "JP", "GB"])
        self.assertEqual(countries.load_cache(cfg.country_cache_path),
                         ["US", "JP", "GB"])

    def test_cache_rejects_invalid_code(self):
        cfg = make_cfg("country_ci")
        self._write_cache(cfg, ["US", "ZZ"])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_rejects_duplicates(self):
        cfg = make_cfg("country_cd")
        self._write_cache(cfg, ["US", "US", "JP"])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_rejects_non_string(self):
        cfg = make_cfg("country_cn")
        self._write_cache(cfg, ["US", 123])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))
        self._write_cache(cfg, ["US", None])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_rejects_empty_value(self):
        cfg = make_cfg("country_ce")
        self._write_cache(cfg, ["US", ""])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_rejects_empty_list(self):
        cfg = make_cfg("country_cl")
        self._write_cache(cfg, [])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_missing_returns_none(self):
        cfg = make_cfg("country_cm")
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_cache_corrupt_json_returns_none(self):
        cfg = make_cfg("country_cj")
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))


class TestResolveFallback(unittest.TestCase):
    """三级来源降级 + 系统级失败语义不变。"""

    def test_dirty_cache_falls_to_builtin(self):
        cfg = make_cfg("country_r1")
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            json.dump(["US", "ZZ"], f)
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "builtin")
        self.assertEqual(warn, "COUNTRY_LIST_FALLBACK")
        self.assertTrue(all(countries.is_valid_code(c) for c in got))

    def test_online_unavailable_cache_fallback(self):
        cfg = make_cfg("country_r2")
        countries.save_cache(cfg.country_cache_path, ["US", "JP"])
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(got, ["US", "JP"])
        self.assertEqual(source, "cache")
        self.assertIsNone(warn)

    def test_both_unavailable_builtin_fallback(self):
        cfg = make_cfg("country_r3")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "builtin")
        self.assertEqual(warn, "COUNTRY_LIST_FALLBACK")
        self.assertIsNotNone(got)

    def test_all_unavailable_system_failure(self):
        cfg = make_cfg("country_r4")
        cfg.builtin_countries = []
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            f.write("not json")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertIsNone(got)
        self.assertEqual(warn, "COUNTRY_LIST_UNAVAILABLE")

    def test_online_success_writes_cache(self):
        cfg = make_cfg("country_r5")
        codes = ["US", "JP", "GB"]
        with mock.patch("countries.fetch_online_countries", return_value=codes):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(got, codes)
        self.assertEqual(source, "online")
        self.assertIsNone(warn)
        self.assertTrue(os.path.isfile(cfg.country_cache_path))


if __name__ == "__main__":
    unittest.main()
