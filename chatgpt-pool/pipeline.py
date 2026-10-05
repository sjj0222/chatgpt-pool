"""刷新编排（§26 / §38 + P1-05 / P1-12 / P1-13 / P1-14）。

系统级失败（国家列表不可用 / 全部源失败 / 内部故障）→ 中止本轮并保留旧池；
节点级失败 → 只淘汰该节点，保留结果与失败原因（§18 / §24 / §31）。
发布唯一出口：new_pool 非空 且 完整验证通过 → 原子替换（P1-13）。
"""
import concurrent.futures
import datetime
import logging
import os

from models import ChatgptStatus, FailureReason, Stage
import chatgpt_check
import countries
import dedup as dedup_mod
import egress
import engine as engine_mod
import fetchers
import parser as parser_mod
import poolstore
import stability

log = logging.getLogger("pool.pipeline")


def _reject(node, reason):
    node.stage = Stage.REJECTED.value
    node.failure_reason = (reason.value if isinstance(reason, FailureReason)
                           else str(reason))


def process_node(cfg, node, ctx, round_id):
    """单个节点的完整状态机（§38 / P1-14 节点部分）。"""
    node.round_id = round_id
    node.stage = Stage.PARSED.value
    eng = ctx["engine_factory"](node)
    try:
        try:
            port = eng.start(node.to_dict(), ctx["alloc_port"]())
        except engine_mod.ProxyStartError:
            _reject(node, FailureReason.PROXY_START_FAIL)
            return
        except Exception:
            log.exception("engine start crashed")
            _reject(node, FailureReason.INTERNAL_ERROR)
            return
        try:
            probe = ctx["probe_factory"](port)

            ip, iso, reason = egress.get_egress(probe, cfg)
            if reason:
                _reject(node, reason)
                return
            if not iso:
                _reject(node, FailureReason.NO_EGRESS_IP)
                return
            node.egress_ip = ip
            node.stage = Stage.EGRESS_OK.value

            # §5：只认真实出口 IP 对应的国家。
            if iso not in ctx["countries"]:
                _reject(node, FailureReason.UNSUPPORTED_COUNTRY)
                return
            node.country = iso
            node.stage = Stage.COUNTRY_OK.value

            res = chatgpt_check.run_strict_check(node, probe, cfg)
            node.chatgpt_status = res["status"]
            node.check_detail = res["detail"]
            node.stage = Stage.CHATGPT_CHECKED.value
            if not res["strict_pass"]:
                _reject(node, res["failure_reason"] or FailureReason.STRICT_CHECK_FAIL)
                return

            sres, s_pass = stability.run_stability(
                lambda: chatgpt_check.run_strict_check(node, probe, cfg), cfg)
            node.stability_results = sres
            node.stability_pass = s_pass
            if not s_pass:
                _reject(node, FailureReason.STABILITY_FAIL)
                return

            node.stage = Stage.FINAL.value
        except Exception:
            log.exception("node processing crashed")
            _reject(node, FailureReason.INTERNAL_ERROR)
    finally:
        try:
            eng.stop()
        except Exception:
            pass


def _process_all(cfg, nodes, ctx, round_id):
    results = list(nodes)
    if cfg.concurrency <= 1 or len(nodes) <= 1:
        for n in results:
            process_node(cfg, n, ctx, round_id)
        return results
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg.concurrency) as ex:
        list(ex.map(lambda n: process_node(cfg, n, ctx, round_id), results))
    return results


def _default_engine_factory(cfg):
    def factory(node):
        rundir = cfg.sub(f"run/engines/{node.node_id}")
        logfile = cfg.sub(f"logs/engine_{node.node_id}.log")
        return engine_mod.MihomoEngine(cfg, rundir, logfile)
    return factory


def _default_alloc(cfg):
    rng = getattr(cfg, "prod_engine_port_range", [18000, 18699])
    return lambda: engine_mod.allocate_port(*rng)


def run_refresh(cfg, inject=None):
    inject = inject or {}
    round_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    poolstore.ensure_dirs(cfg)
    summary = {"round_id": round_id, "published": False, "reason": None,
               "bootstrap_failed": False, "new_pool_count": 0,
               "total": 0, "invalid": 0, "dupes": 0, "final_nodes": []}

    # 1) 获取节点（§26 第一步）；注入的 fetchers 可以是列表或工厂函数
    raw_fetchers = inject.get("fetchers")
    if callable(raw_fetchers):
        raw_fetchers = raw_fetchers()
    raw_items = fetchers.fetch_sources(cfg, raw_fetchers)
    if not raw_items:
        summary["reason"] = "INTERNAL_ERROR: all node sources failed"
        return _finish_failed(cfg, round_id, summary)
    poolstore.write_raw(cfg, round_id, raw_items)

    # 2) 解析 + 去重（§23）
    nodes, invalid = parser_mod.parse_all(raw_items)
    nodes, dupes = dedup_mod.dedup(nodes)
    nodes = nodes[: cfg.max_nodes_per_round]
    summary.update(total=len(nodes), invalid=invalid, dupes=dupes)
    poolstore.write_normalized(cfg, round_id, nodes)

    # 3) 国家列表（系统级，§6 / P1-12）
    resolver = inject.get("country_resolver") or countries.resolve_country_list
    clist, csrc, cwarn = resolver(cfg)
    if not clist:
        summary["reason"] = "COUNTRY_LIST_UNAVAILABLE"
        return _finish_failed(cfg, round_id, summary)
    if cwarn:
        log.warning("%s (country source=%s)", cwarn, csrc)

    ctx = {
        "engine_factory": inject.get("engine_factory") or _default_engine_factory(cfg),
        "probe_factory": inject.get("probe_factory"),
        "alloc_port": inject.get("alloc_port") or _default_alloc(cfg),
        "countries": set(clist),
    }
    if ctx["probe_factory"] is None:
        from probe import ProxyProbe
        ctx["probe_factory"] = lambda port: ProxyProbe(cfg, port)

    # 4) 逐节点状态机（§38）
    results = _process_all(cfg, nodes, ctx, round_id)
    poolstore.write_results(cfg, round_id, results)

    # 5) 候选最终池（§19 / §22 / P1-11）
    new_pool = [n for n in results
                if n.stage == Stage.FINAL.value
                and n.chatgpt_status == ChatgptStatus.STRICT_PASS.value
                and n.stability_pass]
    summary["new_pool_count"] = len(new_pool)
    summary["final_nodes"] = [n.node_id for n in new_pool]
    poolstore.write_final(cfg, round_id, new_pool)

    # 6) 发布门（P1-13：非空 + 验证通过才原子替换；否则保留旧池）
    published = False
    if new_pool:
        try:
            poolstore.emit_and_publish(cfg, new_pool)
            published = True
            summary["reason"] = None
        except Exception as e:
            log.exception("publish failed; old pool preserved")
            summary["reason"] = f"VALIDATION_FAIL: {e}"
    else:
        summary["reason"] = "EMPTY_POOL"
    summary["published"] = published

    return _finish(cfg, round_id, summary)


def _finish_failed(cfg, round_id, summary):
    if not os.path.exists(cfg.pool_path):
        summary["bootstrap_failed"] = True
        log.warning("BOOTSTRAP_FAILED: first run failed, no empty yaml written")
    poolstore.write_round_log(cfg, round_id, summary)
    return summary


def _finish(cfg, round_id, summary):
    if not summary["published"] and not os.path.exists(cfg.pool_path):
        summary["bootstrap_failed"] = True
        log.warning("BOOTSTRAP_FAILED: no pool produced, no empty yaml written")
    poolstore.write_round_log(cfg, round_id, summary)
    return summary
