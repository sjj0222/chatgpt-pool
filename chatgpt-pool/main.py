"""CLI 入口。

用法（生产，Termux）：
  cd ~/chatgpt-pool
  python3 main.py init            # 初始化目录 + config.json
  python3 main.py refresh         # 执行一轮刷新
  python3 main.py serve           # 启动订阅服务 127.0.0.1:18787/chatgpt.yaml
  python3 main.py run             # 订阅服务 + 定时刷新（7200s）
  python3 main.py run --mihomo    # 额外以生产池启动生产 Mihomo（7890）
  python3 main.py check           # 健康检查

测试模式（与生产完全隔离）：
  python3 main.py --test refresh  # 使用 ~/chatgpt-pool-test + 18887 + 19000-19999
"""
import argparse
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
import engine as engine_mod
import mihomo_svc
import pipeline
import poolstore
import server as server_mod
import scheduler
import yaml_lite


def _setup_logging(cfg):
    from logging.handlers import RotatingFileHandler
    os.makedirs(cfg.sub("logs"), exist_ok=True)
    os.makedirs(cfg.sub("run"), exist_ok=True)  # V0.2.2.1：run/detect.log 父目录
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # V0.3.1（0.3.1-3）：RotatingFileHandler——日志超限自动 rotate，禁止无限增长
    rfh = RotatingFileHandler(cfg.sub("logs/pool.log"), encoding="utf-8",
                              maxBytes=50 * 1024 * 1024, backupCount=2)
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[rfh, logging.StreamHandler()])
    # P1-04：REJECTED 明细独立落 logs/reject.log
    rj = logging.getLogger("pool.reject")
    rj.setLevel(logging.INFO)
    rj.propagate = False
    rj.addHandler(RotatingFileHandler(
        cfg.sub("logs/reject.log"), encoding="utf-8",
        maxBytes=50 * 1024 * 1024, backupCount=2))
    # V0.2.2 P0-03 / V0.2.2.1：分阶段检测日志 run/detect.log
    dt = logging.getLogger("pool.detect")
    dt.setLevel(logging.INFO)
    dt.propagate = False
    dt.addHandler(RotatingFileHandler(
        cfg.sub("run/detect.log"), encoding="utf-8",
        maxBytes=50 * 1024 * 1024, backupCount=2))


def _print_detection_result(summary):
    """V0.2.2 P1-03：refresh 输出分阶段检测统计。"""
    print("Detection Result")
    print(f"Total:   {summary.get('total', 0)}")
    print(f"Failed:  {summary.get('failed_node_count', 0)}")
    st = summary.get("stage_summary", {})
    if st:
        print("Stage:   " + ", ".join(f"{k} {v}" for k, v in st.items()))
    print("Reason:  " + ", ".join(
        f"{k} {v}" for k, v in sorted(
            summary.get("reject_summary_detailed", {}).items(),
            key=lambda kv: -kv[1])))
    bp = summary.get("reject_summary_by_protocol", {})
    if bp:
        print("Protocol:")
        for proto, rec in sorted(bp.items()):
            line = f"  {proto}: total={rec['total']} failed={rec['failed']}"
            subs = {k: v for k, v in rec.items() if k not in ("total", "failed")}
            if subs:
                line += " " + ", ".join(f"{k} {v}" for k, v in subs.items())
            print(line)


def cmd_init(cfg):
    poolstore.ensure_dirs(cfg)
    cfg_path = cfg.sub("config.json")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg.to_json() + "\n")
        print(f"config written: {cfg_path}")
    print(f"base dir ready: {cfg.base_dir}")


def cmd_refresh(cfg):
    summary = pipeline.run_refresh(cfg)
    _print_detection_result(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("published") else 1


def cmd_serve(cfg):
    server_mod.start_server(cfg, block=True)


def _prod_mihomo(cfg):
    """生产常驻 Mihomo 服务句柄（run/prod 目录，§33 隔离）。"""
    return mihomo_svc.ProductionMihomo(cfg)


def cmd_mihomo(cfg, action):
    """mihomo start|stop|restart|status|reload（生产常驻实例管理）。"""
    svc = _prod_mihomo(cfg)
    if action == "start":
        summary = svc.start()
    elif action == "stop":
        summary = svc.stop()
        # V0.2.2.2：stop 生命周期验收（PASS port released / PORT_RELEASE_TIMEOUT）
        ports = summary.get("ports", {})
        timeout_ports = [p for p, released in ports.items() if not released]
        if not timeout_ports:
            print("PASS port released")
        else:
            for p in timeout_ports:
                print(f"PORT_RELEASE_TIMEOUT port={p}")
    elif action == "restart":
        summary = svc.restart()
    elif action == "status":
        summary = svc.status()
    elif action == "reload":
        summary = svc.reload()
    else:
        print(f"unknown mihomo action: {action}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_run(cfg, with_mihomo=False):
    srv = server_mod.start_server(cfg)
    if with_mihomo:
        svc = _prod_mihomo(cfg)
        # 启动前先确保最终池最新（可能耗时，属一次性初始化）
        try:
            pipeline.run_refresh(cfg)
        except Exception:
            log = logging.getLogger("pool.main")
            log.exception("initial refresh failed; start with existing pool")
        start_summary = svc.start()
        print("mihomo:", json.dumps(start_summary, ensure_ascii=False))

        def _reload_after_publish(_summary):
            r = svc.reload()
            print("mihomo reload:", json.dumps(r, ensure_ascii=False))

        threading.Thread(
            target=scheduler.refresh_loop,
            args=(cfg,), kwargs={"on_published": _reload_after_publish},
            daemon=True).start()
    else:
        threading.Thread(target=scheduler.refresh_loop, args=(cfg,), daemon=True).start()
    print(f"server on 127.0.0.1:{getattr(cfg, 'server_port', cfg.prod_server_port)} "
          f"(/chatgpt.yaml), refresh every {cfg.refresh_interval}s")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
        print("stopped")


def cmd_check(cfg):
    text = poolstore.read_pool_text(cfg)
    if text is None:
        print(f"pool: MISSING ({cfg.pool_path})")
        return 1
    count = sum(1 for line in text.splitlines()
                if line.startswith("  - name:") or line.startswith("- name:"))
    print(f"pool: OK ({cfg.pool_path}) entries={count}")
    print(f"subscription: http://127.0.0.1:{getattr(cfg, 'server_port', cfg.prod_server_port)}/chatgpt.yaml")
    svc = _prod_mihomo(cfg)
    st = svc.status()
    print(f"mihomo: {'running' if st['running'] else 'stopped'} "
          f"pid={st['pid']} proxy_port={st['proxy_port_open']} "
          f"api_port={st['api_port_open']}")
    return 0


def cmd_scheduler(cfg, action):
    """V0.3 Phase 7：scheduler status|tick|probe|score|cleanup|recover。"""
    import json as _json
    import isolation as isolation_mod
    import probe_services
    import scoring as scoring_mod
    import scheduler as sched_mod

    if action == "status":
        print(_json.dumps(sched_mod.schedule_status(cfg),
                          ensure_ascii=False, indent=2))
        return 0
    if action == "recover":
        rec = sched_mod.crash_recover(cfg)
        print(_json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    if action == "probe":
        with sched_mod.Lock(cfg, "probe"):
            pool = poolstore.read_current_final(cfg)
            node_ids = [n.get("node_id") for n in pool] if pool else []
            res = probe_services.run_service_probe(cfg, node_ids=node_ids)
            print(_json.dumps({"probed": len(res)}, ensure_ascii=False))
        return 0
    if action == "score":
        pool = poolstore.read_current_final(cfg) or []
        from lifecycle_store import NodeLifecycleStore
        lc = NodeLifecycleStore(cfg.sub("state/node_lifecycle.json"))
        import time as _t
        now = int(_t.time())
        for n in pool:
            rec = lc.get(n.get("node_id"))
            if not rec:
                continue
            q = scoring_mod.compute_quality_score(rec, now=now)
            ss = scoring_mod.compute_service_score(rec,
                                                   n.get("chatgpt_status", ""))
            rec["score"] = q.to_dict()
            rec["service_score"] = ss.to_dict()
            rec["capability"] = scoring_mod.capability_tags(ss)
            scoring_mod.record_score_history(rec, q.overall, ts=now)
            lc.upsert(n.get("node_id"), rec)
        lc.save()
        print(_json.dumps({"scored": len(pool)}, ensure_ascii=False))
        return 0
    if action == "cleanup":
        res = sched_mod.enforce_size_limit(cfg)
        expired = isolation_mod.cleanup_expired(cfg)   # §13：30 天过期节点
        res["expired_removed"] = expired
        print(_json.dumps(res, ensure_ascii=False))
        return 0
    if action == "health_check":
        # V0.3.1（0.3.1-3/4）：资源监控 + 日志轮转 + 报告；
        # V0.4.1：source_quality_report 一并生成（异常不中断）
        import runtime_monitor
        import source_quality_report
        rotated = runtime_monitor.rotate_all_logs(cfg)
        report = runtime_monitor.build_runtime_report(cfg)
        runtime_monitor.write_runtime_report(cfg, report)
        stats = runtime_monitor.resource_stats(cfg)
        sq_path = source_quality_report.write_report(cfg)
        print(_json.dumps({"resources": stats, "rotated": rotated,
                           "report": report, "source_quality": sq_path},
                          ensure_ascii=False, indent=2))
        return 0
    if action == "backup":
        # V0.3.1（0.3.1-2）：自动备份任务
        import scheduler as _sched
        base = _sched.backup(cfg)
        print(_json.dumps({"backup_dir": base}, ensure_ascii=False))
        return 0
    if action == "tick":
        def _refresh():
            pipeline.run_refresh(cfg)

        def _probe():
            with sched_mod.Lock(cfg, "probe"):
                pool = poolstore.read_current_final(cfg)
                node_ids = [n.get("node_id") for n in pool] if pool else []
                probe_services.run_service_probe(cfg, node_ids=node_ids)

        def _score():
            cmd_scheduler(cfg, "score")

        def _cleanup():
            cmd_scheduler(cfg, "cleanup")

        def _health_check():
            cmd_scheduler(cfg, "health_check")

        def _backup():
            cmd_scheduler(cfg, "backup")

        ran = sched_mod.tick(cfg, {"refresh": _refresh, "probe": _probe,
                                   "score": _score, "cleanup": _cleanup,
                                   "health_check": _health_check,
                                   "backup": _backup})
        print(_json.dumps(ran, ensure_ascii=False))
        return 0
    print("usage: python3 main.py scheduler status|tick|probe|score|cleanup|recover|health_check|backup")
    return 2


def cmd_backup(cfg):
    """V0.3 Phase 7（§34）：备份 config/history/score/source 快照。"""
    import json as _json
    import scheduler as sched_mod
    base = sched_mod.backup(cfg)
    print(_json.dumps({"backup_dir": base}, ensure_ascii=False))
    return 0


def cmd_web(cfg):
    """V0.3 Phase 9（§36/§37）：本机 Web 管理页面（仅 127.0.0.1）。"""
    import dashboard as dash_mod
    httpd = dash_mod.start_dashboard(cfg, port=cfg.web_port)
    print("web dashboard: http://127.0.0.1:%d  (仅本机访问, §36)" % cfg.web_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        print("web stopped")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chatgpt-pool")
    ap.add_argument("--base", help="覆盖生产目录")
    ap.add_argument("--test", action="store_true", help="测试模式（目录/端口全部隔离）")
    ap.add_argument("cmd", choices=["init", "refresh", "serve", "run", "check",
                                    "mihomo", "scheduler", "backup", "web"])
    ap.add_argument("--mihomo", action="store_true", help="run 时同时启动生产 Mihomo")
    ap.add_argument("mihomo_action", nargs="?",
                    choices=["start", "stop", "restart", "status", "reload",
                             "tick", "probe", "score", "cleanup", "recover",
                             "health_check", "backup"],
                    help="mihomo 子命令；scheduler 子命令 status/tick/probe/score/cleanup/recover/health_check/backup")
    args = ap.parse_args(argv)

    if args.test:
        cfg = Config.for_test(base=args.base)
        cfg.server_port = cfg.test_server_port
        cfg.web_port = cfg.test_web_port
    else:
        cfg = Config(base_dir=args.base) if args.base else Config()
        cfg.server_port = cfg.prod_server_port
        cfg.web_port = cfg.prod_web_port
    _setup_logging(cfg)

    if args.cmd == "init":
        cmd_init(cfg)
    elif args.cmd == "refresh":
        return cmd_refresh(cfg)
    elif args.cmd == "serve":
        cmd_serve(cfg)
    if args.cmd == "run":
        if args.test and args.mihomo:
            print("test mode: refusing to start production mihomo (§32)")
            cmd_run(cfg, with_mihomo=False)
        else:
            cmd_run(cfg, with_mihomo=args.mihomo)
    elif args.cmd == "check":
        return cmd_check(cfg)
    elif args.cmd == "mihomo":
        if not args.mihomo_action:
            print("usage: python3 main.py mihomo start|stop|restart|status|reload")
            return 2
        return cmd_mihomo(cfg, args.mihomo_action)
    elif args.cmd == "scheduler":
        if not args.mihomo_action:
            print("usage: python3 main.py scheduler status|tick|probe|score|cleanup|recover")
            return 2
        return cmd_scheduler(cfg, args.mihomo_action)
    elif args.cmd == "backup":
        return cmd_backup(cfg)
    elif args.cmd == "web":
        return cmd_web(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
