"""场景 G/H：国家列表三级来源（§6 / P1-06）与系统级失败。"""
import json
import os
import unittest
from unittest import mock

import countries
from helpers import make_cfg
from models import FailureReason


class TestCountryList(unittest.TestCase):

    def _codes(self, n=25):
        # V0.4.5：改用 ISO 白名单中的有效代码（确定性排序），不再用 X00 等无效码
        valid = sorted(countries.ISO3166_ALPHA2)
        return valid[:n] + ["US", "JP"]

    def test_online_first(self):
        cfg = make_cfg("country_online")
        codes = self._codes()
        with mock.patch("countries.fetch_online_countries", return_value=codes):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(got, codes)
        self.assertEqual(source, "online")
        self.assertIsNone(warn)
        self.assertTrue(os.path.isfile(cfg.country_cache_path))
        # V0.4.5：在线结果与写入缓存的代码必须全部合法
        self.assertTrue(all(countries.is_valid_code(c) for c in got))
        with open(cfg.country_cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        self.assertTrue(all(countries.is_valid_code(c) for c in cached))

    def test_cache_fallback(self):
        cfg = make_cfg("country_cache")
        countries.save_cache(cfg.country_cache_path, ["US", "JP"])
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(got, ["US", "JP"])
        self.assertEqual(source, "cache")

    def test_builtin_fallback_warns(self):
        cfg = make_cfg("country_builtin")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertIsNotNone(got)
        self.assertEqual(source, "builtin")
        self.assertEqual(warn, "COUNTRY_LIST_FALLBACK")

    def test_unavailable(self):
        cfg = make_cfg("country_unavail")
        cfg.builtin_countries = []
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertIsNone(got)
        self.assertEqual(warn, "COUNTRY_LIST_UNAVAILABLE")

    def test_cache_corrupt_falls_to_builtin(self):
        cfg = make_cfg("country_corrupt")
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w") as f:
            f.write("not json")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "builtin")
        self.assertEqual(warn, "COUNTRY_LIST_FALLBACK")

    def test_fetch_online_rejects_few_codes(self):
        cfg = make_cfg("country_few")
        cfg.min_online_country_codes = 20

        class R:
            status = 200
            body = "US JP GB"

        with mock.patch("probe.NoProxyProbe", side_effect=lambda c: lambda u: R()):
            self.assertIsNone(countries.fetch_online_countries(cfg))


if __name__ == "__main__":
    unittest.main()
