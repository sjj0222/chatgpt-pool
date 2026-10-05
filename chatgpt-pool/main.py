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
import pipeline
import poolstore
import server as server_mod
import scheduler
import yaml_lite


def _setup_logging(cfg):
    os.makedirs(cfg.sub("logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg.sub("logs/pool.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ])


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


def _prod_mihomo_start(cfg):
    """以当前最终池生成生产 Mihomo 配置并启动（P1-08）。"""
    text = poolstore.read_pool_text(cfg)
    if not text:
        print("no pool yet; run refresh first")
        return
    summary = pipeline.run_refresh(cfg)  # 保证使用最新池
    cfg_text = yaml_lite.emit_mihomo_config(
        [_load_latest_final_nodes(cfg)], cfg.prod_mihomo_port)
    rundir = cfg.sub("run/prod")
    os.makedirs(rundir, exist_ok=True)
    with open(os.path.join(rundir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg_text)
    print(f"production mihomo config written: {rundir}/config.yaml")
    print(f"start with: mihomo -d {rundir}")


def _load_latest_final_nodes(cfg):
    final_dir = cfg.sub("final")
    if not os.path.isdir(final_dir):
        return []
    files = sorted(os.listdir(final_dir))
    nodes = []
    for fn in reversed(files):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(final_dir, fn), "r", encoding="utf-8") as f:
            nodes = json.load(f)
        if nodes:
            break
    return nodes


def cmd_run(cfg, with_mihomo=False):
    if with_mihomo:
        threading.Thread(target=_prod_mihomo_start, args=(cfg,), daemon=True).start()
    srv = server_mod.start_server(cfg)
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
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chatgpt-pool")
    ap.add_argument("--base", help="覆盖生产目录")
    ap.add_argument("--test", action="store_true", help="测试模式（目录/端口全部隔离）")
    ap.add_argument("cmd", choices=["init", "refresh", "serve", "run", "check"])
    ap.add_argument("--mihomo", action="store_true", help="run 时同时启动生产 Mihomo")
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
    elif args.cmd == "run":
        cmd_run(cfg, with_mihomo=args.mihomo)
    elif args.cmd == "check":
        return cmd_check(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
