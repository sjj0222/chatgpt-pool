"""存储层（§24 / §27 / P1-05）。

- RAW / normalized / results / final 分离，检测结果永不覆盖 RAW。
- 被淘汰节点也保留检测结果与失败原因（§24）。
- 原子替换：chatgpt.yaml.tmp → 验证 → os.replace（§27 / P1-05）。
"""
import datetime
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


def write_failed_round(cfg, round_id, summary):
    """失败轮次写入 final/failed/{round_id}.json（V0.2.1 P0-02）。

    仅失败轮次落 failed/，绝不覆盖 final/current.json（当前有效池）。
    """
    path = cfg.sub(f"final/failed/{round_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path


def write_current_final(cfg, round_id, nodes):
    """成功发布后更新 final/current.json（原子 tmp→replace，P0-02）。

    结构：{"round_id": ..., "published_at": ..., "nodes": [node_dict, ...]}
    只允许 final_nodes > 0 且全部验证通过后调用（P0-02 / P1-05 / P1-13）。
    """
    os.makedirs(cfg.sub("final"), exist_ok=True)
    payload = {
        "round_id": round_id,
        "published_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "nodes": [n.to_dict() if hasattr(n, "to_dict") else n for n in nodes],
    }
    tmp = cfg.sub("final/current.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg.sub("final/current.json"))
    return cfg.sub("final/current.json")


def read_current_final(cfg):
    """读取当前有效最终池（无则返回 None）。"""
    p = cfg.sub("final/current.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return {"round_id": data.get("round_id", ""),
            "nodes": data.get("nodes", [])}


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
    """读取 final/current.json 的节点列表（无则返回 []，V0.2.1 P0-02）。"""
    cur = read_current_final(cfg)
    return cur["nodes"] if cur else []


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
