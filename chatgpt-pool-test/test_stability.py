"""场景 B/C + §20 / P1-02 / P1-09 / P1-10：稳定性固定 3 次、每次完整 STRICT_PASS。"""
import unittest

from helpers import make_cfg
from stability import run_stability


def seq_check(seq):
    state = {"i": 0}

    def fn():
        i = state["i"]
        if i >= len(seq):
            raise AssertionError("check_fn called more than fixed attempts")
        state["i"] += 1
        return {"strict_pass": seq[i]}
    return fn


class TestStability(unittest.TestCase):

    def test_3of3_pass(self):
        results, ok = run_stability(seq_check([True, True, True]), make_cfg("stab_333"))
        self.assertTrue(ok)
        self.assertEqual(results, [True, True, True])

    def test_2of3_pass(self):
        results, ok = run_stability(seq_check([True, False, True]), make_cfg("stab_213"))
        self.assertTrue(ok)
        self.assertEqual(sum(results), 2)

    def test_1of3_fail(self):
        results, ok = run_stability(seq_check([True, False, False]), make_cfg("stab_113"))
        self.assertFalse(ok)

    def test_0of3_fail(self):
        results, ok = run_stability(seq_check([False, False, False]), make_cfg("stab_003"))
        self.assertFalse(ok)

    def test_exactly_three_attempts(self):
        # 若实现为 4/5/N 次，seq_check 会抛 AssertionError（P1-09）
        cfg = make_cfg("stab_fixed3")
        results, ok = run_stability(seq_check([True, True, True]), cfg)
        self.assertEqual(len(results), cfg.stability_attempts)


if __name__ == "__main__":
    unittest.main()
