"""真实出口 IP 与国家判定（§5）。

只认真实出口 IP 对应的国家；节点名称/域名/SNI/标签一律不参与（§4.1）。
传输层失败映射到具体枚举；拿到响应但解析不出 IP → NO_EGRESS_IP。
"""
import json
import logging

from models import FailureReason
from probe import ProbeError

log = logging.getLogger("pool.egress")


def get_egress(probe, cfg):
    """返回 (ip, iso_alpha2|None, reason|None)。

    reason 为 FailureReason：TLS_FAIL/DNS_FAIL/TIMEOUT/PROXY_ERROR/NO_EGRESS_IP。
    """
    try:
        r = probe(cfg.geoip_url)
    except ProbeError as e:
        return None, None, e.reason
    if r.status is None or r.status >= 400:
        return None, None, FailureReason.NO_EGRESS_IP
    try:
        data = json.loads(r.body)
        ip = data.get("query") or data.get("ip") or ""
        iso = (data.get("countryCode") or data.get("country") or "").upper()
    except Exception:
        return None, None, FailureReason.NO_EGRESS_IP
    if not ip:
        return None, None, FailureReason.NO_EGRESS_IP
    return ip, (iso or None), None
