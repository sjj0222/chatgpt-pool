"""V0.3.1 运行时监控（Phase 0.3.1-3 / 0.3.1-4）。

- 资源监控：memory（进程 RSS）/ disk（工作目录）/ log size / data size。
- 日志上限：超过上限触发 rotate（禁止无限增长，§33 精神）。
- runtime_report.json：uptime / pool_size / active_nodes / score_average /
  source_health / error_count——由 health_check 任务周期更新（7 天观察数据）。
"""
import json
import logging
import os
import time

log = logging.getLogger("pool.runtime")

LOG_ROTATE_BYTES = 50 * 1024 * 1024     # 日志单文件上限 50MB
REPORT_PATH = "run/runtime_report.json"

try:
    import resource
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False


def _mem_rss():
    """进程常驻内存（Linux /proc/self/statm；不可用时 0）。"""
    try:
        with open("/proc/self/statm", "r") as f:
            pages = int(f.read().split()[1])
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        return pages * page_kb          # KB
    except (OSError, ValueError, IndexError):
        return 0


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total


def rotate_if_needed(path, limit=LOG_ROTATE_BYTES):
    """日志超过上限 → 轮转（保留 .1 后缀，禁止无限增长）。"""
    if not os.path.isfile(path):
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= limit:
        return False
    bak = path + ".1"
    try:
        if os.path.isfile(bak):
            os.remove(bak)
        os.rename(path, bak)
        return True
    except OSError as e:
        log.warning("log rotate failed: %s", e)
        return False


def rotate_all_logs(cfg, limit=LOG_ROTATE_BYTES):
    """对 logs/*.log 与 run/detect.log 全部轮转。"""
    rotated = []
    for root in (cfg.sub("logs"), cfg.sub("run")):
        if not os.path.isdir(root):
            continue
        for f in os.listdir(root):
            if f.endswith(".log"):
                if rotate_if_needed(os.path.join(root, f), limit=limit):
                    rotated.append(os.path.join(root, f))
    return rotated


def resource_stats(cfg):
    """V0.3.1-3：memory/disk/log/data 四项监控。"""
    return {
        "memory_rss_kb": _mem_rss(),
        "disk_workdir_bytes": _dir_size(cfg.base_dir),
        "log_size_bytes": _dir_size(cfg.sub("logs")),
        "data_size_bytes": _dir_size(cfg.sub("state"))
                           + _dir_size(cfg.sub("run")),
        "log_limit_bytes": LOG_ROTATE_BYTES,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_runtime_report(cfg, uptime_s=None):
    """V0.3.1-4 / V0.4.1 Phase 2：生成 runtime_report.json（持续观察数据）。"""
    pool = _read_json(cfg.sub("run/final/current.json"))
    rs = _read_json(cfg.sub("run/refresh_state.json"))
    lc = _read_json(cfg.sub("state/node_lifecycle.json")) or {}
    nodes = lc.get("nodes", {})
    sh = _read_json(cfg.sub("run/source_health.json")) or {}
    sc = _read_json(cfg.sub("state/source_lifecycle.json")) or {}
    ok = sum(int(n.get("success_count", 0) or 0) for n in nodes.values())
    fail = sum(int(n.get("fail_count", 0) or 0) for n in nodes.values())
    success_rate = (round(ok / (ok + fail), 4) if ok + fail > 0 else 0.0)
    # 平均分（仅统计 ACTIVE/STANDBY 有效节点）
    scored = [n.get("score", {}).get("overall", 0.0) for n in nodes.values()
              if n.get("score") and n.get("life_state")
              in ("ACTIVE", "STANDBY")]
    score_avg = (round(sum(scored) / len(scored), 1) if scored else 0.0)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uptime_s": uptime_s if uptime_s is not None else 0,
        "start_time": (rs or {}).get("first_start")
                      or (rs or {}).get("last_refresh"),
        "pool_size": len(pool.get("nodes", [])) if pool else 0,
        "active_nodes": sum(1 for n in nodes.values()
                            if n.get("life_state") == "ACTIVE"),
        "success_rate": success_rate,
        "average_score": score_avg,
        "source_count": len(sh) if sh else len(sc.get("sources", {})),
        "source_health": {"count": len(sh),
                          "alive_total": sum(int(v.get("alive", 0) or 0)
                                             for v in sh.values())},
        "error_count": 1 if (rs or {}).get("last_failure_reason") else 0,
        "last_refresh": (rs or {}).get("last_refresh"),
        "last_failure_reason": (rs or {}).get("last_failure_reason"),
    }


def write_runtime_report(cfg, report=None, uptime_s=None):
    """写 run/runtime_report.json + 归档历史（不覆盖历史，Phase 2）。"""
    report = report or build_runtime_report(cfg, uptime_s=uptime_s)
    path = cfg.sub(REPORT_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    # 历史归档（保留最近 168 条 ≈ 7 天每小时）
    hist_dir = cfg.sub("run/history/runtime")
    os.makedirs(hist_dir, exist_ok=True)
    name = (time.strftime("%Y%m%dT%H%M%S") + "_"
            + ("%06d" % (time.time_ns() % 1000000)) + ".json")
    try:
        with open(os.path.join(hist_dir, name), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        files = sorted(os.listdir(hist_dir))
        for old in files[:-168]:
            os.remove(os.path.join(hist_dir, old))
    except OSError as e:
        log.warning("runtime history archive failed: %s", e)
    return path
