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
    node_fingerprint: str = ""       # V0.3：跨轮唯一识别（protocol/server/port/认证/参数）

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        kw = {}
        for k in cls.__dataclass_fields__:
            if k in d:
                kw[k] = d[k]
        return cls(**kw)


# ============ V0.3 Node Quality Optimization：数据模型扩展 ============
# Phase 1 纯增量：新增枚举/结构/存储，不改动 V0.2.2.2 任何既有枚举与格式。

# 数据 schema 版本（V0.3 §26：只增字段，禁止破坏历史数据）。
# 3 = V0.3 质量管理系统数据模型；历史数据无此字段视为旧版，读取兼容。
SCHEMA_VERSION = 3


class LifecycleState(str, Enum):
    """节点生命周期状态（V0.3 §4/§5）。

    与 Stage（单轮检测管线阶段，§7）是两个独立维度：
    Stage 描述本轮检测走到哪一步；LifecycleState 描述节点跨轮生命周期位置。
    """
    DISCOVERED = "DISCOVERED"    # 来源发现：格式/协议正确，不能进入生产
    VALIDATED = "VALIDATED"      # 基础验证：地址/端口/参数完整
    PROBING = "PROBING"          # 质量检测中（TCP→握手→出口IP→TLS→目标服务）
    PROBATION = "PROBATION"      # 新节点观察期，不能直接进入 ACTIVE
    STANDBY = "STANDBY"          # 备用节点：ACTIVE 不足时补充
    ACTIVE = "ACTIVE"            # 生产节点：满足评分阈值
    DEGRADED = "DEGRADED"        # 质量下降：连续失败触发
    COOLDOWN = "COOLDOWN"        # 临时暂停，等待恢复
    RECOVERING = "RECOVERING"    # 恢复重检：成功→ACTIVE，失败→FAILED
    FAILED = "FAILED"            # 失败（可恢复的终点态）
    BLACKLIST = "BLACKLIST"      # 永久淘汰


class SourceState(str, Enum):
    """数据源生命周期（V0.3 §17）。"""
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class Capability(str, Enum):
    """服务能力标签（V0.3 §9，用于后续路由）。"""
    CHATGPT = "chatgpt"
    GITHUB = "github"
    CLAUDE = "claude"
    GEMINI = "gemini"
    WEB = "web"


class NodeEventType(str, Enum):
    """节点事件（V0.3 §27）。"""
    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    PROMOTED = "PROMOTED"        # 晋升
    DEGRADED = "DEGRADED"        # 降级
    RECOVERED = "RECOVERED"      # 恢复
    COOLDOWN = "COOLDOWN"
    EXPIRED = "EXPIRED"          # 过期待删除
    DELETED = "DELETED"


# 质量评分权重（V0.3 §7：稳定性 40% + 可用率 30% + 延迟 20% + 历史信用 10%）
SCORE_WEIGHTS = {
    "stability": 0.40,
    "availability": 0.30,
    "latency": 0.20,
    "credit": 0.10,
}

# 时间衰减（V0.3 §15：24 小时窗口权重 70%，30 天历史 30%）
AVAILABILITY_WINDOWS = {          # 可用率统计窗口（秒）
    "5m": 300,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}
TIME_DECAY = {"recent_24h": 0.70, "history_30d": 0.30}


@dataclass
class NodeQualityScore:
    """节点质量评分 0-100（V0.3 §7/§28）。

    overall = stability*40% + availability*30% + latency*20% + credit*10%；
    breakdown 必须可单独展示（§28：不能只显示总分）。
    """
    overall: float = 0.0
    stability: float = 0.0        # 稳定性：成功次数/总检测次数（0-100）
    availability: float = 0.0     # 可用率（5m/1h/24h/7d 窗口加权，0-100）
    latency: float = 0.0          # 延迟得分（0-100，越低越好）
    credit: float = 0.0           # 历史信用（长期稳定/恢复记录/历史表现，0-100）
    windows: dict = field(default_factory=dict)   # availability 窗口明细

    def compute_overall(self, weights=None):
        w = weights or SCORE_WEIGHTS
        self.overall = round(
            self.stability * w["stability"]
            + self.availability * w["availability"]
            + self.latency * w["latency"]
            + self.credit * w["credit"], 1)
        return self.overall

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class ServiceScore:
    """多维服务评分（V0.3 §8）：禁止只有总分。"""
    overall: float = 0.0          # 综合质量分（同 NodeQualityScore.overall 口径）
    services: dict = field(default_factory=dict)  # {capability: score}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class NodeEvent:
    """节点事件记录（V0.3 §27）。"""
    ts: str = ""
    event: str = ""               # NodeEventType
    node_id: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})
