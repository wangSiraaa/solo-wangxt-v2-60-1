"""窗口提前结束/恢复规则：

- 按步骤配置的可中断点与撤离时间生成待负责人确认的收尾建议；
- 时间不足以满足退出条件时进入明确的异常协调状态；
- 不能把所有步骤强制改成完成；
- 交接未闭合时禁止完成销记。
"""

import unittest

from maintenance_window import (
    ClosingAdvice,
    EventKind,
    ExecutionResponsibility,
    HandoverNotClosed,
    InvalidStepTransition,
    ManualClock,
    PlanState,
    RecommendationNotActionable,
    RecommendationNotConfirmed,
    RecommendationStatus,
    StepState,
    UnresolvedExceptionCoordination,
    WindowError,
)

from .helpers import at, make_coordinator


class EarlyEndTest(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(at(8))
        self.coord = make_coordinator(self.clock)
        self.coord.permits.issue({"P1", "P2"}, valid_until=at(13))
        self.coord.permits.confirm(permit_version=1, protections={"P1", "P2"},
                                   by="防护员-甲", valid_until=at(12, 30))
        self.coord.start_step("A")
        self.coord.start_step("B")
        self.coord.start_step("C")
        self.clock.set(at(9, 30))
        self.coord.complete_step("C")  # 部分步骤已完成
        self.clock.set(at(10))

    def request_compression(self):
        return self.coord.request_early_end(at(10, 30))

    def test_generates_pending_recommendation_from_exit_config(self):
        rec = self.request_compression()
        self.assertIs(rec.status, RecommendationStatus.PENDING_CONFIRMATION)
        self.assertEqual(rec.version, 1)
        items = {i.step_id: i for i in rec.items}

        # A：10 分钟到检查点 + 10 分钟撤离 = 20 分钟 ≤ 剩余 30 分钟
        self.assertIs(items["A"].advice, ClosingAdvice.EXIT_AT_CHECKPOINT)
        self.assertEqual(items["A"].checkpoint, "A-检查点1")
        self.assertEqual(items["A"].evacuate_by, at(10, 20))
        # B：40 + 15 = 55 分钟 > 30 分钟，无法安全退出
        self.assertIs(items["B"].advice, ClosingAdvice.HOLD_FOR_COORDINATION)
        # C：已完成，无需动作
        self.assertIs(items["C"].advice, ClosingAdvice.NO_ACTION)
        # 系统只辅助记录和检查：建议的执行主体是现场人员，不是设备指令
        self.assertTrue(all(i.execution is ExecutionResponsibility.ONSITE_PERSONNEL
                            for i in rec.items))

    def test_insufficient_exit_time_enters_exception_coordination(self):
        self.request_compression()
        self.assertIs(self.coord.plan.state, PlanState.EXCEPTION_COORDINATION)
        self.assertIs(self.coord.plan.step("B").state, StepState.CANNOT_SAFELY_EXIT)
        kinds = [e.kind for e in self.coord.audit]
        self.assertIn(EventKind.EXCEPTION_COORDINATION_ENTERED, kinds)

    def test_blocked_step_cannot_be_forced_to_complete(self):
        self.request_compression()
        with self.assertRaises(InvalidStepTransition):
            self.coord.complete_step("B")
        # 也不能在异常协调未解除时登记撤离
        with self.assertRaises(InvalidStepTransition):
            self.coord.record_step_exited("B")
        self.assertIs(self.coord.plan.step("B").state, StepState.CANNOT_SAFELY_EXIT)

    def test_sign_off_forbidden_while_handover_open(self):
        self.request_compression()
        with self.assertRaises(HandoverNotClosed):
            self.coord.sign_off(by="值班员")

    def test_sign_off_forbidden_while_exception_coordination_open(self):
        self.request_compression()
        # 交接闭合后，异常协调未解除仍禁止销记
        self.coord.offer_handover("白班", "夜班", confirm_by=at(10, 20))
        self.clock.set(at(10, 10))
        self.coord.confirm_handover_receipt(by="夜班班长")
        self.coord.release_handover(by="白班班长")
        with self.assertRaises(UnresolvedExceptionCoordination):
            self.coord.sign_off(by="值班员")

    def test_restore_resolves_exception_and_supersedes_recommendation(self):
        rec1 = self.request_compression()
        self.clock.set(at(10, 10))
        rec2 = self.coord.restore_window(at(12))

        self.assertIs(self.coord.plan.state, PlanState.ACTIVE)
        self.assertIs(self.coord.plan.step("B").state, StepState.IN_PROGRESS)
        self.assertIs(rec1.status, RecommendationStatus.SUPERSEDED)
        self.assertEqual(rec2.version, 2)
        items = {i.step_id: i for i in rec2.items}
        self.assertIs(items["B"].advice, ClosingAdvice.CONTINUE_WORK)
        kinds = [e.kind for e in self.coord.audit]
        self.assertIn(EventKind.EXCEPTION_COORDINATION_RESOLVED, kinds)

        # 已被取代的过期建议不得再确认
        with self.assertRaises(RecommendationNotActionable):
            self.coord.confirm_closing_recommendation(rec1.id, by="负责人")

    def test_invalid_window_adjustments_rejected(self):
        with self.assertRaises(WindowError):
            self.coord.request_early_end(at(13))      # 晚于当前结束时间
        with self.assertRaises(WindowError):
            self.coord.request_early_end(at(9, 30))   # 早于当前时刻
        with self.assertRaises(WindowError):
            self.coord.restore_window(at(11))         # 恢复不能早于当前结束时间

    def test_sign_off_requires_confirmed_recommendation(self):
        self.request_compression()
        self.coord.restore_window(at(12))
        # 步骤全部闭合 + 交接闭合，但建议未确认
        self.coord.record_checkpoint_reached("A", "A-检查点1")
        self.coord.record_step_exited("A")
        self.coord.complete_step("B")
        self.coord.record_step_exited("B")
        self.coord.offer_handover("白班", "夜班", confirm_by=at(11))
        self.clock.set(at(10, 30))
        self.coord.confirm_handover_receipt(by="夜班班长")
        self.coord.release_handover(by="白班班长")
        with self.assertRaises(RecommendationNotConfirmed):
            self.coord.sign_off(by="值班员")

    def test_evacuation_record_does_not_require_permit(self):
        # 撤离是安全动作：即使许可被撤销导致开工资格失效，也允许登记撤离
        self.coord.permits.revoke({"P1"})
        self.coord.record_checkpoint_reached("A", "A-检查点1")
        self.coord.record_step_exited("A")
        self.assertIs(self.coord.plan.step("A").state, StepState.EXITED)


if __name__ == "__main__":
    unittest.main()
