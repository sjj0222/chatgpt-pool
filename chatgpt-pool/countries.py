"""支持国家列表三级来源（§6 / P1-06）。

① 官方在线列表 → ② 本地缓存 supported_countries.json → ③ 内置最近确认列表。
三者全失败 → COUNTRY_LIST_UNAVAILABLE（系统级，禁止继续筛选并生成新池，§18）。
"""
import json
import logging
import os
import re

from probe import ProbeError

log = logging.getLogger("pool.countries")

CODE_RE = re.compile(r"\b([A-Z]{2})\b")


def fetch_online_countries(cfg, probe=None):
    """拉取官方在线列表并抽取 ISO 3166-1 Alpha-2 代码；数量不足视为失败。"""
    if probe is None:
        from probe import NoProxyProbe
        probe = NoProxyProbe(cfg)
    try:
        r = probe(cfg.country_source_url)
    except ProbeError:
        return None
    if r.status is None or r.status >= 400:
        return None
    codes = {c for c in CODE_RE.findall(r.body)}
    if len(codes) >= cfg.min_online_country_codes:
        return sorted(codes)
    return None


def load_cache(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            return None
        codes = [str(c).strip().upper() for c in data]
        if all(len(c) == 2 and c.isalpha() for c in codes):
            return codes
    except Exception:
        return None
    return None


def save_cache(path, codes):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(codes, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def resolve_country_list(cfg):
    """返回 (codes|None, source, warning)。

    warning 取值：None / "COUNTRY_LIST_FALLBACK" / "COUNTRY_LIST_UNAVAILABLE"。
    """
    codes = fetch_online_countries(cfg)
    if codes:
        save_cache(cfg.country_cache_path, codes)
        return codes, "online", None

    codes = load_cache(cfg.country_cache_path)
    if codes:
        return codes, "cache", None

    codes = list(cfg.builtin_countries)
    if codes:
        return codes, "builtin", "COUNTRY_LIST_FALLBACK"

    return None, "none", "COUNTRY_LIST_UNAVAILABLE"
