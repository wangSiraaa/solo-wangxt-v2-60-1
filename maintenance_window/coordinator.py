"""调度协调器：封锁窗口提前结束/恢复、交接班、收尾建议与销记的核心规则。

设计原则：系统只辅助记录和检查，不自行下达设备操作指令。
- 窗口压缩时按各步骤配置的可中断点与撤离时间评估能否安全退出，生成
  “待负责人确认”的收尾建议；建议的执行主体始终是现场人员。
- 缩短后的时间不足以满足已配置退出条件时，计划进入明确的异常协调状态，
  且任何步骤都不能被强制改为完成。
- 旧班交出责任前新班必须确认接收；交接未闭合时禁止完成销记。
"""

from __future__ import annotations

from datetime import datetime

from .clock import Clock
from .errors import (
    DomainError,
    HandoverError,
    HandoverNotClosed,
    InvalidStepTransition,
    LateHandoverConfirmation,
    RecommendationNotActionable,
    RecommendationNotConfirmed,
    StepsNotClosed,
    UnresolvedExceptionCoordination,
    WindowError,
    WorkNotEligible,
)
from .handover import HandoverState, ShiftHandover
from .models import (
    AuditEvent,
    ClosingAdvice,
    ClosingItem,
    ClosingRecommendation,
    EventKind,
    MaintenancePlan,
    PlanState,
    RecommendationStatus,
    Step,
    StepState,
    Window,
)
from .permits import PermitRegistry


class MaintenanceCoordinator:
    TERMINAL_STEP_STATES = (StepState.COMPLETED, StepState.EXITED)

    def __init__(self, plan: MaintenancePlan, window: Window, clock: Clock):
        self.plan = plan
        self.window = window
        self.clock = clock
        self.audit: list[AuditEvent] = []
        self.permits = PermitRegistry(clock, emit=self.audit.append)
        self.handover: ShiftHandover | None = None
        self.recommendations: list[ClosingRecommendation] = []

    # ---------------------------------------------------------------- 窗口调整

    def request_early_end(self, new_end: datetime) -> ClosingRecommendation:
        """请求提前结束窗口：逐步骤评估退出条件，生成待确认的收尾建议。"""
        self._require_not_signed_off()
        at = self.clock.now()
        if new_end >= self.window.end:
            raise WindowError("提前结束的新结束时间必须早于当前窗口结束时间")
        if new_end <= at:
            raise WindowError("新结束时间必须晚于当前时刻")
        self.window.end = new_end
        self._audit(EventKind.EARLY_END_REQUESTED, f"窗口提前至 {new_end}")
        return self._evaluate(at, resumed=False)

    def restore_window(self, new_end: datetime) -> ClosingRecommendation:
        """窗口恢复（压缩后回延）：重新评估，可恢复的步骤解除阻断。"""
        self._require_not_signed_off()
        at = self.clock.now()
        if new_end <= self.window.end:
            raise WindowError("恢复后的结束时间必须晚于当前生效的结束时间")
        self.window.end = new_end
        self._audit(EventKind.WINDOW_RESTORED, f"窗口恢复至 {new_end}")
        return self._evaluate(at, resumed=True)

    def _evaluate(self, at: datetime, resumed: bool) -> ClosingRecommendation:
        remaining = self.window.end - at
        items: list[ClosingItem] = []
        blocked: list[Step] = []
        for step in self.plan.steps:
            if step.state in self.TERMINAL_STEP_STATES:
                items.append(ClosingItem(step.id, ClosingAdvice.NO_ACTION,
                                         f"步骤已处于 {step.state.name}，无需动作"))
                continue
            if step.state is StepState.PENDING:
                items.append(ClosingItem(step.id, ClosingAdvice.NO_ACTION,
                                         "尚未开工，保持不进入"))
                continue
            need = step.exit_requirement()
            fits = need is not None and need <= remaining
            if fits and resumed:
                if step.state is StepState.CANNOT_SAFELY_EXIT:
                    step.state = StepState.IN_PROGRESS
                    self._audit(EventKind.STEP_STATE_RECORDED,
                                f"{step.id} 退出条件重新满足，恢复为作业中")
                items.append(ClosingItem(step.id, ClosingAdvice.CONTINUE_WORK,
                                         "窗口已恢复，剩余时间满足已配置退出条件，可继续作业"))
            elif fits:
                checkpoint = (step.at_checkpoint if step.state is StepState.AT_CHECKPOINT
                              else (step.nearest_checkpoint().name if step.nearest_checkpoint() else None))
                items.append(ClosingItem(
                    step.id, ClosingAdvice.EXIT_AT_CHECKPOINT,
                    checkpoint=checkpoint, evacuate_by=at + need,
                    rationale=f"到达可中断点并撤离共需 {need}，剩余 {remaining}，可满足"))
            else:
                step.state = StepState.CANNOT_SAFELY_EXIT
                blocked.append(step)
                reason = ("未配置可中断点，作业中无法安全中断" if need is None
                          else f"安全退出至少需 {need}，剩余仅 {remaining}")
                items.append(ClosingItem(step.id, ClosingAdvice.HOLD_FOR_COORDINATION,
                                         rationale=f"无法安全退出：{reason}，保持现场等待协调"))
        if blocked:
            self.plan.state = PlanState.EXCEPTION_COORDINATION
            self._audit(EventKind.EXCEPTION_COORDINATION_ENTERED,
                        f"进入异常协调状态，无法安全退出的步骤: {[s.id for s in blocked]}")
        elif self.plan.state is PlanState.EXCEPTION_COORDINATION:
            self.plan.state = PlanState.ACTIVE
            self._audit(EventKind.EXCEPTION_COORDINATION_RESOLVED,
                        "所有步骤的退出条件均已满足，异常协调解除")
        return self._new_recommendation(at, items)

    def _new_recommendation(self, at: datetime, items: list[ClosingItem]) -> ClosingRecommendation:
        for old in self.recommendations:
            if old.status is RecommendationStatus.PENDING_CONFIRMATION:
                old.status = RecommendationStatus.SUPERSEDED
        rec = ClosingRecommendation(
            id=f"CR-{len(self.recommendations) + 1}",
            version=len(self.recommendations) + 1,
            created_at=at,
            window_end=self.window.end,
            items=tuple(items),
        )
        self.recommendations.append(rec)
        self._audit(EventKind.RECOMMENDATION_GENERATED,
                    f"收尾建议 {rec.id} (v{rec.version}) 已生成，待负责人确认")
        return rec

    def confirm_closing_recommendation(self, recommendation_id: str, by: str) -> ClosingRecommendation:
        """负责人确认收尾建议；已被取代/已确认的建议不得再确认。"""
        at = self.clock.now()
        rec = self._recommendation(recommendation_id)
        if rec.status is not RecommendationStatus.PENDING_CONFIRMATION:
            raise RecommendationNotActionable(
                f"建议 {rec.id} 状态为 {rec.status.name}，不能确认")
        rec.status = RecommendationStatus.CONFIRMED
        rec.confirmed_by = by
        rec.confirmed_at = at
        self._audit(EventKind.RECOMMENDATION_CONFIRMED, f"{rec.id} 经负责人 {by} 确认")
        return rec

    # ---------------------------------------------------------------- 步骤状态登记

    def start_step(self, step_id: str) -> None:
        step = self.plan.step(step_id)
        if step.state is not StepState.PENDING:
            raise InvalidStepTransition(f"步骤 {step_id} 当前状态 {step.state.name}，不能开工")
        self._require_eligible(step)
        step.state = StepState.IN_PROGRESS
        self._audit(EventKind.STEP_STATE_RECORDED, f"{step_id} 开工")

    def record_checkpoint_reached(self, step_id: str, point_name: str) -> None:
        step = self.plan.step(step_id)
        if step.state is not StepState.IN_PROGRESS:
            raise InvalidStepTransition(f"步骤 {step_id} 当前状态 {step.state.name}，不能登记到达可中断点")
        if point_name not in {p.name for p in step.exit_config.interruptible_points}:
            raise InvalidStepTransition(f"{point_name} 不是步骤 {step_id} 已配置的可中断点")
        step.state = StepState.AT_CHECKPOINT
        step.at_checkpoint = point_name
        self._audit(EventKind.STEP_STATE_RECORDED, f"{step_id} 到达可中断点 {point_name}")

    def complete_step(self, step_id: str) -> None:
        """登记步骤完成。无法安全退出的步骤禁止被强制改为完成。"""
        step = self.plan.step(step_id)
        if step.state is StepState.CANNOT_SAFELY_EXIT:
            raise InvalidStepTransition(
                f"步骤 {step_id} 无法安全退出，计划处于异常协调状态，不能强制改为完成")
        if step.state not in (StepState.IN_PROGRESS, StepState.AT_CHECKPOINT):
            raise InvalidStepTransition(f"步骤 {step_id} 当前状态 {step.state.name}，不能登记完成")
        self._require_eligible(step)
        step.state = StepState.COMPLETED
        self._audit(EventKind.STEP_STATE_RECORDED, f"{step_id} 作业完成")

    def record_step_exited(self, step_id: str) -> None:
        """登记安全撤离。撤离是安全动作，不要求开工资格，但异常协调未解除时不得登记。"""
        step = self.plan.step(step_id)
        if step.state is StepState.CANNOT_SAFELY_EXIT:
            raise InvalidStepTransition(
                f"步骤 {step_id} 处于异常协调中，协调结论形成前不能登记撤离")
        if step.state not in (StepState.IN_PROGRESS, StepState.AT_CHECKPOINT, StepState.COMPLETED):
            raise InvalidStepTransition(f"步骤 {step_id} 当前状态 {step.state.name}，不能登记撤离")
        step.state = StepState.EXITED
        self._audit(EventKind.STEP_STATE_RECORDED, f"{step_id} 已安全撤离")

    # ---------------------------------------------------------------- 交接班

    def offer_handover(self, from_shift: str, to_shift: str, confirm_by: datetime) -> None:
        if self.handover is not None and self.handover.state is HandoverState.CLOSED:
            raise HandoverError("交接已闭合，不能重复发起")
        self.handover = ShiftHandover(from_shift, to_shift, confirm_by, offered_at=self.clock.now())
        self._audit(EventKind.HANDOVER_OFFERED,
                    f"{from_shift} -> {to_shift} 发起交接，确认截止 {confirm_by}")

    def confirm_handover_receipt(self, by: str) -> None:
        handover = self._require_handover()
        at = self.clock.now()
        try:
            handover.confirm_receipt(by, at)
        except LateHandoverConfirmation:
            self._audit(EventKind.HANDOVER_CONFIRMATION_LATE,
                        f"{by} 的接收确认迟到，不生效，交接保持未闭合")
            raise
        self._audit(EventKind.HANDOVER_RECEIPT_CONFIRMED, f"{by} 确认接收")

    def release_handover(self, by: str) -> None:
        handover = self._require_handover()
        handover.release(by, self.clock.now())
        self._audit(EventKind.HANDOVER_CLOSED, f"{by} 交出责任，交接闭合")

    # ---------------------------------------------------------------- 销记

    def sign_off(self, by: str) -> None:
        """完成销记。交接未闭合、异常协调未解除、步骤未闭合或建议未确认时一律禁止。"""
        at = self.clock.now()
        if self.handover is None or self.handover.state is not HandoverState.CLOSED:
            raise HandoverNotClosed("交接未闭合，禁止完成销记")
        if self.plan.state is PlanState.EXCEPTION_COORDINATION:
            raise UnresolvedExceptionCoordination("异常协调状态未解除，禁止完成销记")
        open_steps = [s.id for s in self.plan.steps if s.state not in self.TERMINAL_STEP_STATES]
        if open_steps:
            raise StepsNotClosed(f"存在未闭合步骤: {open_steps}")
        latest = self.recommendations[-1] if self.recommendations else None
        if latest is None or latest.status is not RecommendationStatus.CONFIRMED:
            raise RecommendationNotConfirmed("最新收尾建议未经负责人确认，禁止完成销记")
        self.plan.state = PlanState.SIGNED_OFF
        self._audit(EventKind.SIGN_OFF_COMPLETED, f"{by} 完成销记")

    # ---------------------------------------------------------------- 内部

    def _require_eligible(self, step: Step) -> None:
        if not self.permits.is_work_eligible(step.required_protections):
            raise WorkNotEligible(
                f"步骤 {step.id} 所需保护 {sorted(step.required_protections)} 的开工资格已失效")

    def _recommendation(self, recommendation_id: str) -> ClosingRecommendation:
        for rec in self.recommendations:
            if rec.id == recommendation_id:
                return rec
        raise KeyError(f"未知收尾建议: {recommendation_id}")

    def _require_handover(self) -> ShiftHandover:
        if self.handover is None:
            raise HandoverError("尚未发起交接")
        return self.handover

    def _require_not_signed_off(self) -> None:
        if self.plan.state is PlanState.SIGNED_OFF:
            raise DomainError("计划已销记，禁止再调整窗口")

    def _audit(self, kind: EventKind, detail: str) -> None:
        self.audit.append(AuditEvent(self.clock.now(), kind, detail))
