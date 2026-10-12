"""支持国家列表三级来源（§6 / P1-06）。

① 官方在线列表 → ② 本地缓存 supported_countries.json → ③ 内置最近确认列表。
三者全失败 → COUNTRY_LIST_UNAVAILABLE（系统级，禁止继续筛选并生成新池，§18）。

V0.4.5（fix/country-list-parser-v045）：在线来源不再使用全文双字母正则
（\b[A-Z]{2}\b 会把正文/导航/脚本中的合法 ISO 片段误收为国家代码）。
改为：解析真实页面结构（渲染后的国家名称 bullet 列表）→ 国家名称显式映射
为 ISO 3166-1 alpha-2 → 全部经 is_valid_code() 白名单验证 → 去重 + 确定性排序
→ 有效数不低于 min_online_country_codes 才接受。

V0.4.4（fix/geoip-ipwhois）：ISO 白名单 + 缓存脏数据整份拒绝。
来源优先级与 FALLBACK/UNAVAILABLE 语义保持不变。
"""
import json
import logging
import os
import re
import unicodedata

from probe import ProbeError

log = logging.getLogger("pool.countries")

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


# ---- V0.4.5：国家名称 → ISO 代码映射（显式、可离线验证） ----
# 键为 normalize_country_name() 规范化后的名称（小写、去重音、去标点、保留
# 括号内注释，如 "congo (brazzaville)" → "congo brazzaville"）。
# 仅收录官方页面真实出现的名称及其有可靠依据的常见变体；
# 不收录 "UK"/"EU"/"USA" 等非 ISO 名称。

def normalize_country_name(name):
    """规范化国家名称：NFD 去重音 → ASCII → 小写 → 去标点 → 压缩空白。

    "Côte d'Ivoire" → "cote d ivoire"；"Congo (Brazzaville)" → "congo brazzaville"。
    """
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFD", name)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


_NAME_TO_CODE = {
    "albania": "AL", "algeria": "DZ", "afghanistan": "AF", "andorra": "AD",
    "angola": "AO", "antigua and barbuda": "AG", "argentina": "AR",
    "armenia": "AM", "australia": "AU", "austria": "AT", "azerbaijan": "AZ",
    "bahamas": "BS", "bahrain": "BH", "bangladesh": "BD", "barbados": "BB",
    "belgium": "BE", "belize": "BZ", "benin": "BJ", "bhutan": "BT",
    "bolivia": "BO", "bosnia and herzegovina": "BA", "botswana": "BW",
    "brazil": "BR", "brunei": "BN", "bulgaria": "BG", "burkina faso": "BF",
    "burundi": "BI", "cabo verde": "CV", "cambodia": "KH", "cameroon": "CM",
    "canada": "CA", "central african republic": "CF", "chad": "TD",
    "chile": "CL", "colombia": "CO", "comoros": "KM",
    "congo brazzaville": "CG", "congo drc": "CD", "costa rica": "CR",
    "cote d ivoire": "CI", "croatia": "HR", "cyprus": "CY",
    "czechia czech republic": "CZ", "czechia": "CZ", "denmark": "DK",
    "djibouti": "DJ", "dominica": "DM", "dominican republic": "DO",
    "ecuador": "EC", "egypt": "EG", "el salvador": "SV",
    "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE",
    "eswatini swaziland": "SZ", "eswatini": "SZ", "ethiopia": "ET",
    "fiji": "FJ", "finland": "FI", "france": "FR", "gabon": "GA",
    "gambia": "GM", "georgia": "GE", "germany": "DE", "ghana": "GH",
    "greece": "GR", "grenada": "GD", "guatemala": "GT", "guinea": "GN",
    "guinea bissau": "GW", "guyana": "GY", "haiti": "HT",
    "holy see vatican city": "VA", "honduras": "HN", "hungary": "HU",
    "iceland": "IS", "india": "IN", "indonesia": "ID", "iraq": "IQ",
    "ireland": "IE", "israel": "IL", "italy": "IT", "jamaica": "JM",
    "japan": "JP", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE",
    "kiribati": "KI", "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA",
    "latvia": "LV", "lebanon": "LB", "lesotho": "LS", "liberia": "LR",
    "libya": "LY", "liechtenstein": "LI", "lithuania": "LT",
    "luxembourg": "LU", "madagascar": "MG", "malawi": "MW", "malaysia": "MY",
    "maldives": "MV", "mali": "ML", "malta": "MT", "marshall islands": "MH",
    "mauritania": "MR", "mauritius": "MU", "mexico": "MX",
    "micronesia": "FM", "moldova": "MD", "monaco": "MC", "mongolia": "MN",
    "montenegro": "ME", "morocco": "MA", "mozambique": "MZ", "myanmar": "MM",
    "namibia": "NA", "nauru": "NR", "nepal": "NP", "netherlands": "NL",
    "new zealand": "NZ", "nicaragua": "NI", "niger": "NE", "nigeria": "NG",
    "north macedonia": "MK", "norway": "NO", "oman": "OM", "pakistan": "PK",
    "palau": "PW", "palestine": "PS", "panama": "PA",
    "papua new guinea": "PG", "paraguay": "PY", "peru": "PE",
    "philippines": "PH", "poland": "PL", "portugal": "PT", "qatar": "QA",
    "romania": "RO", "rwanda": "RW", "saint kitts and nevis": "KN",
    "saint lucia": "LC", "saint vincent and the grenadines": "VC",
    "samoa": "WS", "san marino": "SM", "sao tome and principe": "ST",
    "saudi arabia": "SA", "senegal": "SN", "serbia": "RS",
    "seychelles": "SC", "sierra leone": "SL", "singapore": "SG",
    "slovakia": "SK", "slovenia": "SI", "solomon islands": "SB",
    "somalia": "SO", "south africa": "ZA", "south korea": "KR",
    "south sudan": "SS", "spain": "ES", "sri lanka": "LK",
    "suriname": "SR", "sweden": "SE", "switzerland": "CH", "sudan": "SD",
    "taiwan": "TW", "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "timor leste east timor": "TL", "togo": "TG", "tonga": "TO",
    "trinidad and tobago": "TT", "tunisia": "TN", "turkey": "TR",
    "turkmenistan": "TM", "tuvalu": "TV", "uganda": "UG",
    "ukraine with certain exceptions": "UA", "ukraine": "UA",
    "united arab emirates": "AE", "united kingdom": "GB",
    "united states of america": "US", "uruguay": "UY", "uzbekistan": "UZ",
    "vanuatu": "VU", "vietnam": "VN", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}


def map_country_name(name):
    """国家名称 → ISO 代码；未知名称返回 None，绝不猜测映射。"""
    return _NAME_TO_CODE.get(normalize_country_name(name))


def parse_country_list(text):
    """从页面文本解析国家列表 → 去重、确定性排序的有效 ISO 代码列表。

    识别真实页面结构：渲染后的 Markdown bullet 列表（行首 "- 国家名"）。
    只提取列表条目行，不扫描整页双字母词；条目经 map_country_name() 映射，
    全部经 is_valid_code() 白名单验证；未知名称记录日志但不猜测。
    空页面 / 无列表 / 无法识别 / 无有效结果 → 返回 None（安全失败）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    items = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*•]\s+(.+?)\s*$", line)
        if m:
            items.append(m.group(1).strip())
    if not items:
        return None
    codes = set()
    unknown = []
    for name in items:
        code = map_country_name(name)
        if code is not None and is_valid_code(code):
            codes.add(code)
        else:
            unknown.append(name)
    if unknown:
        log.warning("country name(s) not mapped (%d): %s",
                    len(unknown), ", ".join(unknown[:5]))
    if not codes:
        return None
    return sorted(codes)


def fetch_online_countries(cfg, probe=None):
    """拉取官方在线列表；解析国家名称列表并映射为有效 ISO 代码。

    接受条件（缺一即返回 None → 走缓存/内置降级）：
    1) HTTP 状态有效；
    2) 能从页面结构识别国家列表（parse_country_list 非 None）；
    3) 所有输出代码经 is_valid_code() 白名单验证；
    4) 去重后有效数量 >= cfg.min_online_country_codes。
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
    codes = parse_country_list(r.body)
    if not codes:
        return None
    if len(codes) < cfg.min_online_country_codes:
        return None
    return codes


def load_cache(path):
    """读取本地缓存；脏数据整份拒绝（返回 None → 降级内置）。

    明确规则（V0.4.4/V0.4.5）：
    - 必须为非空 JSON 数组；每项必须是字符串；
    - 每项先执行 strip().upper() 规范化（小写、首尾空格被规范化，如
      "us"/" JP " → "US"/"JP"）；
    - 规范化后每项必须通过 is_valid_code() 白名单校验；
    - 规范化后出现重复项 → 整份拒绝；
    - 无效代码 / 非字符串 / 空字符串 / 空列表 / 非数组 / 坏 JSON → 整份拒绝。
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
