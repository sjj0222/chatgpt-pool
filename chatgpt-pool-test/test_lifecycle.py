"""V0.3 Phase 2 Node 生命周期状态机测试。

覆盖：晋升路径（PROBATION→STANDBY→ACTIVE，禁止新节点直接 ACTIVE）、
降级（1-3 次失败观察、连续失败 DEGRADED/COOLDOWN）、
冷却（10min/1h/24h 升级）、恢复（RECOVERING→ACTIVE / →FAILED）、
删除（30 天 EXPIRED）、BLACKLIST 不可逆。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

from lifecycle import (                                  # noqa: E402
    CONSECUTIVE_FAIL_COOLDOWN, CONSECUTIVE_FAIL_DEGRADE,
    COOLDOWN_ESCALATE_SECONDS, COOLDOWN_FIRST_SECONDS,
    COOLDOWN_SEVERE_SECONDS, FAILED_EXPIRY_SECONDS,
    PROBATION_PASS_THRESHOLD, LifecycleEngine, cooldown_seconds)
from models import LifecycleState, NodeEventType        # noqa: E402


def _rec(state, **kw):
    rec = {"life_state": state, "fail_streak": 0, "success_streak": 0,
           "cooldown_count": 0, "cooldown_until": 0,
           "last_success": "", "last_failure": 0}
    rec.update(kw)
    return rec


class TestPromotion(unittest.TestCase):
    """§10：新节点必经 PROBATION，禁止直接 ACTIVE。"""

    def test_new_node_path(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.DISCOVERED.value)
        s, ev, _ = eng.apply_result(rec, True, now=1000)
        self.assertEqual(s, LifecycleState.PROBATION.value)  # 不能直接 ACTIVE
        self.assertEqual(ev, NodeEventType.VALIDATED.value)
        self.assertEqual(rec["success_streak"], 1)

    def test_probation_passes_to_standby(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.PROBATION.value)
        for _ in range(PROBATION_PASS_THRESHOLD):
            s, ev, _ = eng.apply_result(rec, True, now=1000)
        self.assertEqual(s, LifecycleState.STANDBY.value)
        self.assertEqual(ev, NodeEventType.PROMOTED.value)

    def test_probation_explicit_pass(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.PROBATION.value, success_streak=1)
        s, ev, _ = eng.apply_result(rec, True, now=1000, probation_pass=True)
        self.assertEqual(s, LifecycleState.STANDBY.value)

    def test_standby_to_active_on_score(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.STANDBY.value)
        s, ev, _ = eng.apply_result(rec, True, now=1000, score_ok=True)
        self.assertEqual(s, LifecycleState.ACTIVE.value)
        self.assertEqual(ev, NodeEventType.PROMOTED.value)

    def test_standby_blocks_below_threshold(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.STANDBY.value)
        s, _, _ = eng.apply_result(rec, True, now=1000, score_ok=False)
        self.assertEqual(s, LifecycleState.STANDBY.value)  # 评分不足不晋升


class TestDegradation(unittest.TestCase):
    """§11：禁止一次失败删除；连续失败才降级。"""

    def test_single_fail_keeps_active(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.ACTIVE.value)
        s, ev, _ = eng.apply_result(rec, False, now=1000)
        self.assertEqual(s, LifecycleState.ACTIVE.value)   # 1 次失败只观察
        self.assertIsNone(ev)
        self.assertEqual(rec["fail_streak"], 1)

    def test_consecutive_fails_degrade(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.ACTIVE.value)
        for i in range(CONSECUTIVE_FAIL_DEGRADE):
            s, ev, _ = eng.apply_result(rec, False, now=1000 + i)
        self.assertEqual(s, LifecycleState.DEGRADED.value)
        self.assertEqual(ev, NodeEventType.DEGRADED.value)

    def test_degraded_to_cooldown(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.DEGRADED.value)
        for i in range(CONSECUTIVE_FAIL_COOLDOWN):
            s, ev, _ = eng.apply_result(rec, False, now=2000 + i)
        self.assertEqual(s, LifecycleState.COOLDOWN.value)
        self.assertEqual(ev, NodeEventType.COOLDOWN.value)
        self.assertTrue(rec["cooldown_until"] > 2000)

    def test_probation_fail_does_not_delete(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.PROBATION.value)
        s, _, _ = eng.apply_result(rec, False, now=1000)
        self.assertIn(s, (LifecycleState.PROBATION.value,
                          LifecycleState.DEGRADED.value))  # 不删除


class TestCooldown(unittest.TestCase):
    """§12：10 分钟 / 1 小时 / 24 小时升级。"""

    def test_cooldown_seconds_escalation(self):
        self.assertEqual(cooldown_seconds(1), COOLDOWN_FIRST_SECONDS)
        self.assertEqual(cooldown_seconds(2), COOLDOWN_ESCALATE_SECONDS)
        self.assertEqual(cooldown_seconds(3), COOLDOWN_SEVERE_SECONDS)
        self.assertEqual(cooldown_seconds(5), COOLDOWN_SEVERE_SECONDS)

    def test_cooldown_expiry_recovering(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.COOLDOWN.value,
                   cooldown_until=1000 + COOLDOWN_FIRST_SECONDS)
        s = eng.should_recover(rec, now=1000 + COOLDOWN_FIRST_SECONDS)
        self.assertEqual(s, LifecycleState.RECOVERING.value)
        # 未到期保持
        s2 = eng.should_recover(rec, now=1000 + 60)
        self.assertEqual(s2, LifecycleState.COOLDOWN.value)

    def test_recover_success_active(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.RECOVERING.value)
        s, ev, _ = eng.apply_result(rec, True, now=3000)
        self.assertEqual(s, LifecycleState.ACTIVE.value)
        self.assertEqual(ev, NodeEventType.RECOVERED.value)

    def test_recover_fail_failed(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.RECOVERING.value)
        s, ev, _ = eng.apply_result(rec, False, now=3000)
        self.assertEqual(s, LifecycleState.FAILED.value)
        self.assertEqual(ev, NodeEventType.DEGRADED.value)


class TestExpiry(unittest.TestCase):
    """§13：FAILED 30 天无恢复 → EXPIRED（事件表达）。"""

    def test_failed_expires_after_30d(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.FAILED.value, last_failure=1000)
        self.assertFalse(eng.is_expired(rec, now=1000 + 29 * 86400))
        self.assertTrue(eng.is_expired(rec, now=1000 + 31 * 86400))

    def test_failed_keeps_state_on_more_fails(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.FAILED.value, last_failure=1000)
        s, _, _ = eng.apply_result(rec, False, now=2000)
        self.assertEqual(s, LifecycleState.FAILED.value)

    def test_failed_can_recover_before_expiry(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.FAILED.value, last_failure=1000)
        s, ev, _ = eng.apply_result(rec, True, now=2000)
        self.assertEqual(s, LifecycleState.ACTIVE.value)
        self.assertEqual(ev, NodeEventType.RECOVERED.value)


class TestBlacklist(unittest.TestCase):
    """BLACKLIST 永久，不可逆。"""

    def test_blacklist_immutable_on_success(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.BLACKLIST.value)
        s, _, _ = eng.apply_result(rec, True, now=1000)
        self.assertEqual(s, LifecycleState.BLACKLIST.value)

    def test_blacklist_immutable_on_fail(self):
        eng = LifecycleEngine()
        rec = _rec(LifecycleState.BLACKLIST.value)
        s, _, _ = eng.apply_result(rec, False, now=1000)
        self.assertEqual(s, LifecycleState.BLACKLIST.value)


if __name__ == "__main__":
    unittest.main()
