"""支持国家列表三级来源（§6 / P1-06）。

① 官方在线列表 → ② 本地缓存 supported_countries.json → ③ 内置最近确认列表。
三者全失败 → COUNTRY_LIST_UNAVAILABLE（系统级，禁止继续筛选并生成新池，§18）。

V0.4.4（fix/geoip-ipwhois）：建立明确、可复用的 ISO 3166-1 alpha-2 有效代码
白名单；在线列表与缓存必须逐项验证代码有效性，ZZ/XX/UK/EU 等非 ISO 代码
不得进入任何来源的最终列表；缓存脏数据（无效代码/重复项/非字符串/空值）
整份拒绝并降级。来源优先级与 FALLBACK/UNAVAILABLE 语义保持不变。
"""
import json
import logging
import os
import re

from probe import ProbeError

log = logging.getLogger("pool.countries")

CODE_RE = re.compile(r"\b([A-Z]{2})\b")

# ISO 3166-1 alpha-2 正式分配代码全集（249 项）。
# 排除 user-assigned / reserved（AA, QM, QZ, XA..XZ, ZZ 等）。
ISO3166_ALPHA2 = frozenset({
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT",
    "AU", "AW", "AX", "AZ",
    "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN",
    "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY", "BZ",
    "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO",
    "CR", "CU", "CV", "CW", "CX", "CY", "CZ",
    "DE", "DJ", "DK", "DM", "DO", "DZ",
    "EC", "EE", "EG", "EH", "ER", "ES", "ET",
    "FI", "FJ", "FK", "FM", "FO", "FR",
    "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL", "GM", "GN", "GP",
    "GQ", "GR", "GS", "GT", "GU", "GW", "GY",
    "HK", "HM", "HN", "HR", "HT", "HU",
    "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT",
    "JE", "JM", "JO", "JP",
    "KE", "KG", "KH", "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ",
    "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY",
    "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MO",
    "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW", "MX", "MY", "MZ",
    "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU", "NZ",
    "OM",
    "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT",
    "PW", "PY",
    "QA",
    "RE", "RO", "RS", "RU", "RW",
    "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
    "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ",
    "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR",
    "TT", "TV", "TW", "TZ",
    "UA", "UG", "UM", "US", "UY", "UZ",
    "VA", "VC", "VE", "VG", "VI", "VN", "VU",
    "WF", "WS",
    "YE", "YT",
    "ZA", "ZM", "ZW",
})


def is_valid_code(code):
    """ISO 3166-1 alpha-2 有效性判定（可复用）。

    必须是 2 位 ASCII 大写字母且位于白名单。ZZ/XX/UK/EU 等一律无效。
    """
    return isinstance(code, str) and len(code) == 2 and code in ISO3166_ALPHA2


def _filter_valid(candidates):
    """从候选集合中分离有效/无效代码，返回 (valid_sorted, invalid_sorted)。"""
    valid = sorted(c for c in candidates if is_valid_code(c))
    invalid = sorted(c for c in candidates if not is_valid_code(c))
    return valid, invalid


def fetch_online_countries(cfg, probe=None):
    """拉取官方在线列表并逐项验证 ISO 3166-1 Alpha-2 代码。

    不能仅凭两位大写字母数量达标就接受：无效代码（ZZ/UK/EU 等）先剔除，
    仅统计有效代码；有效数 < min_online_country_codes 视为来源不可靠 → None。
    """
    if probe is None:
        from probe import NoProxyProbe
        probe = NoProxyProbe(cfg)
    try:
        r = probe(cfg.country_source_url)
    except ProbeError:
        return None
    if r.status is None or r.status >= 400:
        return None
    candidates = set(CODE_RE.findall(r.body))
    valid, invalid = _filter_valid(candidates)
    if invalid:
        log.warning("online country list dropped %d invalid code(s): %s",
                    len(invalid), ",".join(invalid))
    if len(valid) >= cfg.min_online_country_codes:
        return valid
    return None


def load_cache(path):
    """读取本地缓存；脏数据整份拒绝（返回 None → 降级）。

    明确拒绝规则：非 list / 空列表 / 含非字符串 / 含无效代码 / 含重复项。
    缓存必须精确地是一组有效且唯一的 ISO 代码，否则不可信。
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            return None
        if not all(isinstance(c, str) for c in data):
            return None
        codes = [c.strip().upper() for c in data]
        if not all(is_valid_code(c) for c in codes):
            return None
        if len(set(codes)) != len(codes):
            return None
        return codes
    except Exception:
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
    来源优先级不变：在线 → 缓存 → 内置。
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
