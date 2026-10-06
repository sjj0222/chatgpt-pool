"""生产 Mihomo 常驻服务 Mock 测试（P1-08 / §33）。

全部使用 FakePopen / monkeypatch 端口探测与 API，不启动任何真实进程，
不产生真实 PID，不触碰生产目录（cfg 全部落在测试临时目录内）。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import main as main_mod                       # noqa: E402
import mihomo_svc                             # noqa: E402
import poolstore                              # noqa: E402
from yaml_lite import emit_mihomo_config      # noqa: E402
from helpers import make_cfg, write_old_pool  # noqa: E402


class FakePopen:
    """记录启动命令；poll 恒 None（假装存活）。"""
    instances = []

    def __init__(self, cmd, stdout=None, stderr=None, start_new_session=False):
        self.args = list(cmd)
        self.pid = 4242
        self.returncode = None
        self.wait_called = False
        FakePopen.instances.append(self)

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        self.wait_called = True

    def kill(self):
        pass


class FakeResp:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_final(cfg, nodes):
    """V0.2.1 P0-02 / V0.2.2.1：写入 run/final/current.json（{"round_id","nodes"}）。"""
    os.makedirs(cfg.sub("run/final"), exist_ok=True)
    path = cfg.sub("run/final/current.json")
    payload = {"round_id": "20261005000000", "nodes": nodes}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _node(name, protocol="ss"):
    return {"name": name, "protocol": protocol, "server": f"10.0.0.{1}",
            "port": 8388, "password": "x", "params": {}}


class TestEmitMihomoConfig(unittest.TestCase):
    def test_multi_node_production_config(self):
        nodes = [_node("a"), _node("b")]
        text = emit_mihomo_config(nodes, 7890, api_port=9090)
        self.assertIn('mixed-port: 7890', text)
        self.assertIn('external-controller: "127.0.0.1:9090"', text)
        self.assertIn("mode: rule", text)
        self.assertIn("  - name: PROXY", text)
        self.assertIn("    type: url-test", text)
        self.assertIn("  - MATCH,PROXY", text)
        # 两个节点都在组里
        self.assertIn('      - "a"', text)
        self.assertIn('      - "b"', text)

    def test_single_node_detection_config(self):
        text = emit_mihomo_config([_node("a")], 18000)
        self.assertNotIn("external-controller", text)
        self.assertNotIn("url-test", text)
        self.assertIn("  - MATCH,GLOBAL", text)
        self.assertIn('      - "a"', text)


class TestProductionMihomoStartStop(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg("mihomo")
        self.cfg.mihomo_start_timeout = 1
        self.svc = mihomo_svc.ProductionMihomo(self.cfg)
        os.makedirs(self.svc.rundir, exist_ok=True)
        FakePopen.instances = []
        # status() 内部会探测版本；统一 mock，避免真实 check_output 干扰
        self._v = mock.patch("mihomo_svc.subprocess.check_output",
                             return_value=b"v1.19.31")
        self._v.start()
        self.addCleanup(self._v.stop)

    def test_paths_inside_test_base(self):
        self.assertTrue(self.svc.pid_path.startswith(self.cfg.base_dir))
        self.assertTrue(self.svc.config_path.startswith(self.cfg.base_dir))
        self.assertTrue(self.svc.log_path.startswith(self.cfg.base_dir))

    def test_start_writes_config_and_pid(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open", return_value=True):
            summary = self.svc.start()
        self.assertTrue(summary["started"])
        self.assertEqual(summary["pid"], 4242)
        self.assertEqual(len(FakePopen.instances), 1)
        self.assertIn("-d", FakePopen.instances[0].args)
        with open(self.svc.config_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("external-controller", text)
        self.assertEqual(str(self.svc._read_pid()), "4242")

    def test_start_idempotent_when_running(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        # 模拟已有运行中实例：PID 文件指向真实存活进程 + 端口开着
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open", return_value=True):
            summary = self.svc.start()
        self.assertFalse(summary["started"])
        self.assertEqual(summary["reason"], "already_running")
        self.assertEqual(FakePopen.instances, [])  # 未重复启动

    def test_start_no_active_pool(self):
        # V0.2.1 P0-03：无 run/final/current.json → NO_ACTIVE_POOL，绝不启动空池
        summary = self.svc.start()
        self.assertFalse(summary["started"])
        self.assertEqual(summary["reason"], "NO_ACTIVE_POOL")

    def test_start_timeout_cleans_pid(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        self.cfg.mihomo_start_timeout = 0.3
        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open", return_value=False):
            with self.assertRaises(mihomo_svc.ProxyStartError):
                self.svc.start()
        self.assertIsNone(self.svc._read_pid())  # 超时后 PID 清理

    def test_start_spawn_failure(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        with mock.patch("mihomo_svc.subprocess.Popen",
                        side_effect=FileNotFoundError("no mihomo")):
            summary = self.svc.start()
        self.assertFalse(summary["started"])
        self.assertIn("spawn_failed", summary["reason"])

    def test_stop_kills_only_pidfile_pid(self):
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write("12345")
        with mock.patch("mihomo_svc._proc_alive", return_value=True), \
             mock.patch("mihomo_svc.os.kill") as mk:
            summary = self.svc.stop()
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["pid"], 12345)
        # 第一个调用是 SIGTERM，目标 pid 必须是 PID 文件里的 12345
        sigterm_call = mk.call_args_list[0]
        self.assertEqual(sigterm_call[0][0], 12345)
        self.assertEqual(sigterm_call[0][1], mihomo_svc.signal.SIGTERM)
        self.assertIsNone(self.svc._read_pid())

    def test_stop_no_pid_file(self):
        summary = self.svc.stop()
        self.assertTrue(summary["stopped"])
        self.assertIsNone(summary["pid"])

    def test_stop_waits_own_proc(self):
        """自己启动的进程：stop 必须 wait 回收（避免僵尸）。"""
        _write_final(self.cfg, [_node("a"), _node("b")])
        with mock.patch("mihomo_svc.subprocess.Popen", FakePopen), \
             mock.patch("mihomo_svc.is_port_open", return_value=True):
            self.svc.start()
        proc = self.svc._proc
        self.assertIsNotNone(proc)
        with mock.patch("mihomo_svc._proc_alive", return_value=True):
            summary = self.svc.stop()
        self.assertTrue(summary["stopped"])
        self.assertTrue(proc.wait_called)
        self.assertIsNone(self.svc._proc)
        self.assertIsNone(self.svc._read_pid())

    def test_status_stopped(self):
        st = self.svc.status()
        self.assertFalse(st["running"])
        self.assertFalse(st["pid_alive"])

    def test_status_running(self):
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with mock.patch("mihomo_svc.is_port_open", return_value=True):
            st = self.svc.status()
        self.assertTrue(st["running"])
        self.assertEqual(st["pid"], os.getpid())

    # ---- V0.2.2.2：stop 生命周期增强（等待/确认 PID 退出/确认端口释放）----

    def test_stop_confirms_pid_exit_and_ports(self):
        """kill → 等待 → 确认 PID 退出 → 确认 7890/9090 释放。"""
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write("12345")
        # _proc_alive 调用序列：初始判定 True → 进入终止；轮询第一次 False 退出；
        # 之后均为 False → pid_exited=True
        seq = [True, True, False, False, False]
        with mock.patch("mihomo_svc._proc_alive", side_effect=seq), \
             mock.patch("mihomo_svc.is_port_open", return_value=False):
            summary = self.svc.stop()
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["pid"], 12345)
        self.assertTrue(summary["pid_exited"])
        self.assertEqual(summary["ports"],
                         {self.cfg.prod_mihomo_port: True,
                          self.cfg.mihomo_api_port: True})

    def test_stop_pid_not_exists_reports_released(self):
        """无 PID 文件：pid_exited=True（无进程），端口未被占用即视为释放。"""
        summary = self.svc.stop()
        self.assertTrue(summary["stopped"])
        self.assertIsNone(summary["pid"])
        self.assertTrue(summary["pid_exited"])
        self.assertTrue(all(summary["ports"].values()))

    def test_stop_port_release_timeout(self):
        """端口未释放：返回 ports={7890: False}，绝不 kill 其他进程。"""
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write("12345")
        with mock.patch("mihomo_svc._proc_alive", return_value=False), \
             mock.patch.object(
                 self.svc, "_wait_ports_released",
                 return_value={self.cfg.prod_mihomo_port: False,
                               self.cfg.mihomo_api_port: True}):
            summary = self.svc.stop()
        self.assertTrue(summary["pid_exited"])
        self.assertFalse(summary["ports"][self.cfg.prod_mihomo_port])
        self.assertTrue(summary["ports"][self.cfg.mihomo_api_port])

    def test_cli_stop_prints_pass_and_timeout(self):
        """CLI 输出：PASS port released / PORT_RELEASE_TIMEOUT port=xxx。"""
        import contextlib
        import io
        _write_final(self.cfg, [_node("a")])
        with mock.patch.object(
                mihomo_svc.ProductionMihomo, "_wait_ports_released",
                return_value={self.cfg.prod_mihomo_port: False,
                              self.cfg.mihomo_api_port: True}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main_mod.cmd_mihomo(self.cfg, "stop")
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("PORT_RELEASE_TIMEOUT port=7890", out)
        self.assertNotIn("PASS port released", out)
        with mock.patch.object(
                mihomo_svc.ProductionMihomo, "_wait_ports_released",
                return_value={self.cfg.prod_mihomo_port: True,
                              self.cfg.mihomo_api_port: True}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main_mod.cmd_mihomo(self.cfg, "stop")
        self.assertIn("PASS port released", buf.getvalue())


class TestProductionMihomoReload(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg("mihomo")
        self.svc = mihomo_svc.ProductionMihomo(self.cfg)
        os.makedirs(self.svc.rundir, exist_ok=True)
        FakePopen.instances = []
        self._v = mock.patch("mihomo_svc.subprocess.check_output",
                             return_value=b"v1.19.31")
        self._v.start()
        self.addCleanup(self._v.stop)

    def test_reload_when_not_running_writes_config(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        result = self.svc.reload()
        self.assertFalse(result["reloaded"])
        self.assertIn("not_running", result["reason"])
        self.assertTrue(os.path.isfile(self.svc.config_path))

    def test_reload_pushes_configs_api(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        calls = {}

        def fake_urlopen(req, timeout=None):
            calls["url"] = req.full_url
            calls["method"] = req.get_method()
            calls["data"] = req.data
            return FakeResp(204)

        with mock.patch("mihomo_svc.is_port_open", return_value=True), \
             mock.patch("mihomo_svc.urllib.request.urlopen", fake_urlopen):
            result = self.svc.reload()
        self.assertTrue(result["reloaded"])
        self.assertIn("/configs?force=true", calls["url"])
        self.assertEqual(calls["method"], "PUT")
        self.assertIn(b"proxies:", calls["data"])

    def test_reload_api_failure_falls_back(self):
        _write_final(self.cfg, [_node("a"), _node("b")])
        with open(self.svc.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with mock.patch("mihomo_svc.is_port_open", return_value=True), \
             mock.patch("mihomo_svc.urllib.request.urlopen",
                        side_effect=OSError("conn refused")):
            result = self.svc.reload()
        self.assertFalse(result["reloaded"])
        self.assertIn("api_failed", result["reason"])
        self.assertTrue(os.path.isfile(self.svc.config_path))


class TestMihomoCli(unittest.TestCase):
    def setUp(self):
        self._v = mock.patch("mihomo_svc.subprocess.check_output",
                             return_value=b"v1.19.31")
        self._v.start()
        self.addCleanup(self._v.stop)

    def test_main_mihomo_status_uses_test_base(self):
        cfg = make_cfg("mihomo_cli")
        ret = main_mod.main(["--test", "--base", cfg.base_dir, "mihomo", "status"])
        self.assertEqual(ret, 0)
        # 测试目录内不应有生产 PID 文件（生产 run/prod 在真实 ~/chatgpt-pool）
        self.assertFalse(os.path.isfile(cfg.sub("run/prod/mihomo.pid")))

    def test_main_mihomo_requires_action(self):
        cfg = make_cfg("mihomo_cli")
        ret = main_mod.main(["--test", "--base", cfg.base_dir, "mihomo"])
        self.assertEqual(ret, 2)


if __name__ == "__main__":
    unittest.main()
