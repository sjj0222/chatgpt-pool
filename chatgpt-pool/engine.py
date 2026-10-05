"""Mihomo 代理引擎（P1-08 / §33 隔离）。

- 每个节点独立启动/停止一个 Mihomo 实例（独立 rundir、日志、端口）。
- 生产/测试完全隔离：测试绝不 kill 生产 PID，生产不读测试配置。
- 生产长驻 Mihomo（main.py --mihomo）使用 prod_mihomo_port。
"""
import logging
import os
import shutil
import socket
import subprocess
import time

from yaml_lite import emit_mihomo_config

log = logging.getLogger("pool.engine")


class ProxyStartError(Exception):
    pass


def is_port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def allocate_port(start, end):
    for p in range(int(start), int(end) + 1):
        if not is_port_open(p):
            return p
    raise ProxyStartError(f"no free port in {start}-{end}")


class MihomoEngine:
    """单节点检测用 Mihomo 实例（start → 就绪端口 → stop）。"""

    def __init__(self, cfg, rundir, logfile):
        self.cfg = cfg
        self.rundir = rundir
        self.logfile = logfile
        self.proc = None
        self.port = None
        self.bin = shutil.which(cfg.mihomo_bin)
        if not self.bin and os.path.isfile(cfg.mihomo_bin):
            self.bin = cfg.mihomo_bin
        if not self.bin:
            raise RuntimeError(
                "mihomo binary not found; install with: pkg install mihomo")

    def start(self, node_dict, mixed_port):
        os.makedirs(self.rundir, exist_ok=True)
        os.makedirs(os.path.dirname(self.logfile), exist_ok=True)
        cfg_text = emit_mihomo_config([node_dict], mixed_port,
                                      mode="global", log_level="warning")
        cfg_path = os.path.join(self.rundir, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg_text)
        logf = open(self.logfile, "a")
        self.proc = subprocess.Popen(
            [self.bin, "-d", self.rundir, "-f", cfg_path],
            stdout=logf, stderr=logf)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.proc = None
                raise ProxyStartError("mihomo exited early")
            if is_port_open(mixed_port):
                self.port = mixed_port
                return mixed_port
            time.sleep(0.3)
        self.stop()
        raise ProxyStartError("mihomo not ready in 20s")

    def stop(self):
        """只终止自己启动的进程；绝不触碰其他 PID。"""
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.port = None
