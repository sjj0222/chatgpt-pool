"""节点解析与归一化（§23 去重前置 / §24 RAW 分离）。

- 支持 ss / vmess / vless / trojan / http(s) / socks5。
- node_id 由 协议+服务器+端口+账号+密码+UUID+网络ID 生成稳定哈希，
  绝不依据节点名称（§23 / §4.1）。
"""
import base64
import hashlib
import json
import logging
import urllib.parse

from models import Node

log = logging.getLogger("pool.parser")


def make_node_id(protocol, server, port, account="", password="", uuid="", net_id=""):
    s = "|".join([
        protocol, (server or "").lower(), str(port),
        account or "", password or "", uuid or "", net_id or "",
    ])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _b64decode(s):
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s + pad)
        return raw.decode("utf-8", "replace")
    except Exception:
        try:
            raw = base64.urlsafe_b64decode(s + pad)
            return raw.decode("utf-8", "replace")
        except Exception:
            return None


def _split_hostport(hp, default_port=0):
    hp = hp.strip()
    if ":" not in hp:
        return hp, default_port
    host, _, port = hp.rpartition(":")
    return host.strip(), int(port or default_port)


def _build_node(protocol, server, port, source, raw, name="",
                account="", password="", uuid="", net_id="", params=None):
    if not server or not port:
        return None
    return Node(
        node_id=make_node_id(protocol, server, port, account, password, uuid, net_id),
        protocol=protocol, server=server, port=port,
        name=name or "", raw=raw, source=source,
        account=account, password=password, uuid=uuid, net_id=net_id,
        params=params or {},
    )


def _parse_ss(data, name, source, raw):
    if "@" in data:
        head, hp = data.split("@", 1)
        host, port = _split_hostport(hp)
        if ":" in head:
            cipher, password = head.split(":", 1)
        else:
            dec = _b64decode(head)
            if not dec or ":" not in dec:
                return None
            cipher, password = dec.split(":", 1)
    else:
        dec = _b64decode(data)
        if not dec:
            return None
        head, hp = dec.split("@", 1)
        host, port = _split_hostport(hp)
        if ":" not in head:
            return None
        cipher, password = head.split(":", 1)
    return _build_node("ss", host, port, source, raw, name,
                       password=password, params={"cipher": cipher})


def _parse_vmess(data, name, source, raw):
    dec = _b64decode(data)
    if not dec:
        return None
    obj = json.loads(dec)
    host = obj.get("add", "")
    port = int(obj.get("port") or 0)
    uid = obj.get("id", "")
    net = obj.get("net", "tcp")
    tls = obj.get("tls", "") == "tls"
    params = {
        "alterId": int(obj.get("aid") or 0),
        "network": net,
        "tls": tls,
        "host": obj.get("host", ""),
        "path": obj.get("path", ""),
        "sni": obj.get("sni", obj.get("host", "")),
        "cipher": "auto",
    }
    return _build_node("vmess", host, port, source, raw,
                       name or obj.get("ps", ""),
                       uuid=uid, net_id=net, params=params)


def _parse_vless(data, name, source, raw):
    u = urllib.parse.urlsplit("vless://" + data)
    host, port = _split_hostport(u.netloc.split("@")[-1] if "@" in u.netloc else u.netloc)
    uid = u.netloc.split("@")[0] if "@" in u.netloc else ""
    q = urllib.parse.parse_qs(u.query)
    net = (q.get("type") or ["tcp"])[0]
    tls = (q.get("security") or [""])[0] == "tls"
    params = {
        "network": net,
        "tls": tls,
        "sni": (q.get("sni") or [""])[0],
        "host": (q.get("host") or [""])[0],
        "path": (q.get("path") or [""])[0],
        "flow": (q.get("flow") or [""])[0],
    }
    return _build_node("vless", host, port, source, raw, name,
                       uuid=uid, net_id=net, params=params)


def _parse_trojan(data, name, source, raw):
    u = urllib.parse.urlsplit("trojan://" + data)
    if "@" not in u.netloc:
        return None
    pwd, hp = u.netloc.split("@", 1)
    host, port = _split_hostport(hp)
    q = urllib.parse.parse_qs(u.query)
    net = (q.get("type") or ["tcp"])[0]
    tls = (q.get("security") or [""])[0] in ("tls", "reality")
    params = {
        "network": net,
        "tls": tls,
        "sni": (q.get("sni") or [""])[0],
        "host": (q.get("host") or [""])[0],
        "path": (q.get("path") or [""])[0],
    }
    return _build_node("trojan", host, port, source, raw, name,
                       password=pwd, net_id=net, params=params)


def _parse_http(data, name, source, raw):
    u = urllib.parse.urlsplit(data)
    proto = u.scheme
    host, port = _split_hostport(u.netloc.rsplit("@", 1)[-1],
                                 default_port=443 if proto == "https" else 80)
    account = password = ""
    if "@" in u.netloc:
        account, _, _ = u.netloc.partition("@")
        if ":" in account:
            account, password = account.split(":", 1)
    return _build_node(proto, host, port, source, raw, name,
                       account=account, password=password)


def _parse_socks(data, name, source, raw):
    u = urllib.parse.urlsplit(data)
    host, port = _split_hostport(u.netloc.rsplit("@", 1)[-1], default_port=1080)
    account = password = ""
    if "@" in u.netloc:
        account, _, _ = u.netloc.partition("@")
        if ":" in account:
            account, password = account.split(":", 1)
    return _build_node("socks5", host, port, source, raw, name,
                       account=account, password=password)


def parse_line(line, source=""):
    """返回 (Node|None, is_invalid)。空行/注释行不是节点也不算无效。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, False
    raw = line
    name = ""
    if "#" in line:
        line, name = line.split("#", 1)
        name = name.strip()
    try:
        if line.startswith("ss://"):
            n = _parse_ss(line[5:], name, source, raw)
        elif line.startswith("vmess://"):
            n = _parse_vmess(line[8:], name, source, raw)
        elif line.startswith("vless://"):
            n = _parse_vless(line[8:], name, source, raw)
        elif line.startswith("trojan://"):
            n = _parse_trojan(line[9:], name, source, raw)
        elif line.startswith("http://") or line.startswith("https://"):
            n = _parse_http(line, name, source, raw)
        elif line.startswith("socks5://") or line.startswith("socks://"):
            n = _parse_socks(line, name, source, raw)
        else:
            n = None
        return n, (n is None)
    except Exception:
        log.debug("parse failed: %s", raw[:80])
        return None, True


def parse_all(source_items):
    """source_items: [(source_name, text), ...] → (nodes, invalid_count)"""
    nodes = []
    invalid = 0
    for src, text in source_items:
        for line in text.splitlines():
            n, bad = parse_line(line, src)
            if n is not None:
                nodes.append(n)
            elif bad:
                invalid += 1
    return nodes, invalid
