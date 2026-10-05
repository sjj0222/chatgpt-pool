"""场景：解析、node_id 去重（§23 / §24）。"""
import base64
import json
import unittest

from dedup import dedup
from models import Node
from parser import make_node_id, parse_line, parse_all


def ss_b64(method, password):
    return base64.b64encode(f"{method}:{password}".encode()).decode()


class TestParser(unittest.TestCase):

    def test_ss_base64(self):
        line = f"ss://{ss_b64('aes-256-gcm', 'pw123')}@1.2.3.4:8388#US"
        n, bad = parse_line(line, "src")
        self.assertIsNotNone(n)
        self.assertFalse(bad)
        self.assertEqual(n.protocol, "ss")
        self.assertEqual(n.server, "1.2.3.4")
        self.assertEqual(n.port, 8388)
        self.assertEqual(n.password, "pw123")
        self.assertEqual(n.params["cipher"], "aes-256-gcm")

    def test_ss_plain(self):
        n, _ = parse_line("ss://aes-256-gcm:pw@5.6.7.8:9000#JP", "src")
        self.assertIsNotNone(n)
        self.assertEqual(n.server, "5.6.7.8")
        self.assertEqual(n.password, "pw")

    def test_vmess(self):
        obj = {"v": "2", "ps": "US-1", "add": "10.0.0.1", "port": "443",
               "id": "uuid-1111", "aid": "0", "net": "ws", "type": "none",
               "host": "cdn.example.com", "path": "/ws", "tls": "tls"}
        line = "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()
        n, bad = parse_line(line, "src")
        self.assertIsNotNone(n)
        self.assertEqual(n.protocol, "vmess")
        self.assertEqual(n.uuid, "uuid-1111")
        self.assertEqual(n.net_id, "ws")
        self.assertTrue(n.params["tls"])
        self.assertEqual(n.params["path"], "/ws")

    def test_vless(self):
        line = "vless://uuid-2222@10.0.0.2:443?type=ws&security=tls&sni=a.com&path=%2Fv#SG"
        n, _ = parse_line(line, "src")
        self.assertIsNotNone(n)
        self.assertEqual(n.protocol, "vless")
        self.assertEqual(n.uuid, "uuid-2222")
        self.assertEqual(n.params["network"], "ws")
        self.assertTrue(n.params["tls"])
        self.assertEqual(n.params["sni"], "a.com")

    def test_trojan(self):
        line = "trojan://secret@10.0.0.3:443?sni=b.com#KR"
        n, _ = parse_line(line, "src")
        self.assertIsNotNone(n)
        self.assertEqual(n.protocol, "trojan")
        self.assertEqual(n.password, "secret")
        self.assertEqual(n.params["sni"], "b.com")

    def test_http_socks(self):
        n, _ = parse_line("http://user:pass@10.0.0.4:8080#DE", "src")
        self.assertIsNotNone(n)
        self.assertEqual(n.protocol, "http")
        self.assertEqual(n.account, "user")
        n2, _ = parse_line("socks5://10.0.0.5:1080#FR", "src")
        self.assertEqual(n2.protocol, "socks5")
        self.assertEqual(n2.port, 1080)

    def test_invalid(self):
        n, bad = parse_line("this is not a proxy", "src")
        self.assertIsNone(n)
        self.assertTrue(bad)
        n2, bad2 = parse_line("", "src")
        self.assertIsNone(n2)
        self.assertFalse(bad2)  # 空行不算无效节点

    def test_node_id_stable_and_name_ignored(self):
        a, _ = parse_line(f"ss://{ss_b64('aes-256-gcm','pw')}@1.1.1.1:1001#US", "s1")
        b, _ = parse_line(f"ss://{ss_b64('aes-256-gcm','pw')}@1.1.1.1:1001#改名", "s2")
        self.assertEqual(a.node_id, b.node_id)
        c, _ = parse_line(f"ss://{ss_b64('aes-256-gcm','other')}@1.1.1.1:1001#US", "s1")
        self.assertNotEqual(a.node_id, c.node_id)

    def test_make_node_id(self):
        self.assertEqual(make_node_id("ss", "A.B", 1, "", "p"),
                         make_node_id("ss", "a.b", 1, "", "p"))
        self.assertNotEqual(make_node_id("ss", "a.b", 1, "", "p"),
                            make_node_id("ss", "a.b", 2, "", "p"))

    def test_parse_all_and_dedup(self):
        text = (
            f"ss://{ss_b64('aes-256-gcm','pw')}@1.1.1.1:1001#US\n"
            f"ss://{ss_b64('aes-256-gcm','pw')}@1.1.1.1:1001#US-again\n"
            f"garbage\n"
            f"# comment\n\n"
        )
        nodes, invalid = parse_all([("mock", text)])
        self.assertEqual(len(nodes), 2)
        self.assertEqual(invalid, 1)
        uniq, dupes = dedup(nodes)
        self.assertEqual(len(uniq), 1)
        self.assertEqual(dupes, 1)


if __name__ == "__main__":
    unittest.main()
