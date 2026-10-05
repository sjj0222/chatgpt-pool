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
