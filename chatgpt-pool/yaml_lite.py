"""极简 YAML 生成与校验（零第三方依赖，适配 Termux）。

负责：最终订阅 chatgpt.yaml（§25）、Mihomo 单节点/生产配置（P1-08）、
以及原子替换前的结构校验（§27：tmp 验证通过才替换）。

缩进约定：proxies 下条目为 2 空格，条目字段为 4 空格（Clash 兼容）。
"""
import json


def _q(v):
    return json.dumps(str(v), ensure_ascii=False)


def _base_lines(node):
    return [
        f'  - name: {_q(node.get("name") or node.get("server"))}',
        f'    type: {_q(node.get("protocol"))}',
        f'    server: {_q(node.get("server"))}',
        f'    port: {int(node.get("port") or 0)}',
    ]


def _entry_lines(node):
    """按协议输出 Clash/Mihomo 可识别的代理条目。"""
    p = node.get("params") or {}
    t = node.get("protocol")
    lines = _base_lines(node)
    if t == "ss":
        lines += [
            f'    cipher: {_q(p.get("cipher", "aes-256-gcm"))}',
            f'    password: {_q(node.get("password"))}',
        ]
    elif t == "vmess":
        lines += [
            f'    uuid: {_q(node.get("uuid"))}',
            f'    alterId: {int(p.get("alterId", 0))}',
            f'    cipher: auto',
            f'    tls: {bool(p.get("tls", False))}',
            f'    network: {_q(p.get("network", "tcp"))}',
        ]
        if p.get("path"):
            lines += [f'    ws-opts: {{ path: {_q(p.get("path"))}, headers: {{ Host: {_q(p.get("host", node.get("server")))} }} }}']
        if p.get("sni"):
            lines += [f'    sni: {_q(p.get("sni"))}']
    elif t == "vless":
        lines += [
            f'    uuid: {_q(node.get("uuid"))}',
            f'    network: {_q(p.get("network", "tcp"))}',
            f'    tls: {bool(p.get("tls", False))}',
            f'    udp: true',
        ]
        if p.get("sni"):
            lines += [f'    sni: {_q(p.get("sni"))}']
        if p.get("flow"):
            lines += [f'    flow: {_q(p.get("flow"))}']
        if p.get("path"):
            lines += [f'    ws-opts: {{ path: {_q(p.get("path"))}, headers: {{ Host: {_q(p.get("host", node.get("server")))} }} }}']
    elif t == "trojan":
        lines += [
            f'    password: {_q(node.get("password"))}',
            f'    sni: {_q(p.get("sni", node.get("server")))}',
        ]
        if p.get("path"):
            lines += [f'    network: {_q(p.get("network", "ws"))}',
                      f'    ws-opts: {{ path: {_q(p.get("path"))}, headers: {{ Host: {_q(p.get("host", node.get("server")))} }} }}']
    elif t in ("http", "https", "socks5"):
        if node.get("account"):
            lines += [f'    username: {_q(node.get("account"))}',
                      f'    password: {_q(node.get("password"))}']
    return lines


def emit_subscription(nodes):
    """生成最终订阅 chatgpt.yaml（§25）。"""
    out = ["proxies:"]
    for n in nodes:
        out += _entry_lines(n)
    return "\n".join(out) + "\n"


def emit_mihomo_config(nodes, mixed_port, mode="global"):
    """生成 Mihomo 配置（P1-08：Mihomo 负责节点代理/切换/出口）。"""
    lines = [
        f"mixed-port: {int(mixed_port)}",
        "mode: global",
        "log-level: warning",
        "allow-lan: false",
        'bind-address: "127.0.0.1"',
        "ipv6: false",
        "proxies:",
    ]
    for n in nodes:
        lines += _entry_lines(n)
    lines += [
        "proxy-groups:",
        "  - name: GLOBAL",
        "    type: select",
        "    proxies: [node]",
        "rules: []",
    ]
    return "\n".join(lines) + "\n"


def validate_subscription(text):
    """最小结构校验：必须含 proxies 且至少 1 个条目（§27 / P1-05）。"""
    if not text or "proxies:" not in text:
        return False
    count = 0
    for line in text.splitlines():
        if line.strip().startswith("- name:"):
            count += 1
    return count > 0
