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
