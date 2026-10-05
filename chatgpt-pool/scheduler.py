"""定时刷新（§26：默认 7200 秒）。

刷新失败/异常 → 保留旧池；只记录，不清空。
"""
import logging
import time

import pipeline

log = logging.getLogger("pool.scheduler")


def refresh_loop(cfg, stop_event=None):
    while True:
        try:
            summary = pipeline.run_refresh(cfg)
            log.info("refresh done: published=%s reason=%s pool=%s",
                     summary.get("published"), summary.get("reason"),
                     summary.get("new_pool_count"))
        except Exception:
            log.exception("refresh crashed; old pool preserved")
        if stop_event is not None:
            if stop_event.wait(cfg.refresh_interval):
                break
        else:
            time.sleep(cfg.refresh_interval)
