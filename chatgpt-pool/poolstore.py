"""存储层（§24 / §27 / P1-05）。

- RAW / normalized / results / final 分离，检测结果永不覆盖 RAW。
- 被淘汰节点也保留检测结果与失败原因（§24）。
- 原子替换：chatgpt.yaml.tmp → 验证 → os.replace（§27 / P1-05）。
"""
import json
import logging
import os

import yaml_lite

log = logging.getLogger("pool.store")


def ensure_dirs(cfg):
    for name in ("raw", "normalized", "results", "final", "logs", "run",
                 "state"):
        os.makedirs(cfg.sub(name), exist_ok=True)


def write_raw(cfg, round_id, items):
    path = cfg.sub(f"raw/{round_id}.txt")
    with open(path, "w", encoding="utf-8") as f:
        for src, text in items:
            f.write(f"# source: {src}\n")
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    return path


def _jsonl(path, objects):
    with open(path, "w", encoding="utf-8") as f:
        for o in objects:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


def write_normalized(cfg, round_id, nodes):
    path = cfg.sub(f"normalized/{round_id}.jsonl")
    _jsonl(path, [n.to_dict() for n in nodes])
    return path


def write_results(cfg, round_id, nodes):
    """全部节点（含 REJECTED）的检测结果与失败原因都保留。"""
    path = cfg.sub(f"results/{round_id}.jsonl")
    _jsonl(path, [n.to_dict() for n in nodes])
    return path


def write_final(cfg, round_id, nodes):
    path = cfg.sub(f"final/{round_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([n.to_dict() for n in nodes], f, ensure_ascii=False, indent=2)
    return path


def write_round_log(cfg, round_id, summary):
    path = cfg.sub(f"state/{round_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def read_pool_text(cfg):
    p = cfg.pool_path
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def read_latest_final(cfg):
    """读取最近一轮 final 目录中的最终节点列表（无则返回 []）。"""
    final_dir = cfg.sub("final")
    if not os.path.isdir(final_dir):
        return []
    for fn in reversed(sorted(os.listdir(final_dir))):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(final_dir, fn), "r", encoding="utf-8") as f:
            nodes = json.load(f)
        if nodes:
            return nodes
    return []


def atomic_write_pool(cfg, text):
    """tmp 写入 → 校验 → os.replace；绝不先删旧池再生成（§27 / P1-05）。"""
    if not yaml_lite.validate_subscription(text):
        raise ValueError("subscription validation failed; refusing to publish")
    tmp = cfg.tmp_pool_path
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, cfg.pool_path)
    return cfg.pool_path


def emit_and_publish(cfg, nodes):
    """把候选最终池序列化为订阅并原子发布；失败抛异常（调用方保留旧池）。"""
    text = yaml_lite.emit_subscription([n.to_dict() for n in nodes])
    return atomic_write_pool(cfg, text)
