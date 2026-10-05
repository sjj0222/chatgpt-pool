"""生产 Mihomo 常驻服务（P1-08 / §33 隔离细化）。

职责（仅生产侧；测试环境通过 Config.for_test 落到测试目录，天然隔离）：
- start：以最新最终池生成完整生产配置并启动常驻 Mihomo，写 PID 文件；
  已在运行时幂等返回，不重复启动。
- stop：只终止 PID 文件记录的进程，绝不触碰其他 PID（§33）。
- status：进程存活 + 代理端口/API 端口探测。
- reload：刷新发布新池后，重写配置并经 external-controller API 热重载；
  API 不可达时回退为写盘（下次 start 生效），不破坏运行中的实例。

与逐节点检测引擎（engine.MihomoEngine）隔离：检测引擎每节点独立
rundir/端口/日志；本模块只管理 run/prod 常驻实例。
"""
import logging
import os
import signal
import subprocess
import time
import urllib.request

from engine import ProxyStartError, is_port_open
import poolstore
from yaml_lite import emit_mihomo_config

log = logging.getLogger("pool.mihomo_svc")


def _proc_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class ProductionMihomo:
    """生产常驻 Mihomo 实例（一个项目仅一个；目录由 cfg 决定）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rundir = cfg.sub("run/prod")
        self.config_path = os.path.join(self.rundir, "config.yaml")
        self.pid_path = os.path.join(self.rundir, "mihomo.pid")
        self.log_path = cfg.sub("logs/mihomo.log")
        self._proc = None  # 保留句柄以便 stop 时 wait 回收（避免僵尸）

    # ---- 路径与探测 ----
    def _read_pid(self):
        try:
            with open(self.pid_path, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _write_pid(self, pid):
        os.makedirs(self.rundir, exist_ok=True)
        with open(self.pid_path, "w", encoding="utf-8") as f:
            f.write(str(pid))

    def _bin(self):
        bin_path = self.cfg.mihomo_bin
        if os.path.isfile(bin_path):
            return bin_path
        return bin_path  # 交由 PATH 解析（subprocess 找不到会抛 FileNotFoundError）

    def ensure_config(self, nodes):
        """以最终池生成完整生产配置并写盘（原子：tmp → replace）。"""
        text = emit_mihomo_config(
            nodes, self.cfg.prod_mihomo_port,
            api_port=self.cfg.mihomo_api_port,
            group_url=self.cfg.mihomo_group_url,
            log_level=self.cfg.mihomo_log_level)
        os.makedirs(self.rundir, exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.config_path)
        return self.config_path

    # ---- 状态 ----
    def status(self):
        pid = self._read_pid()
        alive = _proc_alive(pid)
        proxy_open = is_port_open(self.cfg.prod_mihomo_port)
        api_open = is_port_open(self.cfg.mihomo_api_port)
        return {
            "running": bool(alive and proxy_open),
            "pid": pid,
            "pid_alive": bool(alive),
            "proxy_port_open": proxy_open,
            "api_port_open": api_open,
            "proxy_port": self.cfg.prod_mihomo_port,
            "api_port": self.cfg.mihomo_api_port,
            "config": self.config_path,
            "log": self.log_path,
        }

    # ---- 生命周期 ----
    def start(self, nodes=None):
        """启动常驻实例；已在运行则幂等返回。返回 summary dict。"""
        st = self.status()
        if st["running"]:
            return {"started": False, "reason": "already_running",
                    "pid": st["pid"], "config": self.config_path}

        if nodes is None:
            nodes = poolstore.read_latest_final(self.cfg)
        if not nodes:
            return {"started": False, "reason": "no_final_nodes; run refresh first",
                    "config": self.config_path}

        self.ensure_config(nodes)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        logf = open(self.log_path, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [self._bin(), "-d", self.rundir, "-f", self.config_path],
                stdout=logf, stderr=logf, start_new_session=True)
        except Exception as e:
            logf.close()
            return {"started": False, "reason": f"spawn_failed: {e}"}

        self._write_pid(proc.pid)
        self._proc = proc
        deadline = time.time() + self.cfg.mihomo_start_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                self._clear_pid()
                return {"started": False, "reason": "mihomo exited early",
                        "exit_code": proc.returncode, "log": self.log_path}
            if is_port_open(self.cfg.prod_mihomo_port):
                log.info("prod mihomo started pid=%s port=%s",
                         proc.pid, self.cfg.prod_mihomo_port)
                return {"started": True, "pid": proc.pid,
                        "proxy_port": self.cfg.prod_mihomo_port,
                        "api_port": self.cfg.mihomo_api_port,
                        "config": self.config_path}
            time.sleep(0.3)

        # 启动超时：终止自己启动的进程并清理
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._proc = None
        self._clear_pid()
        raise ProxyStartError(
            f"mihomo not ready in {self.cfg.mihomo_start_timeout}s; see {self.log_path}")

    def _clear_pid(self):
        try:
            os.remove(self.pid_path)
        except OSError:
            pass

    def stop(self):
        """只终止 PID 文件记录的进程（§33：绝不触碰其他 PID）。"""
        pid = self._read_pid()
        proc = self._proc if (self._proc is not None
                              and self._proc.pid == pid) else None
        if pid and _proc_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            if proc is not None:
                # 自己启动的进程：wait 回收，避免僵尸残留
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            else:
                for _ in range(25):  # 最多等 5 秒
                    if not _proc_alive(pid):
                        break
                    time.sleep(0.2)
            if _proc_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                if proc is not None:
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
            log.info("prod mihomo stopped pid=%s", pid)
        self._proc = None
        self._clear_pid()
        return {"stopped": True, "pid": pid}

    def restart(self, nodes=None):
        self.stop()
        return self.start(nodes)

    # ---- 热重载（刷新发布后调用）----
    def reload(self, nodes=None):
        """写新配置 → external-controller 热重载；API 不可达则回退写盘。"""
        if nodes is None:
            nodes = poolstore.read_latest_final(self.cfg)
        self.ensure_config(nodes or [])
        st = self.status()
        if not st["running"]:
            return {"reloaded": False,
                    "reason": "not_running; config written, applied on next start",
                    "config": self.config_path}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                text = f.read()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.cfg.mihomo_api_port}/configs?force=true",
                data=text.encode("utf-8"), method="PUT",
                headers={"Content-Type": "application/yaml"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = 200 <= resp.status < 300
            return {"reloaded": ok,
                    "reason": None if ok else f"api_status_{resp.status}",
                    "config": self.config_path}
        except Exception as e:
            return {"reloaded": False,
                    "reason": f"api_failed: {e}; config written, restart to apply",
                    "config": self.config_path}
