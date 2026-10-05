"""§25 / §36 B/C / P1-08：订阅服务交付 + 生产端口隔离。"""
import socket
import unittest
import urllib.error
import urllib.request

import server as server_mod
from helpers import make_cfg, write_old_pool


class TestServer(unittest.TestCase):

    def test_serves_pool_and_404(self):
        cfg = make_cfg("server_ephemeral")
        write_old_pool(cfg)
        srv = server_mod.start_server(cfg, port=0)
        port = srv.server_address[1]
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/chatgpt.yaml") as r:
                self.assertEqual(r.status, 200)
                self.assertIn("old", r.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/other")
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_prod_port_not_used_by_tests(self):
        # 测试绝不占用生产端口 18787（§3 / §33）
        cfg = make_cfg("server_prodport")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", cfg.prod_server_port),
                                     timeout=0.5)

    def test_test_port_18887(self):
        cfg = make_cfg("server_18887")
        write_old_pool(cfg)
        try:
            srv = server_mod.start_server(cfg, port=cfg.test_server_port)
        except OSError:
            self.skipTest("18887 occupied in this environment")
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{cfg.test_server_port}/chatgpt.yaml") as r:
                self.assertEqual(r.status, 200)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
