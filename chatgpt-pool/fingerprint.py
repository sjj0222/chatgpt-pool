"""节点指纹（V0.3 §3 Node Fingerprint）。

node_fingerprint 用于跨轮唯一识别节点，组成：
  protocol + server + port + 认证信息 hash + TLS 参数 + 网络参数

用途：
- 去重（§23 基础上补充跨轮稳定识别）。
- 历史继承：同 fingerprint 保留历史信用（§3 规则）。
- 节点迁移识别：仅名称/来源变化时 fingerprint 不变。

规则：
- 规范化输入（params 按键排序、忽略空值），同参数必得同 fingerprint。
- 输出 sha256 hex（32 字符）。
- 不保存任何账号明文/Token（§35 安全规则：只保存质量数据与哈希）。
"""
import hashlib
import json


def _norm_params(params):
    """TLS/网络等参数规范化：仅保留非空标量，按键排序，稳定序列化。"""
    if not params:
        return {}
    out = {}
    for k in sorted(params.keys()):
        v = params[k]
        if v in (None, "", [], {}):
            continue
        out[k] = str(v)
    return out


def _norm(value):
    return "" if value is None else str(value)


def generate_fingerprint(protocol, server, port, account="", password="",
                         uuid="", params=None):
    """protocol+server+port+认证信息+网络参数 → 稳定 sha256 hex。

    认证信息以单向 hash 参与（§35：不保存明文凭据），UUID 也作为身份组成。
    """
    payload = {
        "protocol": _norm(protocol).lower(),
        "server": _norm(server).lower(),
        "port": int(port) if port else 0,
        "account": _norm(account),
        "password": _norm(password),
        "uuid": _norm(uuid),
        "params": _norm_params(params),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint_for_node(node):
    """从 Node 对象生成指纹（名称/来源/raw 不参与，保证迁移识别与去重）。"""
    return generate_fingerprint(
        node.protocol, node.server, node.port,
        account=node.account, password=node.password, uuid=node.uuid,
        params=node.params)
