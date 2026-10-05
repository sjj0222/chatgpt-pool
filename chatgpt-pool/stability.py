"""稳定性复测（§20 / P1-02 / P1-09 / P1-10）。

- 固定 3 次（不允许 4/5/N 次）。
- 每次复测 = 重新执行完整 STRICT_PASS（P1-02），不是轻量可达。
- ≥2 次成功 → STABILITY_PASS；1/0 次 → STABILITY_FAIL。
"""
import logging
import time

log = logging.getLogger("pool.stability")


def run_stability(check_fn, cfg):
    """check_fn() 返回 run_strict_check 的 dict；返回 (results, passed)。"""
    results = []
    for i in range(cfg.stability_attempts):
        if i > 0 and cfg.stability_interval > 0:
            time.sleep(cfg.stability_interval)
        r = check_fn()
        results.append(bool(r.get("strict_pass")))
    passed = sum(results) >= cfg.stability_min_pass
    return results, passed
