"""组合场景验收测试：

一个正在执行的封锁窗口被要求提前结束，同时换班。时间线：
  08:00 许可 v1 签发并确认，A/B/C 开工；09:30 步骤 C 完成
  10:00 窗口压缩到 10:30 → A 可撤离、B 无法安全退出 → 异常协调状态
  10:10 窗口恢复到 12:00 → 异常协调解除，建议版本更替
  10:20 发起交接（截止 10:30）；10:35 新班确认迟到 → 交接未闭合，禁止销记
  10:40 重新发起交接；10:45 确认、10:46 旧班交出责任 → 交接闭合
  10:50 部分保护确认撤销（许可 v2）→ B 开工资格失效，过期确认不得恢复
  11:15 恢复 P2（许可 v3）→ 重新确认后 B 复工并完成
  11:30 负责人确认收尾建议；11:35 销记完成
"""

import unittest

from maintenance_window import (
    ClosingAdvice,
    EventKind,
    ExecutionResponsibility,
    HandoverNotClosed,
    HandoverNotConfirmed,
    HandoverState,
    InvalidStepTransition,
    LateHandoverConfirmation,
    ManualClock,
    PlanState,
    RecommendationStatus,
    StalePermitVersion,
    StepState,
    WorkNotEligible,
)

from .helpers import at, make_coordinator


class CombinedScenarioTest(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(at(8))
        self.coord = make_coordinator(self.clock)

    def test_compress_restore_late_handover_and_partial_revoke(self):
        coord = self.coord

        # ---- 08:00 许可 v1 签发并确认，步骤开工 ----
        permit_v1 = coord.permits.issue({"P1", "P2"}, valid_until=at(13))
        self.assertEqual(permit_v1.version, 1)
        coord.permits.confirm(permit_version=1, protections={"P1", "P2"},
                              by="防护员-甲", valid_until=at(12, 30))
        coord.start_step("A")
        coord.start_step("B")
        coord.start_step("C")
        self.clock.set(at(9, 30))
        coord.complete_step("C")  # 部分步骤已完成

        # ---- 10:00 窗口压缩到 10:30 ----
        self.clock.set(at(10))
        rec1 = coord.request_early_end(at(10, 30))
        self.assertIs(rec1.status, RecommendationStatus.PENDING_CONFIRMATION)
        items = {i.step_id: i for i in rec1.items}
        self.assertIs(items["A"].advice, ClosingAdvice.EXIT_AT_CHECKPOINT)
        self.assertIs(items["B"].advice, ClosingAdvice.HOLD_FOR_COORDINATION)
        self.assertIs(items["C"].advice, ClosingAdvice.NO_ACTION)
        self.assertTrue(all(i.execution is ExecutionResponsibility.ONSITE_PERSONNEL
                            for i in rec1.items))
        # 缩短后的时间不满足 B 的退出条件 → 明确的异常协调状态
        self.assertIs(coord.plan.state, PlanState.EXCEPTION_COORDINATION)
        self.assertIs(coord.plan.step("B").state, StepState.CANNOT_SAFELY_EXIT)
        # 不能把所有步骤强制改成完成
        with self.assertRaises(InvalidStepTransition):
            coord.complete_step("B")
        # 交接未闭合（尚未发起）禁止销记
        with self.assertRaises(HandoverNotClosed):
            coord.sign_off(by="值班员")

        # ---- 10:10 窗口恢复到 12:00 ----
        self.clock.set(at(10, 10))
        rec2 = coord.restore_window(at(12))
        self.assertEqual(rec2.version, 2)
        self.assertIs(rec1.status, RecommendationStatus.SUPERSEDED)
        self.assertIs(coord.plan.state, PlanState.ACTIVE)
        self.assertIs(coord.plan.step("B").state, StepState.IN_PROGRESS)

        # ---- 交接班：确认迟到 → 交接未闭合 ----
        self.clock.set(at(10, 20))
        coord.offer_handover("白班", "夜班", confirm_by=at(10, 30))
        with self.assertRaises(HandoverNotConfirmed):
            coord.release_handover(by="白班班长")  # 新班未确认前旧班不得交出责任
        self.clock.set(at(10, 35))
        with self.assertRaises(LateHandoverConfirmation):
            coord.confirm_handover_receipt(by="夜班班长")  # 迟到确认不生效
        self.assertIs(coord.handover.state, HandoverState.OFFERED)
        with self.assertRaises(HandoverNotClosed):
            coord.sign_off(by="值班员")  # 交接未闭合禁止完成销记

        # ---- 重新交接并闭合 ----
        self.clock.set(at(10, 40))
        coord.offer_handover("白班", "夜班", confirm_by=at(10, 50))
        self.clock.set(at(10, 45))
        coord.confirm_handover_receipt(by="夜班班长")
        self.clock.set(at(10, 46))
        coord.release_handover(by="白班班长")
        self.assertIs(coord.handover.state, HandoverState.CLOSED)

        # ---- 10:50 部分保护确认撤销 → 许可 v2 ----
        self.clock.set(at(10, 50))
        permit_v2 = coord.permits.revoke({"P2"})
        self.assertEqual(permit_v2.version, 2)
        self.assertFalse(coord.permits.is_work_eligible({"P2"}))  # B 开工资格失效
        # 过期（旧版本）确认不得恢复已失效的开工资格
        with self.assertRaises(StalePermitVersion):
            coord.permits.confirm(permit_version=1, protections={"P2"},
                                  by="防护员-甲", valid_until=at(12, 30))
        self.assertFalse(coord.permits.is_work_eligible({"P2"}))
        # 资格失效期间不能登记完成
        self.clock.set(at(10, 55))
        with self.assertRaises(WorkNotEligible):
            coord.complete_step("B")

        # ---- 重新确认当前版本；A 到达可中断点并撤离（现场执行，系统只记录）----
        coord.permits.confirm(permit_version=2, protections={"P1"},
                              by="防护员-乙", valid_until=at(12, 30))
        self.assertTrue(coord.permits.is_work_eligible({"P1"}))
        self.assertFalse(coord.permits.is_work_eligible({"P2"}))
        self.clock.set(at(11))
        coord.record_checkpoint_reached("A", "A-检查点1")
        self.clock.set(at(11, 10))
        coord.record_step_exited("A")

        # ---- 11:15 恢复 P2 → 许可 v3，重新确认后 B 复工并完成 ----
        self.clock.set(at(11, 15))
        permit_v3 = coord.permits.issue({"P1", "P2"}, valid_until=at(13))
        self.assertEqual(permit_v3.version, 3)
        coord.permits.confirm(permit_version=3, protections={"P1", "P2"},
                              by="防护员-乙", valid_until=at(12, 30))
        self.assertTrue(coord.permits.is_work_eligible({"P2"}))
        self.clock.set(at(11, 20))
        coord.complete_step("B")
        coord.record_step_exited("B")

        # ---- 11:30 负责人确认收尾建议；11:35 销记 ----
        self.clock.set(at(11, 30))
        coord.confirm_closing_recommendation(rec2.id, by="负责人-张")
        self.clock.set(at(11, 35))
        coord.sign_off(by="值班员-李")
        self.assertIs(coord.plan.state, PlanState.SIGNED_OFF)

        # ---- 许可版本严格递增；关键事件均有审计记录 ----
        self.assertEqual([p.version for p in coord.permits.permits], [1, 2, 3])
        self.assertEqual([r.version for r in coord.recommendations], [1, 2])
        kinds = [e.kind for e in coord.audit]
        for expected in (
            EventKind.EARLY_END_REQUESTED,
            EventKind.EXCEPTION_COORDINATION_ENTERED,
            EventKind.WINDOW_RESTORED,
            EventKind.EXCEPTION_COORDINATION_RESOLVED,
            EventKind.HANDOVER_CONFIRMATION_LATE,
            EventKind.HANDOVER_CLOSED,
            EventKind.PERMIT_REVOKED,
            EventKind.STALE_CONFIRMATION_REJECTED,
            EventKind.RECOMMENDATION_CONFIRMED,
            EventKind.SIGN_OFF_COMPLETED,
        ):
            self.assertIn(expected, kinds)


if __name__ == "__main__":
    unittest.main()
