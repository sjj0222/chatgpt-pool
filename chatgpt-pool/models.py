"""核心状态模型（V5.2 §7-§9 / §16-§17 / §21-§22 + P1-03）。

- stage_status 与 chatgpt_status 分离（§8/§9）。
- failure_reason 统一枚举 + 固定优先级（§16/§17 + P1-03）。
- FINAL 节点不得携带 failure_reason（§21）。
"""
from dataclasses import dataclass, field, asdict
from enum import Enum


class Stage(str, Enum):
    """节点生命周期（§7）。"""
    NEW = "NEW"
    PARSED = "PARSED"
    EGRESS_OK = "EGRESS_OK"
    COUNTRY_OK = "COUNTRY_OK"
    CHATGPT_CHECKED = "CHATGPT_CHECKED"
    STABILITY_CHECKED = "STABILITY_CHECKED"
    FINAL = "FINAL"
    REJECTED = "REJECTED"


class ChatgptStatus(str, Enum):
    """ChatGPT 检测结果（§9），与 stage_status 独立。"""
    UNKNOWN = "UNKNOWN"
    NETWORK_PASS = "NETWORK_PASS"
    STRICT_PASS = "STRICT_PASS"
    FAIL = "FAIL"


class FailureReason(str, Enum):
    """失败原因统一枚举（§16 + P1-03 新增 STRICT_CHECK_FAIL）。"""
    INVALID_NODE = "INVALID_NODE"
    PROXY_START_FAIL = "PROXY_START_FAIL"
    NO_EGRESS_IP = "NO_EGRESS_IP"
    COUNTRY_LIST_UNAVAILABLE = "COUNTRY_LIST_UNAVAILABLE"
    UNSUPPORTED_COUNTRY = "UNSUPPORTED_COUNTRY"
    OPENAI_BLOCKED = "OPENAI_BLOCKED"
    TLS_FAIL = "TLS_FAIL"
    DNS_FAIL = "DNS_FAIL"
    PROXY_ERROR = "PROXY_ERROR"
    TIMEOUT = "TIMEOUT"
    CHATGPT_UNREACHABLE = "CHATGPT_UNREACHABLE"
    STABILITY_FAIL = "STABILITY_FAIL"
    STRICT_CHECK_FAIL = "STRICT_CHECK_FAIL"  # P1-03
    INTERNAL_ERROR = "INTERNAL_ERROR"


# §17 失败优先级（STRICT_CHECK_FAIL 按 P1-03 定义置于 STABILITY_FAIL 之后、
# INTERNAL_ERROR 之前，作为节点级兜底）。
FAILURE_PRIORITY = [
    FailureReason.INVALID_NODE,
    FailureReason.PROXY_START_FAIL,
    FailureReason.NO_EGRESS_IP,
    FailureReason.COUNTRY_LIST_UNAVAILABLE,
    FailureReason.UNSUPPORTED_COUNTRY,
    FailureReason.OPENAI_BLOCKED,
    FailureReason.TLS_FAIL,
    FailureReason.DNS_FAIL,
    FailureReason.PROXY_ERROR,
    FailureReason.TIMEOUT,
    FailureReason.CHATGPT_UNREACHABLE,
    FailureReason.STABILITY_FAIL,
    FailureReason.STRICT_CHECK_FAIL,
    FailureReason.INTERNAL_ERROR,
]


class DiagnosticStage(str, Enum):
    """失败发生阶段（V0.2.2 P0-01：回答"哪一步失败"）。

    与 §7 状态机（Stage）是两个维度：Stage 描述节点走到哪一步，
    DiagnosticStage 描述失败发生在哪个检测阶段。
    """
    PARSE = "PARSE"          # 解析/规范化失败
    PROXY = "PROXY"          # 代理引擎启动/握手阶段
    EGRESS = "EGRESS"        # 真实出口 IP 获取
    COUNTRY = "COUNTRY"      # 国家判定
    TLS = "TLS"              # TLS/传输层
    CHATGPT = "CHATGPT"      # ChatGPT 严格检测
    STABILITY = "STABILITY"  # 稳定性复测
    CONFIG = "CONFIG"        # 配置无效
    UNKNOWN = "UNKNOWN"


class SubReason(str, Enum):
    """细分失败原因（V0.2.2 P0-01 统一枚举）。

    作为 failure_detail.sub_reason 使用，回答"为什么失败"；
    顶层 failure_reason 仍使用 §16 权威枚举（准入/优先级/统计不受影响）。
    """
    TCP_TIMEOUT = "TCP_TIMEOUT"
    TCP_REFUSED = "TCP_REFUSED"
    TCP_ERROR = "TCP_ERROR"
    PROXY_HANDSHAKE_FAIL = "PROXY_HANDSHAKE_FAIL"
    TLS_TIMEOUT = "TLS_TIMEOUT"
    TLS_CERT_FAIL = "TLS_CERT_FAIL"
    TLS_SNI_FAIL = "TLS_SNI_FAIL"
    TLS_HANDSHAKE_FAIL = "TLS_HANDSHAKE_FAIL"
    NO_EGRESS_IP = "NO_EGRESS_IP"
    EGRESS_TIMEOUT = "EGRESS_TIMEOUT"
    CHATGPT_BLOCKED = "CHATGPT_BLOCKED"
    CHATGPT_TIMEOUT = "CHATGPT_TIMEOUT"
    CONFIG_INVALID = "CONFIG_INVALID"
    UNKNOWN_FAIL = "UNKNOWN_FAIL"


def highest_priority(reasons):
    """多个问题同时出现时，按 §17 优先级取最终 failure_reason。"""
    rs = set(reasons)
    for r in FAILURE_PRIORITY:
        if r in rs:
            return r
    return None


@dataclass
class Node:
    """一个归一化节点；RAW 原文保留在 raw 字段，检测结果与 RAW 分离（§24）。"""
    node_id: str = ""
    protocol: str = ""
    server: str = ""
    port: int = 0
    name: str = ""
    raw: str = ""
    source: str = ""
    account: str = ""
    password: str = ""
    uuid: str = ""
    net_id: str = ""
    params: dict = field(default_factory=dict)
    stage: str = Stage.NEW.value
    chatgpt_status: str = ChatgptStatus.UNKNOWN.value
    failure_reason: str = ""
    failure_stage: str = ""          # V0.2.2：失败阶段（DiagnosticStage）
    failure_detail: dict = field(default_factory=dict)  # V0.2.2：sub_reason + 上下文
    check_detail: dict = field(default_factory=dict)
    egress_ip: str = ""
    country: str = ""
    stability_results: list = field(default_factory=list)
    stability_pass: bool = False
    round_id: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        kw = {}
        for k in cls.__dataclass_fields__:
            if k in d:
                kw[k] = d[k]
        return cls(**kw)
