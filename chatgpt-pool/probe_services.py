"""V0.3 服务能力探测（§22 L3 服务检测，§8/§9 的 service_score 数据源）。

通过本地 Mihomo 代理端口（cfg.prod_mihomo_port，默认 7890）探测各服务域名：
- github / claude / gemini / web
- 结果写入 lifecycle 记录的 probe_scores（service_score 直接读取）。

真实运行依赖生产 Mihomo 在线（scheduler probe 任务在 mihomo 运行时执行）；
无代理时跳过并记录，不改变任何节点行为。

安全：只做 HTTP(S) 可达性探测，不保存任何账号/Token（§35）。
"""
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("pool.probe_services")

SERVICE_TARGETS = {
    "github": "https://github.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "web": "https://www.google.com",
}
PROBE_TIMEOUT = 5.0


def _proxy_probe(url, proxy_port, timeout=PROBE_TIMEOUT):
    """经 http 代理端口探测目标（走 CONNECT 隧道）。

    返回 0-100：任何 HTTP 响应（含 4xx/5xx，说明隧道与目标可达）
    → 100（网络可达证据）；异常 → 0。
    """
    proxy = "http://127.0.0.1:%d" % proxy_port
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with opener.open(req, timeout=timeout) as resp:
            return 100.0
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也是可达证据（服务器响应了）
        return 100.0
    except Exception:
        return 0.0


def probe_service(probe_fn, url, timeout=PROBE_TIMEOUT):
    """通用探测入口（支持注入；probe_fn(url) → score）。"""
    return probe_fn(url)


def run_service_probe(cfg, node_ids=None, proxy_port=None, probe_fn=None,
                      scorer=None):
    """对节点执行服务探测并写回 lifecycle.probe_scores。

    probe_fn：注入的探测函数 url→score（测试用）；默认走代理端口。
    返回 {node_id: {service: score}}。
    """
    from lifecycle_store import NodeLifecycleStore
    port = proxy_port if proxy_port is not None \
        else getattr(cfg, "prod_mihomo_port", 7890)
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    records = lc.all()
    if node_ids is not None:
        records = {k: v for k, v in records.items() if k in node_ids}
    results = {}
    for nid, rec in records.items():
        scores = {}
        for name, url in SERVICE_TARGETS.items():
            if probe_fn is not None:
                score = probe_fn(url)
            else:
                if _proxy_available(port):
                    score = _proxy_probe(url, port)
                else:
                    log.info("mihomo proxy %d not available; skip probe %s",
                             port, name)
                    score = None
            if score is not None:
                scores[name] = score
        if scores:
            rec["probe_scores"] = scores
            lc.upsert(nid, rec)
            results[nid] = scores
    lc.save()
    return results


def _proxy_available(port):
    """检查本地代理端口是否可连（探测经 CONNECT 隧道）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()
