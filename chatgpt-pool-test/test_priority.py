"""§17 失败优先级（含 P1-03 的 STRICT_CHECK_FAIL 位置）。"""
import unittest

from models import FailureReason, highest_priority


class TestPriority(unittest.TestCase):

    def test_order_respected(self):
        self.assertEqual(
            highest_priority([FailureReason.STABILITY_FAIL,
                              FailureReason.INVALID_NODE]),
            FailureReason.INVALID_NODE)
        self.assertEqual(
            highest_priority([FailureReason.TIMEOUT,
                              FailureReason.TLS_FAIL]),
            FailureReason.TLS_FAIL)
        self.assertEqual(
            highest_priority([FailureReason.CHATGPT_UNREACHABLE,
                              FailureReason.OPENAI_BLOCKED]),
            FailureReason.OPENAI_BLOCKED)
        self.assertEqual(
            highest_priority([FailureReason.NO_EGRESS_IP,
                              FailureReason.PROXY_START_FAIL]),
            FailureReason.PROXY_START_FAIL)

    def test_strict_check_fail_position(self):
        # P1-03：STRICT_CHECK_FAIL 位于 STABILITY_FAIL 之后、INTERNAL_ERROR 之前
        idx = {r: i for i, r in enumerate(FailureReason)}
        self.assertLess(idx[FailureReason.STABILITY_FAIL],
                       idx[FailureReason.STRICT_CHECK_FAIL])
        self.assertLess(idx[FailureReason.STRICT_CHECK_FAIL],
                       idx[FailureReason.INTERNAL_ERROR])

    def test_empty(self):
        self.assertIsNone(highest_priority([]))


if __name__ == "__main__":
    unittest.main()
