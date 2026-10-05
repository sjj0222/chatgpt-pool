#!/usr/bin/env python3
"""测试入口（测试与生产完全隔离）：
cd ~/chatgpt-pool-test && python3 run_tests.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.dirname(os.path.abspath(__file__)),
                            pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
