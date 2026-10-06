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
    os.makedirs(cfg.sub("logs"), exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[
            logging.FileHandler(cfg.sub("logs/pool.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ])
    # P1-04：REJECTED 明细独立落 logs/reject.log
    rj = logging.getLogger("pool.reject")
    rj.setLevel(logging.INFO)
    rj.propagate = False
    rj.addHandler(logging.FileHandler(
        cfg.sub("logs/reject.log"), encoding="utf-8"))


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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chatgpt-pool")
    ap.add_argument("--base", help="覆盖生产目录")
    ap.add_argument("--test", action="store_true", help="测试模式（目录/端口全部隔离）")
    ap.add_argument("cmd", choices=["init", "refresh", "serve", "run", "check", "mihomo"])
    ap.add_argument("--mihomo", action="store_true", help="run 时同时启动生产 Mihomo")
    ap.add_argument("mihomo_action", nargs="?",
                    choices=["start", "stop", "restart", "status", "reload"],
                    help="mihomo 子命令：start/stop/restart/status/reload")
    args = ap.parse_args(argv)

    if args.test:
        cfg = Config.for_test(base=args.base)
        cfg.server_port = cfg.test_server_port
    else:
        cfg = Config(base_dir=args.base) if args.base else Config()
        cfg.server_port = cfg.prod_server_port
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
