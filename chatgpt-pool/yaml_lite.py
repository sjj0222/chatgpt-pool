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


def emit_mihomo_config(nodes, mixed_port, api_port=None, mode="rule",
                       group_url="https://www.gstatic.com/generate_204",
                       log_level="info"):
    """生成 Mihomo 配置（P1-08：Mihomo 负责节点代理/切换/代理连接/出口 IP）。

    - 生产常驻（api_port 非空）：完整配置——external-controller 供热重载/健康检查，
      多节点时加 url-test 自动切换组 PROXY，rules 全量走 PROXY（订阅场景全代理）。
    - 单节点检测（api_port=None）：最小配置，GLOBAL select 单节点，不启用 url-test，
      与逐节点检测引擎行为一致。
    """
    names = [str(n.get("name") or n.get("server")) for n in nodes]
    if len(names) >= 2:
        groups = [
            "proxy-groups:",
            "  - name: PROXY",
            "    type: url-test",
            f'    url: "{group_url}"',
            "    interval: 300",
            "    tolerance: 50",
            "    proxies:",
        ]
        for nm in names:
            groups.append(f'      - "{nm}"')
        groups += [
            "  - name: GLOBAL",
            "    type: select",
            "    proxies:",
            "      - PROXY",
        ]
        for nm in names:
            groups.append(f'      - "{nm}"')
        rules = ["rules:", "  - MATCH,PROXY"]
    else:
        groups = [
            "proxy-groups:",
            "  - name: GLOBAL",
            "    type: select",
            "    proxies:",
        ]
        for nm in names:
            groups.append(f'      - "{nm}"')
        rules = ["rules:", "  - MATCH,GLOBAL"]

    lines = [
        f"mixed-port: {int(mixed_port)}",
        'bind-address: "127.0.0.1"',
        "allow-lan: false",
        f"mode: {mode}",
        f"log-level: {log_level}",
        "ipv6: false",
    ]
    if api_port is not None:
        lines.append(f'external-controller: "127.0.0.1:{int(api_port)}"')
    lines += ["proxies:"]
    for n in nodes:
        lines += _entry_lines(n)
    lines += groups + rules
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
