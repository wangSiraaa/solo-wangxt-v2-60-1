"""不变量测试：逐条钉住需求约束。"""

import unittest

from blockade import (
    Confirmation,
    CoordinationState,
    CoordinationStateError,
    DomainError,
    EquipmentCommandForbidden,
    ExpiredConfirmationError,
    HandoverNotClosedError,
    HandoverStateError,
    NotResponsibleError,
    PermitInvalidError,
    StaleConfirmationError,
    StepStatus,
    SuggestionStatus,
    UnsafeExitError,
    WindowState,
    WindowStateError,
)

from helpers import H, M, T0, build_operation, start_all_steps


class HandoverInvariantTest(unittest.TestCase):
    def setUp(self):
        self.op = build_operation()
        start_all_steps(self.op)

    def test_responsibility_stays_with_old_shift_until_acceptance(self):
        self.op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                                  now=T0 + 90 * M, accept_due_at=T0 + 100 * M)
        self.assertEqual(self.op.responsible, "负责人甲")  # 未确认前责任不交
        self.op.accept_handover(T0 + 95 * M, by_shift="夜班")
        self.assertEqual(self.op.responsible, "负责人乙")

    def test_sign_off_forbidden_while_handover_open(self):
        self.op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                                  now=T0 + 90 * M, accept_due_at=T0 + 100 * M)
        with self.assertRaises(HandoverNotClosedError):
            self.op.sign_off_completion(T0 + 95 * M, by="负责人甲")

    def test_late_acceptance_is_flagged(self):
        self.op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                                  now=T0 + 90 * M, accept_due_at=T0 + 100 * M)
        self.op.accept_handover(T0 + 120 * M, by_shift="夜班")
        self.assertTrue(self.op.handover.is_late)

    def test_only_new_shift_can_accept(self):
        self.op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                                  now=T0 + 90 * M, accept_due_at=T0 + 100 * M)
        with self.assertRaises(HandoverStateError):
            self.op.accept_handover(T0 + 95 * M, by_shift="早班")

    def test_second_handover_rejected_while_pending(self):
        self.op.initiate_handover(new_shift="夜班", new_responsible="负责人乙",
                                  now=T0 + 90 * M, accept_due_at=T0 + 100 * M)
        with self.assertRaises(HandoverStateError):
            self.op.initiate_handover(new_shift="中班", new_responsible="负责人丙",
                                      now=T0 + 95 * M, accept_due_at=T0 + 105 * M)


class ExitConditionInvariantTest(unittest.TestCase):
    def setUp(self):
        self.op = build_operation()
        start_all_steps(self.op)

    def test_compression_with_sufficient_exit_time_stays_normal(self):
        # 压缩到 11:30：剩余 150min，远大于 S2 所需 35min；S3 虽不可中断，
        # 但时间充足不构成退出条件违例之外的强制完工——此处仅验证时间维度
        op = build_operation()
        op.activate_window(T0)
        op.record_step_status("S2", StepStatus.IN_PROGRESS, T0 + 10 * M, by="负责人甲")
        suggestion = op.request_early_end(T0 + 210 * M, T0 + 60 * M, by="调度员")
        self.assertIs(op.coordination_state, CoordinationState.NORMAL)
        self.assertIs(suggestion.status, SuggestionStatus.PENDING_CONFIRMATION)

    def test_non_interruptible_step_triggers_exception_on_compression(self):
        self.op.request_early_end(T0 + 150 * M, T0 + 90 * M, by="调度员")
        self.assertIs(self.op.coordination_state, CoordinationState.EXCEPTION_COORDINATION)
        self.assertTrue(any("无法安全退出" in p for p in self.op.last_problems))

    def test_insufficient_time_triggers_exception(self):
        self.op.request_early_end(T0 + 100 * M, T0 + 90 * M, by="调度员")  # 仅剩 10min
        self.assertTrue(any("不足以满足已配置退出条件" in p for p in self.op.last_problems))

    def test_steps_are_never_forced_to_completed(self):
        self.op.request_early_end(T0 + 100 * M, T0 + 90 * M, by="调度员")
        # 异常协调下步骤保持现场报告的原状态
        self.assertIs(self.op.steps["S2"].status, StepStatus.IN_PROGRESS)
        self.assertIs(self.op.steps["S3"].status, StepStatus.IN_PROGRESS)

    def test_resolve_requires_exit_conditions_met(self):
        self.op.request_early_end(T0 + 100 * M, T0 + 90 * M, by="调度员")
        with self.assertRaises(UnsafeExitError):
            self.op.resolve_exception(T0 + 92 * M, by="负责人甲", note="尝试闭合")

    def test_sign_off_blocked_during_exception_coordination(self):
        self.op.request_early_end(T0 + 100 * M, T0 + 90 * M, by="调度员")
        with self.assertRaises(CoordinationStateError):
            self.op.sign_off_completion(T0 + 95 * M, by="负责人甲")

    def test_restore_only_from_compressed(self):
        with self.assertRaises(WindowStateError):
            self.op.restore_window(T0 + 4 * H, T0 + 90 * M, by="调度员")


class PermitInvariantTest(unittest.TestCase):
    def setUp(self):
        self.op = build_operation()
        start_all_steps(self.op)

    def test_permit_versions_increase_monotonically(self):
        self.op.revoke_protection("P1", T0 + 90 * M, by="安全员")
        self.op.revoke_protection("P2", T0 + 95 * M, by="安全员")
        self.op.reinstate_permit(T0 + 100 * M, by="负责人甲", note="重新核验")
        versions = [e.detail["version"] for e in self.op.events
                    if e.kind in ("permit_changed", "permit_reinstated")]
        self.assertEqual(versions, [2, 3, 4])

    def test_stale_confirmation_rejected(self):
        self.op.revoke_protection("P1", T0 + 90 * M, by="安全员")  # 版本 1 -> 2
        stale = Confirmation("C-1", permit_version=1, confirmed_by="安全员",
                             confirmed_at=T0 + 95 * M, expires_at=T0 + 300 * M)
        with self.assertRaises(StaleConfirmationError):
            self.op.apply_confirmation(stale, T0 + 95 * M)

    def test_expired_confirmation_rejected(self):
        expired = Confirmation("C-1", permit_version=1, confirmed_by="安全员",
                               confirmed_at=T0 + 30 * M, expires_at=T0 + 60 * M)
        with self.assertRaises(ExpiredConfirmationError):
            self.op.apply_confirmation(expired, T0 + 90 * M)

    def test_confirmation_never_restores_invalidated_permit(self):
        self.op.revoke_protection("P2", T0 + 90 * M, by="安全员")  # 版本 2，失效
        c = Confirmation("C-1", permit_version=2, confirmed_by="安全员",
                         confirmed_at=T0 + 95 * M, expires_at=T0 + 300 * M)
        with self.assertRaises(PermitInvalidError):
            self.op.apply_confirmation(c, T0 + 95 * M)
        self.assertEqual(self.op.permit.version, 2)
        self.assertEqual(self.op.permit.status.name, "INVALIDATED")

    def test_valid_confirmation_is_recorded(self):
        c = Confirmation("C-1", permit_version=1, confirmed_by="安全员",
                         confirmed_at=T0 + 90 * M, expires_at=T0 + 300 * M)
        self.op.apply_confirmation(c, T0 + 90 * M)
        self.assertTrue(any(e.kind == "confirmation_accepted" for e in self.op.events))
        self.assertEqual(self.op.permit.version, 1)  # 确认不改变许可

    def test_completion_record_requires_valid_permit(self):
        self.op.revoke_protection("P2", T0 + 90 * M, by="安全员")
        with self.assertRaises(PermitInvalidError):
            self.op.record_step_status("S2", StepStatus.COMPLETED, T0 + 95 * M, by="负责人甲")
        with self.assertRaises(PermitInvalidError):
            self.op.record_step_status("S1", StepStatus.IN_PROGRESS, T0 + 95 * M, by="负责人甲")
        # 安全类状态记录不受许可限制
        self.op.record_step_status("S2", StepStatus.EVACUATED, T0 + 96 * M, by="负责人甲")


class SuggestionInvariantTest(unittest.TestCase):
    def setUp(self):
        self.op = build_operation()
        start_all_steps(self.op)

    def test_suggestion_requires_current_responsible_confirmation(self):
        s = self.op.request_early_end(T0 + 150 * M, T0 + 90 * M, by="调度员")
        with self.assertRaises(NotResponsibleError):
            self.op.confirm_suggestion(s.suggestion_id, T0 + 95 * M, by="无关人员")
        self.op.confirm_suggestion(s.suggestion_id, T0 + 96 * M, by="负责人甲")
        self.assertIs(s.status, SuggestionStatus.CONFIRMED)

    def test_new_assessment_supersedes_pending_suggestion(self):
        first = self.op.request_early_end(T0 + 150 * M, T0 + 90 * M, by="调度员")
        self.op.restore_window(T0 + 4 * H, T0 + 100 * M, by="调度员")
        self.assertIs(first.status, SuggestionStatus.SUPERSEDED)
        with self.assertRaises(DomainError):
            self.op.confirm_suggestion(first.suggestion_id, T0 + 101 * M, by="负责人甲")

    def test_sign_off_blocked_while_suggestion_pending(self):
        op = build_operation()
        op.activate_window(T0)
        op.record_step_status("S2", StepStatus.IN_PROGRESS, T0 + 10 * M, by="负责人甲")
        op.request_early_end(T0 + 210 * M, T0 + 60 * M, by="调度员")  # 时间充足，保持 NORMAL
        with self.assertRaises(DomainError):
            op.sign_off_completion(T0 + 70 * M, by="负责人甲")


class SystemBoundaryTest(unittest.TestCase):
    def test_equipment_commands_are_forbidden(self):
        op = build_operation()
        with self.assertRaises(EquipmentCommandForbidden):
            op.issue_equipment_command("open_valve", target="V-101")


if __name__ == "__main__":
    unittest.main()
