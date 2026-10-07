"""定时刷新（§26：默认 7200 秒）。

刷新失败/异常 → 保留旧池；只记录，不清空。
"""
import logging
import time

import pipeline

log = logging.getLogger("pool.scheduler")


def refresh_loop(cfg, stop_event=None, on_published=None):
    """循环刷新；每轮发布成功后调用 on_published(summary)（如热重载生产 Mihomo）。"""
    while True:
        try:
            summary = pipeline.run_refresh(cfg)
            log.info("refresh done: published=%s reason=%s pool=%s",
                     summary.get("published"), summary.get("reason"),
                     summary.get("new_pool_count"))
            if summary.get("published") and on_published is not None:
                try:
                    on_published(summary)
                except Exception:
                    log.exception("on_published callback failed")
        except Exception:
            log.exception("refresh crashed; old pool preserved")
        if stop_event is not None:
            if stop_event.wait(cfg.refresh_interval):
                break
        else:
            time.sleep(cfg.refresh_interval)


# ============ V0.3 Phase 7：任务调度/锁/崩溃恢复/备份/资源限制 ============
# 上方 refresh_loop 是 V5.2/§26 既有循环（保留不变）；下方为新增能力。

import json
import os
import shutil

# §23 默认周期（秒）；V0.3.1 增加 health_check / backup 任务（0.3.1-2）
SCHEDULE = {
    "refresh": 6 * 3600,     # 6h
    "probe": 30 * 60,        # 30min
    "score": 1 * 3600,       # 1h
    "cleanup": 24 * 3600,    # 24h
    "health_check": 3600,     # 1h（V0.4.1：runtime_report 每小时更新）
    "backup": 12 * 3600,     # 12h（V0.3.1：自动备份）
}
# §22 探测等级
PROBE_LEVELS = {
    "refresh": 1,   # L1 TCP 高频
    "score": 2,     # L2 质量中频
    "probe": 3,     # L3 服务低频
}
HISTORY_SIZE_LIMIT = 500 * 1024 * 1024   # §33：500MB
COMPRESS_SCORE_HISTORY = 10
COMPRESS_ATTEMPTS = 50


def _state_path(cfg):
    return cfg.sub("run/scheduler_state.json")


class Lock:
    """§32：O_EXCL 文件锁（refresh.lock / probe.lock）。

    非阻塞获取；陈旧锁（进程已死）由崩溃恢复清理。
    """

    def __init__(self, cfg, name):
        self.path = cfg.sub("run/locks/%s.lock" % name)
        self.acquired = False

    def acquire(self, timeout=0):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + timeout
        while True:
            try:
                fd = os.open(self.path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                self.acquired = True
                return True
            except FileExistsError:
                if timeout <= 0 or time.time() >= deadline:
                    return False
                time.sleep(0.5)

    def release(self):
        if self.acquired and os.path.isfile(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _load_state(cfg):
    p = _state_path(cfg)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        log.warning("scheduler_state unreadable, reset")
        return {}


def _save_state(cfg, state):
    os.makedirs(os.path.dirname(_state_path(cfg)), exist_ok=True)
    tmp = _state_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _state_path(cfg))


def due_tasks(cfg, now=None):
    """返回到期任务名列表（§23）。"""
    now = now if now is not None else int(time.time())
    state = _load_state(cfg)
    last = state.get("last_run", {})
    out = []
    for name, period in SCHEDULE.items():
        last_ts = int(last.get(name, 0) or 0)
        if now - last_ts >= period:
            out.append(name)
    return out


def tick(cfg, runner, now=None):
    """执行全部到期任务（§23）。runner: dict name→callable。

    V0.3.1（0.3.1-2）：refresh 用 Lock 保护（禁止 refresh/refresh 并发）；
    单任务异常不影响其他任务；执行后更新 last_run 并持久化。
    """
    now = now if now is not None else int(time.time())
    state = _load_state(cfg)
    last = state.setdefault("last_run", {})
    ran = {}
    for name in due_tasks(cfg, now=now):
        fn = runner.get(name)
        if fn is None:
            log.warning("no runner for task %s", name)
            continue
        try:
            if name in ("refresh", "probe", "backup", "cleanup"):
                with Lock(cfg, name):
                    fn()
            else:
                fn()
            last[name] = now
            ran[name] = True
        except Exception:
            log.exception("scheduler task %s failed; next tick retries", name)
            ran[name] = False
    state["last_run"] = last
    state["last_tick"] = now
    _save_state(cfg, state)
    return ran


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def stale_locks(cfg):
    """崩溃恢复（§31）：找出残留锁并清理（记录进程 PID 供诊断）。"""
    lock_dir = cfg.sub("run/locks")
    if not os.path.isdir(lock_dir):
        return []
    stale = []
    for name in os.listdir(lock_dir):
        p = os.path.join(lock_dir, name)
        if not name.endswith(".lock"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                pid = f.read().strip()
        except OSError:
            pid = "?"
        # 锁文件存在但持有进程已不存在 → 陈旧锁
        if not _pid_alive(pid):
            stale.append({"lock": name, "pid": pid})
            try:
                os.remove(p)
            except OSError:
                pass
    return stale


def crash_recover(cfg):
    """§31：启动恢复检查——上次调度状态、当前池、调度任务、陈旧锁。"""
    state = _load_state(cfg)
    active_pool = cfg.sub("run/final/current.json")
    recover = {
        "scheduler_state_exists": bool(state),
        "last_tick": state.get("last_tick"),
        "active_pool_exists": os.path.isfile(active_pool),
        "stale_locks": stale_locks(cfg),
        "next_due": due_tasks(cfg),
        "recovered": False,
    }
    # 进程被杀/重启后：陈旧锁已清；调度任务按到期自动续跑
    if recover["stale_locks"] or state:
        recover["recovered"] = True
    return recover


def backup(cfg, dest_dir=None):
    """§34：备份 config/history/score/source 快照到 backup/{ts}/。"""
    ts = time.strftime("%Y%m%d%H%M%S")
    base = dest_dir or cfg.sub("backup/%s" % ts)
    os.makedirs(base, exist_ok=True)
    targets = [
        ("state/node_lifecycle.json", "lifecycle.json"),
        ("state/source_lifecycle.json", "source_lifecycle.json"),
        ("state/node_history.json", "node_history.json"),
        ("run/final/current.json", "current_pool.json"),
        ("run/source_health.json", "source_health.json"),
    ]
    for src, dst in targets:
        sp = cfg.sub(src)
        if os.path.isfile(sp):
            shutil.copy2(sp, os.path.join(base, dst))
    return base


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


def enforce_size_limit(cfg, limit=HISTORY_SIZE_LIMIT):
    """§33：历史数据（state/）> 500MB → 压缩（截断 score_history/attempts）。"""
    state_dir = cfg.sub("state")
    if not os.path.isdir(state_dir):
        return {"size_bytes": 0, "compressed": False}
    size = _dir_size(state_dir)
    if size <= limit:
        return {"size_bytes": size, "compressed": False}
    from lifecycle_store import NodeLifecycleStore
    lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
    for rec in lc.all().values():
        if rec.get("score_history") and len(rec["score_history"]) \
                > COMPRESS_SCORE_HISTORY:
            rec["score_history"] = rec["score_history"][-COMPRESS_SCORE_HISTORY:]
        if rec.get("recent_attempts") and len(rec["recent_attempts"]) \
                > COMPRESS_ATTEMPTS:
            rec["recent_attempts"] = rec["recent_attempts"][-COMPRESS_ATTEMPTS:]
    lc.save()
    return {"size_bytes": size, "compressed": True, "limit_bytes": limit}


def schedule_status(cfg):
    """调度状态（供 CLI status）。"""
    state = _load_state(cfg)
    last = state.get("last_run", {})
    now = int(time.time())
    out = {"schedule": {}, "last_tick": state.get("last_tick")}
    for name, period in SCHEDULE.items():
        last_ts = int(last.get(name, 0) or 0)
        out["schedule"][name] = {
            "period_s": period,
            "level": PROBE_LEVELS.get(name),
            "last_run": last_ts,
            "next_in_s": max(0, period - (now - last_ts)),
        }
    return out
