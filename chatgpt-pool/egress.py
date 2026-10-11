"""真实出口 IP 与国家判定（§5）。

只认真实出口 IP 对应的国家；节点名称/域名/SNI/标签一律不参与（§4.1）。
传输层失败细分（V0.2.2 P0-01）：超时类统一归 EGRESS_TIMEOUT（egress 阶段语义），
其余细分原因透传；出口服务错误、无效响应或缺少有效 IP/国家代码均拒绝通过。
"""
import ipaddress
import json
import logging
import re

from models import FailureReason, SubReason
from probe import ProbeError

log = logging.getLogger("pool.egress")


def get_egress(probe, cfg):
    """返回 (ip, iso_alpha2|None, reason|None, sub|None, detail|None)。

    reason 为顶层 FailureReason（§16）；sub/detail 为 V0.2.2 诊断细分。
    """
    try:
        r = probe(cfg.geoip_url)
    except ProbeError as e:
        # 超时类（TCP/TLS 超时）在 egress 阶段统一归 EGRESS_TIMEOUT
        if e.sub in (SubReason.TCP_TIMEOUT, SubReason.TLS_TIMEOUT):
            return None, None, FailureReason.TIMEOUT, \
                SubReason.EGRESS_TIMEOUT, {
                    "elapsed_ms": e.detail.get("elapsed_ms"),
                    "url": cfg.geoip_url, "probe_sub": e.sub.value}
        return None, None, e.reason, e.sub, {
            **e.detail, "url": cfg.geoip_url}
    if r.status is None or r.status >= 400:
        return None, None, FailureReason.NO_EGRESS_IP, \
            SubReason.NO_EGRESS_IP, {
                "status": r.status, "url": cfg.geoip_url,
                "response_kind": "http_error"}
    try:
        data = json.loads(r.body)
        if not isinstance(data, dict):
            raise ValueError("GeoIP JSON must be an object")
        # ipwho.is may return success=false even with HTTP 200.
        if data.get("success") is False:
            return None, None, FailureReason.NO_EGRESS_IP, \
                SubReason.NO_EGRESS_IP, {
                    "status": r.status, "url": cfg.geoip_url,
                    "response_kind": "geoip_service_error",
                    "message": str(data.get("message", ""))[:200]}
        ip = str(data.get("query") or data.get("ip") or "").strip()
        raw_iso = data.get("countryCode") or data.get("country_code") or data.get("country") or ""
        iso = str(raw_iso).strip().upper()
    except Exception:
        return None, None, FailureReason.NO_EGRESS_IP, \
            SubReason.NO_EGRESS_IP, {
                "status": r.status, "url": cfg.geoip_url,
                "response_kind": "invalid_json",
                "body_snippet": (r.body or "")[:200]}
    try:
        ip_obj = ipaddress.ip_address(ip)
        ip = str(ip_obj)
    except ValueError:
        return None, None, FailureReason.NO_EGRESS_IP, \
            SubReason.NO_EGRESS_IP, {
                "status": r.status, "url": cfg.geoip_url,
                "response_kind": "invalid_ip",
                "body_snippet": (r.body or "")[:200]}
    # is_global excludes private, loopback, link-local, multicast, reserved,
    # documentation/test ranges, unspecified, and other non-public addresses.
    if not ip_obj.is_global:
        return None, None, FailureReason.NO_EGRESS_IP, \
            SubReason.NO_EGRESS_IP, {
                "status": r.status, "url": cfg.geoip_url,
                "response_kind": "non_public_ip",
                "ip": ip,
                "body_snippet": (r.body or "")[:200]}
    if not re.fullmatch(r"[A-Z]{2}", iso):
        return None, None, FailureReason.NO_EGRESS_IP, \
            SubReason.NO_EGRESS_IP, {
                "status": r.status, "url": cfg.geoip_url,
                "response_kind": "missing_or_invalid_country_code",
                "body_snippet": (r.body or "")[:200]}
    return ip, iso, None, None, None
