"""节点源抓取（§26 第一步）。

来源是配置项（node_source_urls），支持 http(s) URL 与本地文件路径。
全部来源失败 → 本轮视为系统级失败（保留旧池）。
"""
import logging
import os

log = logging.getLogger("pool.fetchers")


def _http_text(url, cfg):
    from probe import NoProxyProbe, ProbeError
    probe = NoProxyProbe(cfg, read_limit=4 * 1024 * 1024)
    r = probe(url)
    if r.status is None or r.status >= 400:
        raise RuntimeError(f"http {r.status} for {url}")
    return r.body


def _local_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def default_fetchers(cfg):
    out = []
    for url in cfg.node_source_urls:
        if url.startswith(("http://", "https://")):
            out.append((url, lambda u=url: _http_text(u, cfg)))
        elif os.path.isfile(os.path.expanduser(url)):
            p = os.path.expanduser(url)
            out.append((url, lambda p=p: _local_text(p)))
    return out


def fetch_sources(cfg, fetchers=None):
    """返回 [(source_name, text), ...]；全部失败返回 []。

    fetchers 元素为 (name, fn)：fn 可以是可调用对象或直接的字面量文本
    （测试注入用字面量文本即可）。
    """
    items = []
    for name, fn in (fetchers or default_fetchers(cfg)):
        try:
            text = fn() if callable(fn) else str(fn)
            if text and text.strip():
                items.append((name, text))
                log.info("fetched %s (%d bytes)", name, len(text))
        except Exception as e:
            log.warning("fetch %s failed: %s", name, e)
    return items
