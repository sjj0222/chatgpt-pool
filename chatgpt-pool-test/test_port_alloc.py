"""V0.4.3.3：临时 Mihomo 端口并发分配原子性测试。

修复 TOCTOU race：旧 allocate_port() 多线程同时看到同一端口空闲，
返回同端口 → mihomo bind: address already in use。
新实现：_PORT_LOCK + _RESERVED_PORTS 预留集，选中即占。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "chatgpt-pool"))

import engine


class TestPortAlloc(unittest.TestCase):
    def setUp(self):
        """每个测试前清空预留集（进程内单例）。"""
        with engine._PORT_LOCK:
            engine._RESERVED_PORTS.clear()

    def test_alloc_returns_in_range(self):
        p = engine.allocate_port(18000, 18005)
        self.assertTrue(18000 <= p <= 18005)
        engine.release_port(p)

    def test_alloc_all_unique_concurrent(self):
        """10 线程同时 allocate，必须全部拿到不同端口。"""
        ports = []
        lock = threading.Lock()

        def worker():
            p = engine.allocate_port(18100, 18120)
            with lock:
                ports.append(p)

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ports), 15)
        self.assertEqual(len(set(ports)), 15, f"端口重复: {ports}")
        for p in ports:
            engine.release_port(p)

    def test_release_allows_realloc(self):
        """release 后端口可再次分配。"""
        p1 = engine.allocate_port(18200, 18200)
        self.assertEqual(p1, 18200)
        with self.assertRaises(engine.ProxyStartError):
            engine.allocate_port(18200, 18200)
        engine.release_port(p1)
        p2 = engine.allocate_port(18200, 18200)
        self.assertEqual(p2, 18200)
        engine.release_port(p2)

    def test_exhausted_raises(self):
        """范围耗尽时正确抛 ProxyStartError。"""
        taken = [engine.allocate_port(18300, 18302) for _ in range(3)]
        self.assertEqual(len(set(taken)), 3)
        with self.assertRaises(engine.ProxyStartError):
            engine.allocate_port(18300, 18302)
        for p in taken:
            engine.release_port(p)

    def test_release_none_safe(self):
        engine.release_port(None)


if __name__ == "__main__":
    unittest.main()

