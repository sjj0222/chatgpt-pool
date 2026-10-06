"""刷新编排（§26 / §38 + P1-05 / P1-12 / P1-13 / P1-14）。

系统级失败（国家列表不可用 / 全部源失败 / 内部故障）→ 中止本轮并保留旧池；
节点级失败 → 只淘汰该节点，保留结果与失败原因（§18 / §24 / §31）。
发布唯一出口：new_pool 非空 且 完整验证通过 → 原子替换（P1-13）。

V0.2.1 hardening：
- P0-01：reject_summary 由本轮真实结果统计（禁止猜测/二次扫描）。
- P0-02：成功发布 → run/final/current.json（原子替换）；失败轮次 → run/failed/{round_id}.json，
  绝不覆盖 current.json。
- P0-05：refresh 结束输出完整诊断（published / pool_size / round_id / reason /
  reject_summary）。
- P1-01/02/03：节点历史（state/node_history.json）跨轮保留，健康度
  GOOD/DEGRADED/BAD 派生，评分仅用于排序（不改准入与淘汰规则）。
- P1-04：REJECTED 明细写 logs/reject.log。

V0.2.2 Diagnostic Enhancement：
- P0-01：失败分层——failure_stage（哪一步）+ failure_detail.sub_reason（为什么，
  14 细分枚举）+ detail 上下文；顶层 failure_reason（§16）不变。
- P0-02：失败节点明细 run/failed/nodes/{round_id}.jsonl（≤1000，P1-01）。
- P0-03：run/detect.log 分阶段检测日志（node_id STAGE START/END result reason）。
- P0-04：reject_summary_by_protocol（判断协议整体失效）。
- P0-05：run/source_health.json（源健康：fetch/parsed/valid/alive/alive_rate）。
- P1-02：run/refresh_state.json（status 读取 last_refresh/last_success/
  last_failure_reason/current_pool_age）。

V0.2.2.1 Diagnostic Fix：
- 诊断产物路径统一到 run/（run/final/current.json、run/failed/{round_id}.json、
  run/failed/nodes/{round_id}.jsonl、run/source_health.json、run/detect.log、
  run/refresh_state.json）；detect.log 追加轮次摘要
  （round_id / Detection Result / Stage / Reason / Protocol）。
- P1-03：refresh 输出分阶段统计（stage_summary + reject_summary_detailed）。
"""
import concurrent.futures
import datetime
import json
import logging
import os
import time

from models import (ChatgptStatus, DiagnosticStage, FailureReason, SubReason,
                    Stage)
import chatgpt_check
import countries
import dedup as dedup_mod
import egress
import engine as engine_mod
import fetchers
import history as history_mod
import parser as parser_mod
import poolstore
import source_health
import stability

log = logging.getLogger("pool.pipeline")
reject_log = logging.getLogger("pool.reject")
detect_log = logging.getLogger("pool.detect")


def _diag_start(node_id, stage):
    detect_log.info("%s %s START", node_id, stage)


def _diag_end(node_id, stage, result, reason=""):
    detect_log.info("%s %s END %s%s", node_id, stage, result,
                    f" {reason}" if reason else "")


def _reject(node, reason, stage=None, sub=None, detail=None):
    """设置失败三要素：顶层 reason（§16）+ 阶段（哪一步）+ 细分/细节（为什么）。"""
    node.stage = Stage.REJECTED.value
    node.failure_reason = (reason.value if isinstance(reason, FailureReason)
                           else str(reason))
    node.failure_stage = (stage.value if isinstance(stage, DiagnosticStage)
                          else (stage or ""))
    sub_val = (sub.value if isinstance(sub, SubReason)
               else (sub or ""))
    detail = dict(detail or {})
    if sub_val:
        detail["sub_reason"] = sub_val
    node.failure_detail = detail or {}


def process_node(cfg, node, ctx, round_id, history):
    """单个节点的完整状态机（§38 / P1-14 节点部分）。"""
    node.round_id = round_id
    node.stage = Stage.PARSED.value
    t0 = time.monotonic()
    eng = None
    try:
        _diag_start(node.node_id, "PROXY")
        try:
            eng = ctx["engine_factory"](node)
        except Exception as e:
            # 引擎构造失败（如二进制缺失）按节点级失败处理，不拖垮整轮
            log.warning("engine factory failed for %s: %s", node.node_id, e)
            _reject(node, FailureReason.PROXY_START_FAIL,
                    DiagnosticStage.PROXY, SubReason.PROXY_HANDSHAKE_FAIL,
                    {"type": "engine_factory", "error": str(e)[:200]})
            _diag_end(node.node_id, "PROXY", "FAIL", node.failure_reason)
            return
        try:
            port = eng.start(node.to_dict(), ctx["alloc_port"]())
        except engine_mod.ProxyStartError as e:
            _reject(node, FailureReason.PROXY_START_FAIL,
                    DiagnosticStage.PROXY,
                    getattr(e, "sub", None) or SubReason.PROXY_HANDSHAKE_FAIL,
                    {"type": "proxy_start", "error": str(e)[:200]})
            _diag_end(node.node_id, "PROXY", "FAIL", node.failure_reason)
            return
        except Exception:
            log.exception("engine start crashed")
            _reject(node, FailureReason.INTERNAL_ERROR,
                    DiagnosticStage.PROXY, SubReason.UNKNOWN_FAIL,
                    {"type": "engine_start"})
            _diag_end(node.node_id, "PROXY", "FAIL", node.failure_reason)
            return
        _diag_end(node.node_id, "PROXY", "OK")
        try:
            probe = ctx["probe_factory"](port)

            _diag_start(node.node_id, "EGRESS")
            ip, iso, reason, sub, detail = egress.get_egress(probe, cfg)
            if reason:
                _reject(node, reason, DiagnosticStage.EGRESS, sub, detail)
                _diag_end(node.node_id, "EGRESS", "FAIL", node.failure_reason)
                return
            if not iso:
                _reject(node, FailureReason.NO_EGRESS_IP,
                        DiagnosticStage.EGRESS, SubReason.NO_EGRESS_IP,
                        {"url": cfg.geoip_url})
                _diag_end(node.node_id, "EGRESS", "FAIL", node.failure_reason)
                return
            node.egress_ip = ip
            node.stage = Stage.EGRESS_OK.value
            _diag_end(node.node_id, "EGRESS", "OK", iso)

            # §5：只认真实出口 IP 对应的国家。
            if iso not in ctx["countries"]:
                _reject(node, FailureReason.UNSUPPORTED_COUNTRY,
                        DiagnosticStage.COUNTRY, None,
                        {"country": iso, "ip": ip})
                return
            node.country = iso
            node.stage = Stage.COUNTRY_OK.value

            _diag_start(node.node_id, "CHATGPT")
            res = chatgpt_check.run_strict_check(node, probe, cfg)
            node.chatgpt_status = res["status"]
            node.check_detail = res["detail"]
            node.stage = Stage.CHATGPT_CHECKED.value
            if not res["strict_pass"]:
                # V0.2.2 阶段口径：传输层失败（TCP_*/TLS_*）→ TLS 阶段
                # （用户示例 TLS_TIMEOUT→stage TLS）；业务性失败
                # （CHATGPT_BLOCKED/CHATGPT_TIMEOUT/STRICT_CHECK_FAIL）→ CHATGPT 阶段
                sub = res["failure_sub"]
                subv = sub.value if isinstance(sub, SubReason) else str(sub or "")
                if subv.startswith(("TCP_", "TLS_", "PROXY_")):
                    chk_stage = DiagnosticStage.TLS
                else:
                    chk_stage = DiagnosticStage.CHATGPT
                _reject(node,
                        res["failure_reason"] or FailureReason.STRICT_CHECK_FAIL,
                        chk_stage, res["failure_sub"],
                        res["failure_detail"])
                _diag_end(node.node_id, "CHATGPT", "FAIL", node.failure_reason)
                return
            _diag_end(node.node_id, "CHATGPT", "OK", res["status"])

            _diag_start(node.node_id, "STABILITY")
            sres, s_pass = stability.run_stability(
                lambda: chatgpt_check.run_strict_check(node, probe, cfg), cfg)
            node.stability_results = sres
            node.stability_pass = s_pass
            if not s_pass:
                _reject(node, FailureReason.STABILITY_FAIL,
                        DiagnosticStage.STABILITY, None,
                        {"results": [bool(r) for r in sres],
                         "attempts": cfg.stability_attempts})
                _diag_end(node.node_id, "STABILITY", "FAIL", node.failure_reason)
                return
            _diag_end(node.node_id, "STABILITY", "OK", f"{sum(sres)}/{len(sres)}")

            node.stage = Stage.FINAL.value
        except Exception:
            log.exception("node processing crashed")
            _reject(node, FailureReason.INTERNAL_ERROR,
                    DiagnosticStage.UNKNOWN, SubReason.UNKNOWN_FAIL,
                    {"type": "processing"})
    finally:
        if eng is not None:
            try:
                eng.stop()
            except Exception:
                pass
        _record_node(node, history, time.monotonic() - t0)


def _record_node(node, history, latency):
    """写入跨轮历史（P1-01/02）；REJECTED 明细进 reject.log（P1-04）。

    历史永不因单轮失败删除：失败同样 record，供健康度派生与恢复。
    """
    ok = node.stage == Stage.FINAL.value
    history.record(node.node_id, ok=ok, latency=round(latency, 3),
                   ip=node.egress_ip, country=node.country,
                   reason=node.failure_reason)
    if not ok:
        reject_log.info("REJECT round=%s node=%s reason=%s ip=%s country=%s",
                        node.round_id, node.node_id,
                        node.failure_reason or "UNKNOWN",
                        node.egress_ip or "-", node.country or "-")


def _process_all(cfg, nodes, ctx, round_id, history):
    for n in nodes:
        history.mark_checking(n.node_id)   # P1-01：CHECKING
    results = list(nodes)
    if cfg.concurrency <= 1 or len(nodes) <= 1:
        for n in results:
            process_node(cfg, n, ctx, round_id, history)
        return results
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg.concurrency) as ex:
        list(ex.map(lambda n: process_node(cfg, n, ctx, round_id, history),
                    results))
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


def _build_reject_summary(results):
    """P0-01：按本轮真实结果统计 REJECTED 的 failure_reason（顶层 §16）。"""
    counter = {}
    for n in results:
        if n.stage == Stage.REJECTED.value and n.failure_reason:
            counter[n.failure_reason] = counter.get(n.failure_reason, 0) + 1
    return counter


def _build_reject_summary_detailed(results):
    """V0.2.2 P0-01：按细分原因（SubReason）统计；无细分回退到顶层。"""
    counter = {}
    for n in results:
        if n.stage != Stage.REJECTED.value:
            continue
        sub = (n.failure_detail or {}).get("sub_reason") or n.failure_reason
        counter[sub] = counter.get(sub, 0) + 1
    return counter


def _build_stage_summary(results):
    """V0.2.2 P1-03：按失败阶段（DiagnosticStage）统计。"""
    counter = {}
    for n in results:
        if n.stage != Stage.REJECTED.value:
            continue
        st = n.failure_stage or "UNKNOWN"
        counter[st] = counter.get(st, 0) + 1
    return counter


def _build_reject_summary_by_protocol(results):
    """V0.2.2 P0-04：按协议统计 total / failed / 各细分原因数。

    用于判断"协议整体失效"（如某协议 total=failed 且集中于一个细分原因）。
    """
    by_proto = {}
    for n in results:
        rec = by_proto.setdefault(n.protocol or "unknown",
                                  {"total": 0, "failed": 0})
        rec["total"] += 1
        if n.stage == Stage.REJECTED.value:
            rec["failed"] += 1
            sub = (n.failure_detail or {}).get("sub_reason") or n.failure_reason
            rec[sub] = rec.get(sub, 0) + 1
    return by_proto


def _collect_source_health(cfg, round_id, raw_items, parsed_nodes,
                           valid_nodes, final_nodes):
    """V0.2.2 P0-05：更新 run/source_health.json（V0.2.2.1 路径）。"""
    parsed_by_source = {}
    for n in parsed_nodes:
        parsed_by_source[n.source] = parsed_by_source.get(n.source, 0) + 1
    valid_by_source = {}
    for n in valid_nodes:
        valid_by_source[n.source] = valid_by_source.get(n.source, 0) + 1
    alive_by_source = {}
    for n in final_nodes:
        alive_by_source[n.source] = alive_by_source.get(n.source, 0) + 1
    raw_sources = {src for src, _ in raw_items}
    sh = source_health.SourceHealth(cfg.sub("run/source_health.json"))
    return sh.update_round(cfg.node_source_urls, raw_sources,
                           parsed_by_source, valid_by_source,
                           alive_by_source, round_id)


def run_refresh(cfg, inject=None):
    inject = inject or {}
    round_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    poolstore.ensure_dirs(cfg)
    summary = {"round_id": round_id, "published": False, "reason": None,
               "bootstrap_failed": False, "new_pool_count": 0,
               "pool_size": 0, "total": 0, "invalid": 0, "dupes": 0,
               "final_nodes": [], "reject_summary": {},
               "reject_summary_detailed": {}, "reject_summary_by_protocol": {},
               "stage_summary": {}, "failed_node_count": 0}
    history = history_mod.NodeHistory(cfg.sub("state/node_history.json"))

    # 1) 获取节点（§26 第一步）；注入的 fetchers 可以是列表或工厂函数
    raw_fetchers = inject.get("fetchers")
    if callable(raw_fetchers):
        raw_fetchers = raw_fetchers()
    raw_items = fetchers.fetch_sources(cfg, raw_fetchers)
    if not raw_items:
        summary["reason"] = "INTERNAL_ERROR: all node sources failed"
        return _finish_failed(cfg, round_id, summary, history)
    poolstore.write_raw(cfg, round_id, raw_items)

    # 2) 解析 + 去重（§23）
    parsed_nodes, invalid = parser_mod.parse_all(raw_items)
    nodes, dupes = dedup_mod.dedup(parsed_nodes)
    nodes = nodes[: cfg.max_nodes_per_round]
    summary.update(total=len(nodes), invalid=invalid, dupes=dupes)
    poolstore.write_normalized(cfg, round_id, nodes)

    # 3) 国家列表（系统级，§6 / P1-12）
    resolver = inject.get("country_resolver") or countries.resolve_country_list
    clist, csrc, cwarn = resolver(cfg)
    if not clist:
        summary["reason"] = "COUNTRY_LIST_UNAVAILABLE"
        return _finish_failed(cfg, round_id, summary, history)
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
    results = _process_all(cfg, nodes, ctx, round_id, history)
    poolstore.write_results(cfg, round_id, results)
    summary["reject_summary"] = _build_reject_summary(results)
    summary["reject_summary_detailed"] = _build_reject_summary_detailed(results)
    summary["reject_summary_by_protocol"] = _build_reject_summary_by_protocol(results)
    summary["stage_summary"] = _build_stage_summary(results)
    failed_path = poolstore.write_failed_nodes(cfg, round_id, results)  # P0-02
    summary["failed_node_count"] = 0
    if failed_path:
        with open(failed_path, "r", encoding="utf-8") as f:
            summary["failed_node_count"] = sum(1 for _ in f)

    # 5) 候选最终池（§19 / §22 / P1-11）；评分仅排序，不改准入（P1-03）
    new_pool = [n for n in results
                if n.stage == Stage.FINAL.value
                and n.chatgpt_status == ChatgptStatus.STRICT_PASS.value
                and n.stability_pass]
    new_pool.sort(key=lambda n: history.score(n.node_id), reverse=True)
    summary["new_pool_count"] = len(new_pool)
    summary["pool_size"] = len(new_pool)
    summary["final_nodes"] = [n.node_id for n in new_pool]

    # P0-05：源健康统计（真实 pipeline 数据；parsed=解析成功数，valid=去重后）
    summary["source_health"] = _collect_source_health(
        cfg, round_id, raw_items, parsed_nodes, nodes, new_pool)

    # 6) 发布门（P1-13：非空 + 验证通过才原子替换；否则保留旧池）
    #    P0-02：成功 → current.json 替换；失败 → failed/{round_id}.json
    published = False
    if new_pool:
        try:
            poolstore.emit_and_publish(cfg, new_pool)
            poolstore.write_current_final(cfg, round_id, new_pool)
            published = True
            summary["reason"] = None
        except Exception as e:
            log.exception("publish failed; old pool preserved")
            summary["reason"] = f"VALIDATION_FAIL: {e}"
            poolstore.write_failed_round(cfg, round_id, summary)
    else:
        summary["reason"] = "EMPTY_POOL"
        poolstore.write_failed_round(cfg, round_id, summary)
    summary["published"] = published

    return _finish(cfg, round_id, summary, history)


def _write_detect_summary(summary):
    """V0.2.2 P0-03 + V0.2.2.1：detect.log 写入轮次摘要（round_id / Detection
    Result / Stage / Reason / Protocol）。"""
    detect_log.info("round=%s published=%s pool_size=%s reason=%s",
                    summary.get("round_id", ""),
                    summary.get("published"),
                    summary.get("pool_size", 0),
                    summary.get("reason") or "")
    st = summary.get("stage_summary", {})
    if st:
        detect_log.info("Stage: " + ", ".join(
            f"{k} {v}" for k, v in st.items()))
    detect_log.info("Reason: " + ", ".join(
        f"{k} {v}" for k, v in sorted(
            summary.get("reject_summary_detailed", {}).items(),
            key=lambda kv: -kv[1])))
    bp = summary.get("reject_summary_by_protocol", {})
    if bp:
        detect_log.info("Protocol: " + json.dumps(bp, ensure_ascii=False))
    detect_log.info("Detection Result: total=%s failed_nodes=%s",
                    summary.get("total", 0),
                    summary.get("failed_node_count", 0))


def _finish_failed(cfg, round_id, summary, history):
    poolstore.write_failed_round(cfg, round_id, summary)   # P0-02：系统级失败也留痕
    if not os.path.exists(cfg.pool_path):
        summary["bootstrap_failed"] = True
        log.warning("BOOTSTRAP_FAILED: first run failed, no empty yaml written")
    poolstore.write_round_log(cfg, round_id, summary)
    _write_detect_summary(summary)
    _write_refresh_state(cfg, summary)
    history.save()
    return summary


def _finish(cfg, round_id, summary, history):
    if not summary["published"] and not os.path.exists(cfg.pool_path):
        summary["bootstrap_failed"] = True
        log.warning("BOOTSTRAP_FAILED: no pool produced, no empty yaml written")
    poolstore.write_round_log(cfg, round_id, summary)
    _write_detect_summary(summary)
    _write_refresh_state(cfg, summary)
    history.save()
    return summary


def _write_refresh_state(cfg, summary):
    """V0.2.2 P1-02：最近刷新状态（status 读取 last_refresh/last_success/
    last_failure_reason）。V0.2.2.1：补齐 round_id/status 字段
    （{"round_id","status","published","pool_size"} 至少四项）。"""
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    state = {
        "round_id": summary.get("round_id", ""),
        "status": ("published" if summary.get("published")
                   else (summary.get("reason") or "FAILED")),
        "last_refresh": now,
        "last_round_id": summary.get("round_id", ""),
        "published": bool(summary.get("published")),
        "last_success": (summary.get("round_id", "")
                         if summary.get("published") else ""),
        "last_success_at": now if summary.get("published") else "",
        "last_failure_reason": "" if summary.get("published")
        else (summary.get("reason") or ""),
        "pool_size": summary.get("pool_size", 0),
    }
    poolstore.write_refresh_state(cfg, state)
    return state
