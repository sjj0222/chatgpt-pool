"""V0.4.5：国家列表解析器测试（fix/country-list-parser-v045）。

全部 Mock / 本地 fixture，不发起真实网络请求；不写生产池。
覆盖：真实页面结构解析、确定性、噪声抵抗、缓存规范化规则、三级降级。
"""
import json
import os
import unittest
from unittest import mock

import countries
from helpers import make_cfg

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "openaicountries.md")


def _load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return f.read()


class _R:
    def __init__(self, status, body):
        self.status = status
        self.body = body


def _probe_for(body):
    def factory(_cfg):
        def probe(_url):
            return _R(200, body)
        return probe
    return factory


class TestParseRealPage(unittest.TestCase):
    """8.1 真实页面结构 fixture。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _load_fixture()
        cls.parsed = countries.parse_country_list(cls.body)

    def test_fixture_has_no_iso_codes(self):
        """真实页面是纯名称列表，不含两位大写 ISO 代码（回归保护）。"""
        import re
        self.assertFalse(re.search(r"\b[A-Z]{2}\b", self.body))

    def test_parses_to_valid_codes(self):
        self.assertIsNotNone(self.parsed)
        self.assertGreaterEqual(len(self.parsed), 20)
        self.assertTrue(all(countries.is_valid_code(c) for c in self.parsed))

    def test_known_codes_present(self):
        for c in ("US", "JP", "GB", "DE", "FR", "AU"):
            self.assertIn(c, self.parsed)

    def test_deterministic(self):
        self.assertEqual(countries.parse_country_list(self.body), self.parsed)
        self.assertEqual(self.parsed, sorted(set(self.parsed)))

    def test_no_non_iso_output(self):
        self.assertNotIn("UK", self.parsed)
        self.assertNotIn("EU", self.parsed)
        self.assertNotIn("USA", self.parsed)
        self.assertNotIn("ZZ", self.parsed)

    def test_online_accepts_real_page(self):
        cfg = make_cfg("parser_online")
        with mock.patch("probe.NoProxyProbe", side_effect=_probe_for(self.body)):
            got = countries.fetch_online_countries(cfg)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(len(got), cfg.min_online_country_codes)
        self.assertTrue(all(countries.is_valid_code(c) for c in got))


class TestParseFailureModes(unittest.TestCase):
    """8.1 安全失败 / 8.2 噪声抵抗。"""

    def test_empty_body(self):
        self.assertIsNone(countries.parse_country_list(""))
        self.assertIsNone(countries.parse_country_list("   \n  "))

    def test_no_list_structure(self):
        body = ("Albania Algeria Argentina\n" + "US JP GB\n" +
                "plain paragraph with no bullets")
        self.assertIsNone(countries.parse_country_list(body))

    def test_noise_26_valid_codes_without_list(self):
        """正文含 26+ 合法双字母片段但无列表结构 → 拒绝。"""
        noise = " ".join(sorted(countries.ISO3166_ALPHA2)[:26])
        body = "Navigation: " + noise + " footer\n<script>var x=1;</script>"
        self.assertIsNone(countries.parse_country_list(body))

    def test_nav_footer_scripts_with_bullets_ignored(self):
        """导航/脚本/正文含大量合法 ISO 码，但 bullet 行都是真实国家 → 正常接受。"""
        body = ("<nav>US JP GB DE FR IT ES NL SE</nav>\n"
                "<script>var s='US JP GB';</script>\n"
                "- Albania\n- Algeria\n- Argentina\n- Australia\n"
                "- Austria\n- Belgium\n- Brazil\n- Canada\n"
                "- Chile\n- Colombia\n- Denmark\n- Egypt\n"
                "- Finland\n- France\n- Germany\n- Greece\n"
                "- Iceland\n- India\n- Ireland\n- Italy\n- Japan\n- Mexico\n")
        codes = countries.parse_country_list(body)
        self.assertIsNotNone(codes)
        for c in codes:
            self.assertTrue(countries.is_valid_code(c))

    def test_eu_uk_usa_zz_noise_rejected(self):
        """EU/UK/USA/ZZ 噪声：不映射为任何代码。"""
        body = ("- Albania\n- United Kingdom\n- United States of America\n"
                "- UK\n- EU\n- USA\n- ZZ\n- Germany\n")
        codes = countries.parse_country_list(body)
        self.assertIsNotNone(codes)
        self.assertEqual(codes, ["AL", "DE", "GB", "US"])

    def test_below_threshold_rejected(self):
        """只有少量真实国家 → 低于阈值 → 在线来源拒绝。"""
        body = ("- Albania\n- Algeria\n- Argentina\n- Australia\n- Austria\n")
        with mock.patch("probe.NoProxyProbe", side_effect=_probe_for(body)):
            cfg = make_cfg("parser_few")
            self.assertIsNone(countries.fetch_online_countries(cfg))

    def test_unknown_names_not_guessed(self):
        """未知名称不猜测映射，只记录日志。"""
        with mock.patch("countries.log.warning") as warn:
            codes = countries.parse_country_list(
                "- Atlantis\n- Wakanda\n- Germany\n")
        self.assertEqual(codes, ["DE"])
        warn.assert_called_once()

    def test_mixed_extra_text_only_accepts_list_items(self):
        """正常列表混入无关文本：只接受列表结构条目，非名称条目不映射。"""
        body = ("# Supported countries\nSome intro text.\n"
                "- Albania\n- Atlantis\n- US\nfooter notes here\n")
        codes = countries.parse_country_list(body)
        self.assertEqual(codes, ["AL"])


class TestCacheNormalization(unittest.TestCase):
    """六：缓存规则（含规范化 docstring 行为固定）。"""

    def _write(self, cfg, data):
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_lowercase_and_spaces_normalized(self):
        cfg = make_cfg("cache_norm1")
        self._write(cfg, ["us", " JP ", "GB"])
        self.assertEqual(countries.load_cache(cfg.country_cache_path),
                         ["US", "JP", "GB"])

    def test_dup_after_normalization_rejected(self):
        cfg = make_cfg("cache_norm2")
        self._write(cfg, ["US", "us"])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))
        self._write(cfg, ["US", " US"])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_invalid_rejected(self):
        cfg = make_cfg("cache_norm3")
        self._write(cfg, ["US", "ZZ"])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_non_string_rejected(self):
        cfg = make_cfg("cache_norm4")
        self._write(cfg, ["US", 123])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_empty_value_rejected(self):
        cfg = make_cfg("cache_norm5")
        self._write(cfg, ["US", ""])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_empty_list_rejected(self):
        cfg = make_cfg("cache_norm6")
        self._write(cfg, [])
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))

    def test_bad_json_rejected(self):
        cfg = make_cfg("cache_norm7")
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            f.write("{bad json")
        self.assertIsNone(countries.load_cache(cfg.country_cache_path))


class TestResolveDegradation(unittest.TestCase):
    """8.3 降级行为：状态码/警告语义不变。"""

    def test_online_success(self):
        cfg = make_cfg("deg_online")
        codes = sorted(countries.ISO3166_ALPHA2)[:25]
        with mock.patch("countries.fetch_online_countries", return_value=codes):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "online")
        self.assertIsNone(warn)
        self.assertEqual(got, codes)

    def test_cache_fallback(self):
        cfg = make_cfg("deg_cache")
        countries.save_cache(cfg.country_cache_path, ["US", "JP"])
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "cache")
        self.assertIsNone(warn)
        self.assertEqual(got, ["US", "JP"])

    def test_builtin_fallback(self):
        cfg = make_cfg("deg_builtin")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertEqual(source, "builtin")
        self.assertEqual(warn, "COUNTRY_LIST_FALLBACK")
        self.assertIsNotNone(got)

    def test_unavailable(self):
        cfg = make_cfg("deg_unavail")
        cfg.builtin_countries = []
        os.makedirs(os.path.dirname(cfg.country_cache_path), exist_ok=True)
        with open(cfg.country_cache_path, "w", encoding="utf-8") as f:
            f.write("{bad")
        with mock.patch("countries.fetch_online_countries", return_value=None):
            got, source, warn = countries.resolve_country_list(cfg)
        self.assertIsNone(got)
        self.assertEqual(warn, "COUNTRY_LIST_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
