"""组合请求测试：窗口压缩后又恢复 + 交班确认迟到 + 部分保护确认撤销。

时间线（虚拟时钟，T0 = 08:00，窗口计划 08:00-12:00）：
  09:30 调度要求 10:00 提前结束 -> 退出条件不满足，进入异常协调
  09:35 发起交接（要求 09:45 前确认）
  09:40 交接未闭合，销记被拒绝
  09:50 新班迟到确认，责任转移
  09:55 窗口恢复至 12:00（异常协调不自动清除）
  10:00 部分保护确认撤销 -> 许可失效（版本 1->2）
  10:05 过期/失效确认一律不得恢复开工资格
  10:10 新负责人确认当前收尾建议
  10:15 人工重新签发许可（版本 2->3）
  10:20 现场报告安全状态（停点/撤离）
  10:25 负责人闭合异常协调
  10:30 按实际状态完成销记
"""

import unittest

from blockade import (
    Confirmation,
    CoordinationState,
    ExpiredConfirmationError,
    HandoverNotClosedError,
    NotResponsibleError,
    PermitInvalidError,
    StaleConfirmationError,
    StepStatus,
    SuggestionAction,
    SuggestionStatus,
    WindowState,
)

from helpers import H, M, T0, build_operation, start_all_steps


class CombinedScenarioTest(unittest.TestCase):
    def setUp(self):
        self.op = build_operation()
        start_all_steps(self.op)

    def test_compress_restore_late_handover_and_partial_revocation(self):
        op = self.op

        # ---- 09:30 窗口压缩到 10:00：剩余 30min < S2 所需 35min，S3 无法安全退出
        suggestion = op.request_early_end(T0 + 120 * M, T0 + 90 * M, by="调度员")
        self.assertIs(op.window.state, WindowState.COMPRESSED)
        self.assertIs(op.coordination_state, CoordinationState.EXCEPTION_COORDINATION)
        self.assertEqual(len(op.last_problems), 2)  # 时间不足 + 无法安全退出
        self.assertTrue(suggestion.requires_human_confirmation)
        self.assertIs(suggestion.status, SuggestionStatus.PENDING_CONFIRMATION)
        # 系统不强制把步骤改成完成
        self.assertIs(op.steps["S2"].status, StepStatus.IN_PROGRESS)
        self.assertIs(op.steps["S3"].status, StepStatus.IN_PROGRESS)
        actions = {i.step_id: i.action for i in suggestion.items}
        self.assertIs(actions["S3"], SuggestionAction.CANNOT_SAFELY_EXIT)
        self.assertIs(actions["S2"], SuggestionAction.PARK_AT_INTERRUPTIBLE_POINT)

        # ---- 09:35 发起交接，责任仍属旧班
        op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                             now=T0 + 95 * M, accept_due_at=T0 + 105 * M)
        self.assertEqual(op.responsible, "负责人甲")

        # ---- 09:40 交接未闭合，禁止完成销记
        with self.assertRaises(HandoverNotClosedError):
            op.sign_off_completion(T0 + 100 * M, by="负责人甲")

        # ---- 09:50 新班迟到确认，交接闭合且标记迟到
        op.accept_handover(T0 + 110 * M, by_shift="夜班")
        self.assertTrue(op.handover.is_closed)
        self.assertTrue(op.handover.is_late)
        self.assertEqual(op.responsible, "负责人乙")
        # 旧负责人已无权确认建议
        with self.assertRaises(NotResponsibleError):
            op.confirm_suggestion(suggestion.suggestion_id, T0 + 111 * M, by="负责人甲")

        # ---- 09:55 窗口恢复至 12:00：异常协调不自动清除，许可不受影响
        op.restore_window(T0 + 4 * H, T0 + 115 * M, by="调度员")
        self.assertIs(op.window.state, WindowState.RESTORED)
        self.assertEqual(op.window.revision, 2)
        self.assertIs(op.coordination_state, CoordinationState.EXCEPTION_COORDINATION)

        # ---- 10:00 部分保护确认撤销（S3 所需接地保护）-> 许可失效，版本递增
        op.revoke_protection("P2", T0 + 120 * M, by="安全员")
        self.assertIs(op.permit.status.name, "INVALIDATED")
        self.assertEqual(op.permit.version, 2)
        self.assertIs(op.coordination_state, CoordinationState.EXCEPTION_COORDINATION)

        # ---- 10:05 过期确认不得恢复已失效的开工资格
        stale = Confirmation("C-1", permit_version=1, confirmed_by="安全员",
                             confirmed_at=T0 + 125 * M, expires_at=T0 + 300 * M)
        with self.assertRaises(StaleConfirmationError):
            op.apply_confirmation(stale, T0 + 125 * M)
        expired = Confirmation("C-2", permit_version=2, confirmed_by="安全员",
                               confirmed_at=T0 + 90 * M, expires_at=T0 + 110 * M)
        with self.assertRaises(ExpiredConfirmationError):
            op.apply_confirmation(expired, T0 + 125 * M)
        current_but_invalid = Confirmation("C-3", permit_version=2, confirmed_by="安全员",
                                           confirmed_at=T0 + 125 * M, expires_at=T0 + 300 * M)
        with self.assertRaises(PermitInvalidError):
            op.apply_confirmation(current_but_invalid, T0 + 125 * M)
        self.assertEqual(op.permit.version, 2)  # 资格未恢复，版本未被搅动

        # ---- 许可失效期间禁止记录完工，系统不强制改写
        with self.assertRaises(PermitInvalidError):
            op.record_step_status("S3", StepStatus.COMPLETED, T0 + 126 * M, by="负责人乙")
        self.assertIs(op.steps["S3"].status, StepStatus.IN_PROGRESS)

        # ---- 10:10 新负责人确认当前收尾建议（恢复+撤销后重新生成的版本）
        current = op.suggestions[-1]
        self.assertIs(current.status, SuggestionStatus.PENDING_CONFIRMATION)
        op.confirm_suggestion(current.suggestion_id, T0 + 130 * M, by="负责人乙")
        self.assertIs(current.status, SuggestionStatus.CONFIRMED)

        # ---- 10:15 人工重新签发许可，版本递增
        op.reinstate_permit(T0 + 135 * M, by="负责人乙", note="保护恢复并重新核验")
        self.assertEqual(op.permit.version, 3)
        # 重新签发前的旧版本确认依然无效
        old = Confirmation("C-4", permit_version=2, confirmed_by="安全员",
                           confirmed_at=T0 + 136 * M, expires_at=T0 + 300 * M)
        with self.assertRaises(StaleConfirmationError):
            op.apply_confirmation(old, T0 + 136 * M)

        # ---- 10:20 现场报告安全状态（安全类记录随时允许）
        op.record_step_status("S2", StepStatus.PAUSED_AT_INTERRUPTIBLE_POINT,
                              T0 + 140 * M, by="负责人乙")
        op.record_step_status("S3", StepStatus.EVACUATED, T0 + 145 * M, by="负责人乙")

        # ---- 10:25 负责人闭合异常协调
        op.resolve_exception(T0 + 150 * M, by="负责人乙", note="人员撤离，现场安全")
        self.assertIs(op.coordination_state, CoordinationState.NORMAL)

        # ---- 10:30 完成销记：按实际状态记录
        record = op.sign_off_completion(T0 + 155 * M, by="负责人乙")
        self.assertEqual(record.step_statuses, {
            "S1": StepStatus.COMPLETED,
            "S2": StepStatus.PAUSED_AT_INTERRUPTIBLE_POINT,
            "S3": StepStatus.EVACUATED,
        })
        self.assertTrue(record.handover_late)
        self.assertEqual(record.permit_version, 3)
        self.assertIs(record.window_state, WindowState.RESTORED)

        # ---- 所有许可变更版本严格递增
        versions = [e.detail["version"] for e in op.events
                    if e.kind in ("permit_changed", "permit_reinstated")]
        self.assertEqual(versions, [2, 3])


if __name__ == "__main__":
    unittest.main()
