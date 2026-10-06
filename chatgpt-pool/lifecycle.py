"""V0.3 节点生命周期状态机（§4/§5/§10/§11/§12/§13）。

与单轮检测管线（Stage，§7）独立：LifecycleState 描述节点跨轮生命周期位置。
规则（§10-13）：
- 新节点晋升：DISCOVERED→VALIDATED→PROBING→PROBATION→STANDBY→ACTIVE；
  禁止新节点直接替换老节点（PROBATION 是必经观察期）。
- 降级（§11）：禁止一次失败删除；1-3 次失败观察；连续失败 ACTIVE→DEGRADED；
  长期 DEGRADED→COOLDOWN。
- 冷却（§12）：第一次 10 分钟，连续 1 小时，严重 24 小时；到期重新 PROBING。
- 恢复：RECOVERING 成功→ACTIVE，失败→FAILED。
- 删除（§13）：FAILED→COOLDOWN→EXPIRED→DELETE；30 天无恢复删除；
  BLACKLIST 永久淘汰。
"""
import time

# 阈值与时长（可配置）
CONSECUTIVE_FAIL_DEGRADE = 3      # 连续失败 ≥3 → DEGRADED（§11）
CONSECUTIVE_FAIL_COOLDOWN = 3     # DEGRADED 连续失败 ≥3 → COOLDOWN
PROBATION_PASS_THRESHOLD = 3      # PROBATION 连续成功 ≥3 → STANDBY
COOLDOWN_FIRST_SECONDS = 600      # 第一次 10 分钟（§12）
COOLDOWN_ESCALATE_SECONDS = 3600  # 连续 1 小时
COOLDOWN_SEVERE_SECONDS = 86400   # 严重 24 小时
FAILED_EXPIRY_SECONDS = 30 * 86400  # FAILED 30 天无恢复 → EXPIRED（§13）

from models import LifecycleState, NodeEventType


def cooldown_seconds(cooldown_count):
    """§12：冷却时长——首次 10 分钟、连续 1 小时、严重（≥3 次）24 小时。"""
    if cooldown_count >= 3:
        return COOLDOWN_SEVERE_SECONDS
    if cooldown_count == 2:
        return COOLDOWN_ESCALATE_SECONDS
    return COOLDOWN_FIRST_SECONDS


class LifecycleEngine:
    """以"本轮检测结果"驱动状态迁移。

    ctx 约定（rec：lifecycle 记录 dict）：
      rec["life_state"]: 当前状态
      rec["fail_streak"]: 当前连续失败次数（降级观察用）
      rec["success_streak"]: 当前连续成功次数（晋升观察用）
      rec["cooldown_count"]: 冷却累计次数（时长升级用）
      rec["cooldown_until"]: 冷却到期时间戳（秒）
      rec["last_failure"]: 最后失败时间戳（EXPIRED 判定用）
    """

    def apply_result(self, rec, probe_success, now=None,
                     score_ok=None, probation_pass=None):
        """返回 (new_state, event, detail)。

        probe_success：本轮检测是否通过（STRICT_PASS + 稳定性）。
        score_ok：评分是否达 ACTIVE 阈值（Phase 4 接入；None 视为达标）。
        probation_pass：PROBATION 观察是否通过（None 时按 success_streak 判定）。
        """
        now = now if now is not None else int(time.time())
        cur = rec.get("life_state", LifecycleState.DISCOVERED.value)
        fail_streak = int(rec.get("fail_streak", 0))
        success_streak = int(rec.get("success_streak", 0))
        cooldown_count = int(rec.get("cooldown_count", 0))
        cooldown_until = rec.get("cooldown_until", 0) or 0

        if probe_success:
            return self._on_success(
                cur, rec, success_streak, fail_streak, cooldown_count,
                cooldown_until, now, score_ok, probation_pass)
        return self._on_fail(
            cur, rec, fail_streak, success_streak, cooldown_count,
            cooldown_until, now)

    # ---- 成功路径 ----
    def _on_success(self, cur, rec, ss, fs, cc, cu, now, score_ok,
                    probation_pass):
        new = cur
        event = None
        if cur in (LifecycleState.DISCOVERED.value,
                   LifecycleState.VALIDATED.value,
                   LifecycleState.PROBING.value):
            # 首次通过进入观察期（PROBATION 必经，§10）
            new = LifecycleState.PROBATION.value
            event = NodeEventType.VALIDATED.value
            ss = 1
        elif cur == LifecycleState.PROBATION.value:
            ss = ss + 1
            passed = (probation_pass if probation_pass is not None
                      else ss >= PROBATION_PASS_THRESHOLD)
            if passed:
                new = LifecycleState.STANDBY.value
                event = NodeEventType.PROMOTED.value
        elif cur == LifecycleState.STANDBY.value:
            # 备用节点评分达标 → ACTIVE（§6/§10）
            if score_ok is None or score_ok:
                new = LifecycleState.ACTIVE.value
                event = NodeEventType.PROMOTED.value
        elif cur == LifecycleState.DEGRADED.value:
            # 降级节点恢复成功 → RECOVERING → ACTIVE（§11/§12）
            new = LifecycleState.RECOVERING.value
            event = NodeEventType.RECOVERED.value
        elif cur in (LifecycleState.COOLDOWN.value,
                     LifecycleState.RECOVERING.value,
                     LifecycleState.FAILED.value):
            # 冷却/恢复/失败节点重新检测成功 → 恢复 ACTIVE
            new = LifecycleState.ACTIVE.value
            event = NodeEventType.RECOVERED.value
        # ACTIVE 保持；BLACKLIST 不可逆
        rec.update(fail_streak=0, success_streak=ss,
                   cooldown_count=0, cooldown_until=0,
                   last_success=now)
        return new, event, {}

    # ---- 失败路径 ----
    def _on_fail(self, cur, rec, fs, ss, cc, cu, now):
        new = cur
        event = None
        if cur in (LifecycleState.DISCOVERED.value,
                   LifecycleState.VALIDATED.value,
                   LifecycleState.PROBING.value):
            # 基础/检测阶段失败 → FAILED（可恢复终点态，§13）
            new = LifecycleState.FAILED.value
            event = NodeEventType.DEGRADED.value
            fs = fs + 1
        elif cur in (LifecycleState.PROBATION.value,
                     LifecycleState.STANDBY.value,
                     LifecycleState.ACTIVE.value):
            # 短期观察（§11）：1-3 次失败只观察，连续失败才降级
            fs = fs + 1
            if fs >= CONSECUTIVE_FAIL_DEGRADE:
                new = LifecycleState.DEGRADED.value
                event = NodeEventType.DEGRADED.value
        elif cur == LifecycleState.DEGRADED.value:
            fs = fs + 1
            if fs >= CONSECUTIVE_FAIL_COOLDOWN:
                cc = cc + 1
                new = LifecycleState.COOLDOWN.value
                event = NodeEventType.COOLDOWN.value
                rec["cooldown_until"] = now + cooldown_seconds(cc)
        elif cur == LifecycleState.COOLDOWN.value:
            # 冷却中失败：保持冷却，冷却时长按次数升级
            if now >= (cu or 0) and fs + 1 >= CONSECUTIVE_FAIL_COOLDOWN:
                cc = cc + 1
                rec["cooldown_until"] = now + cooldown_seconds(cc)
            fs = fs + 1
        elif cur == LifecycleState.RECOVERING.value:
            new = LifecycleState.FAILED.value
            event = NodeEventType.DEGRADED.value
            fs = fs + 1
        elif cur == LifecycleState.FAILED.value:
            # 已 FAILED 再失败：保持 FAILED（过期删除由 is_expired/cleanup 判定，
            # EXPIRED 通过 NodeEventType.EXPIRED 事件表达，不新增状态）
            fs = fs + 1
        rec.update(fail_streak=fs, success_streak=0,
                   last_failure=now)
        return new, event, {}

    def should_recover(self, rec, now=None):
        """COOLDOWN 到期 → 重新 PROBING（§12）。"""
        now = now if now is not None else int(time.time())
        if rec.get("life_state") == LifecycleState.COOLDOWN.value:
            if now >= int(rec.get("cooldown_until", 0) or 0):
                return LifecycleState.RECOVERING.value
        return rec.get("life_state")

    def is_expired(self, rec, now=None):
        """FAILED 超过 30 天无恢复 → EXPIRED（§13 删除流程）。"""
        now = now if now is not None else int(time.time())
        if rec.get("life_state") == LifecycleState.FAILED.value:
            last = int(rec.get("last_failure", 0) or 0)
            return now - last >= FAILED_EXPIRY_SECONDS
        return False
