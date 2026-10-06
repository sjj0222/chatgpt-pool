"""V0.3 Phase 4 评分系统测试。

覆盖：稳定性/可用率窗口加权/延迟/信用（§7）、breakdown 可展示（§28）、
service_score 多维（§8）、capability 标签（§9）、时间衰减（§15）、
评分仅排序不改准入（P1-03）。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "chatgpt-pool"))

import scoring                                            # noqa: E402
from models import (AVAILABILITY_WINDOWS, ChatgptStatus,   # noqa: E402
                    SCORE_WEIGHTS)


def _rec(**kw):
    rec = {"success_count": 0, "fail_count": 0, "fail_streak": 0,
           "recover_count": 0, "recent_attempts": [], "score_history": [],
           "probe_scores": {}}
    rec.update(kw)
    return rec


def _attempt(ts, ok, latency_ms=None):
    return {"t": ts, "ok": ok, "latency_ms": latency_ms}


class TestStability(unittest.TestCase):
    def test_ratio(self):
        rec = _rec(success_count=9, fail_count=1)
        self.assertEqual(scoring.stability_score(rec), 90.0)

    def test_empty(self):
        self.assertEqual(scoring.stability_score(_rec()), 0.0)

    def test_all_fail(self):
        rec = _rec(success_count=0, fail_count=4)
        self.assertEqual(scoring.stability_score(rec), 0.0)


class TestAvailability(unittest.TestCase):
    def test_window_weighted(self):
        """5m/1h/24h/7d 嵌套加权（近窗重；长窗口包含短窗口记录）。"""
        now = 100_000
        rec = _rec(recent_attempts=[
            _attempt(now - 100, True),      # 落在全部 4 窗口
            _attempt(now - 1000, False),    # 落在 1h/24h/7d
            _attempt(now - 5000, True),     # 落在 24h/7d
            _attempt(now - 90_000, False),  # 仅 7d
        ])
        # 5m:100% · 1h:50% · 24h:66.7% · 7d:50%
        w = scoring.WINDOW_WEIGHTS
        expected = round(
            100.0 * w["5m"] + 50.0 * w["1h"]
            + round(100.0 * 2 / 3, 1) * w["24h"] + 50.0 * w["7d"], 1)
        self.assertEqual(scoring.availability_score(rec, now=now),
                         expected)
        self.assertEqual(scoring.availability_score(rec, now=now), 73.3)

    def test_no_attempts(self):
        self.assertEqual(scoring.availability_score(_rec(), now=100_000), 0.0)

    def test_windows_defined(self):
        self.assertEqual(set(AVAILABILITY_WINDOWS),
                         {"5m", "1h", "24h", "7d"})


class TestLatency(unittest.TestCase):
    def test_fast_full_score(self):
        rec = _rec(recent_attempts=[_attempt(1, True, 250)])
        self.assertEqual(scoring.latency_score(rec), 100.0)

    def test_linear_penalty(self):
        rec = _rec(recent_attempts=[_attempt(1, True, 500)])
        self.assertEqual(scoring.latency_score(rec), 90.0)

    def test_floor_zero(self):
        rec = _rec(recent_attempts=[_attempt(1, True, 2500)])
        self.assertEqual(scoring.latency_score(rec), 0.0)


class TestCredit(unittest.TestCase):
    def test_base_plus_history_decay(self):
        """§15：24h 70% + 30d 30% 时间衰减。"""
        now = int(time.time())
        rec = _rec(score_history=[
            {"t": now - 600, "overall": 90},         # 24h 内
            {"t": now - 10 * 86400, "overall": 50},  # 30 天内
        ])
        c = scoring.credit_score(rec)
        expected = (60.0 + (90 * .70 + 50 * .30) * 0.20)
        self.assertEqual(c, round(expected, 1))

    def test_recover_bonus_capped(self):
        rec = _rec(recover_count=20)
        c = scoring.credit_score(rec)
        self.assertEqual(c, 80.0)   # 60 + 20（上限）

    def test_long_term_bonus(self):
        rec = _rec(success_count=40, fail_streak=0)
        self.assertEqual(scoring.credit_score(rec), 65.0)

    def test_clipped(self):
        rec = _rec(recover_count=100, success_count=100, fail_streak=0,
                   score_history=[{"t": int(1e9), "overall": 100}])
        self.assertLessEqual(scoring.credit_score(rec), 100.0)


class TestQualityScore(unittest.TestCase):
    def test_overall_from_breakdown(self):
        rec = _rec(success_count=9, fail_count=1,
                   recent_attempts=[_attempt(1, True, 400)],
                   recover_count=1)
        q = scoring.compute_quality_score(rec, now=1_000_000)
        expected = round(
            q.stability * SCORE_WEIGHTS["stability"]
            + q.availability * SCORE_WEIGHTS["availability"]
            + q.latency * SCORE_WEIGHTS["latency"]
            + q.credit * SCORE_WEIGHTS["credit"], 1)
        self.assertEqual(q.overall, expected)
        # §28：breakdown 可单独展示
        for k in ("overall", "stability", "availability", "latency",
                  "credit"):
            self.assertIn(k, q.to_dict())

    def test_zero_new_node(self):
        """新节点无数据：credit 基础分 60 → overall=6.0（低分，不能直接 ACTIVE）。"""
        q = scoring.compute_quality_score(_rec(), now=1_000_000)
        self.assertEqual(q.overall, 6.0)


class TestServiceScore(unittest.TestCase):
    def test_chatgpt_strict_pass(self):
        rec = _rec()
        ss = scoring.compute_service_score(rec, ChatgptStatus.STRICT_PASS.value)
        self.assertEqual(ss.services["chatgpt"], 95.0)
        self.assertEqual(ss.overall, 95.0)

    def test_chatgpt_network_pass(self):
        ss = scoring.compute_service_score(_rec(),
                                           ChatgptStatus.NETWORK_PASS.value)
        self.assertEqual(ss.services["chatgpt"], 60.0)

    def test_chatgpt_fail(self):
        ss = scoring.compute_service_score(_rec(), ChatgptStatus.FAIL.value)
        self.assertEqual(ss.services["chatgpt"], 0.0)

    def test_probe_scores_included(self):
        rec = _rec(probe_scores={"github": 90.0, "web": 55.0})
        ss = scoring.compute_service_score(rec, ChatgptStatus.STRICT_PASS.value)
        self.assertEqual(ss.services["github"], 90.0)
        self.assertEqual(ss.services["web"], 55.0)
        self.assertEqual(ss.overall, round((95 + 90 + 55) / 3, 1))

    def test_empty_services(self):
        ss = scoring.compute_service_score(_rec(), "")
        self.assertEqual(ss.services, {})
        self.assertEqual(ss.overall, 0.0)


class TestCapability(unittest.TestCase):
    def test_tags_by_threshold(self):
        ss = scoring.compute_service_score(
            _rec(probe_scores={"github": 90.0, "web": 50.0}),
            ChatgptStatus.STRICT_PASS.value)
        tags = scoring.capability_tags(ss)
        self.assertIn("chatgpt", tags)
        self.assertIn("github", tags)
        self.assertNotIn("web", tags)   # 50 < 70


class TestRecords(unittest.TestCase):
    def test_record_attempt_limit(self):
        rec = _rec()
        for i in range(scoring.ATTEMPTS_LIMIT + 50):
            scoring.record_attempt(rec, True, 100, ts=i)
        self.assertEqual(len(rec["recent_attempts"]), scoring.ATTEMPTS_LIMIT)

    def test_record_score_history_limit(self):
        rec = _rec()
        for i in range(60):
            scoring.record_score_history(rec, float(i), ts=i)
        self.assertEqual(len(rec["score_history"]), 50)
        self.assertEqual(rec["score_history"][-1]["overall"], 59.0)


if __name__ == "__main__":
    unittest.main()
