"""封锁窗口执行的记录与检查核心。

系统职责边界：
- 只记录、检查、生成待负责人确认的收尾建议；
- 不自行下达设备操作指令（issue_equipment_command 一律拒绝）；
- 不强制改写任何步骤状态（步骤状态只能由现场报告后记录，
  且开工/完工记录必须持有有效许可）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .errors import (
    CoordinationStateError,
    DomainError,
    EquipmentCommandForbidden,
    ExpiredConfirmationError,
    HandoverNotClosedError,
    HandoverStateError,
    NotResponsibleError,
    PermitInvalidError,
    StaleConfirmationError,
    UnsafeExitError,
    WindowStateError,
)
from .models import (
    BlockadeWindow,
    ClosingSuggestion,
    Confirmation,
    CoordinationState,
    Event,
    ExitAssessment,
    ExitAssessmentItem,
    Permit,
    PermitStatus,
    Protection,
    ShiftHandover,
    SignOffRecord,
    Step,
    StepStatus,
    SuggestionAction,
    SuggestionItem,
    SuggestionStatus,
    WindowState,
)


class BlockadeOperation:
    def __init__(
        self,
        *,
        window: BlockadeWindow,
        steps: list[Step],
        protections: list[Protection],
        permit: Permit,
        shift: str,
        responsible: str,
    ):
        self.window = window
        self.steps: dict[str, Step] = {s.step_id: s for s in steps}
        self.protections: dict[str, Protection] = {p.protection_id: p for p in protections}
        self.permit = permit
        self.shift = shift
        self.responsible = responsible          # 当前负责人（交接闭合后才转移）
        self.handover: ShiftHandover | None = None
        self.coordination_state = CoordinationState.NORMAL
        self.suggestions: list[ClosingSuggestion] = []
        self.events: list[Event] = []
        self.sign_off_record: SignOffRecord | None = None
        self.early_exit_required = False        # 窗口被压缩后要求提前撤离
        self.last_problems: tuple[str, ...] = ()
        self._suggestion_seq = 0

    # ------------------------------------------------------------ 内部工具

    def _log(self, kind: str, at: datetime, **detail) -> None:
        self.events.append(Event(at, kind, detail))

    def _step(self, step_id: str) -> Step:
        try:
            return self.steps[step_id]
        except KeyError:
            raise DomainError(f"未知步骤 {step_id}") from None

    def _protection(self, protection_id: str) -> Protection:
        try:
            return self.protections[protection_id]
        except KeyError:
            raise DomainError(f"未知保护 {protection_id}") from None

    def _change_permit(self, status: PermitStatus, now: datetime, note: str) -> None:
        """许可每次变更版本严格递增。"""
        self.permit = Permit(
            self.permit.permit_id,
            self.permit.version + 1,
            status,
            now,
            self.permit.valid_until,
            note,
        )
        self._log("permit_changed", now, version=self.permit.version,
                  status=status.value, note=note)

    # ------------------------------------------------------------ 评估

    def exit_assessment(self) -> ExitAssessment:
        items = []
        total = timedelta(0)
        all_interruptible = True
        for step in self.steps.values():
            req = step.exit_requirement()
            items.append(ExitAssessmentItem(step.step_id, req.feasible, req.required, req.reason))
            total += req.required
            all_interruptible = all_interruptible and req.feasible
        return ExitAssessment(tuple(items), total, all_interruptible)

    def _reevaluate(self, now: datetime) -> None:
        """检查已配置退出条件；不满足则进入明确的异常协调状态。

        异常协调状态只由负责人显式闭合（resolve_exception），
        窗口恢复或许可变化都不会自动清除。
        """
        assessment = self.exit_assessment()
        remaining = self.window.current_end - now
        problems: list[str] = []
        if self.early_exit_required and not assessment.all_interruptible:
            bad = [i.step_id for i in assessment.items if not i.feasible]
            problems.append(f"步骤 {bad} 无可安全中断点，无法安全退出")
        if assessment.total_required > remaining:
            problems.append(
                f"剩余时间 {remaining} 不足以满足已配置退出条件 {assessment.total_required}"
            )
        if self.permit.status is not PermitStatus.VALID and any(
            s.status is StepStatus.IN_PROGRESS for s in self.steps.values()
        ):
            problems.append("开工许可已失效且仍有步骤在执行")
        self.last_problems = tuple(problems)
        if problems and self.coordination_state is CoordinationState.NORMAL:
            self.coordination_state = CoordinationState.EXCEPTION_COORDINATION
            self._log("coordination_state_changed", now,
                      to=self.coordination_state.value, reasons=problems)

    # ------------------------------------------------------------ 窗口

    def activate_window(self, now: datetime) -> None:
        if self.window.state is not WindowState.SCHEDULED:
            raise WindowStateError("窗口不在待开始状态")
        if self.permit.status is not PermitStatus.VALID:
            raise PermitInvalidError("许可无效，禁止开工")
        self.window.state = WindowState.ACTIVE
        self._log("window_activated", now)

    def request_early_end(self, new_end: datetime, now: datetime, by: str) -> ClosingSuggestion:
        """调度要求提前结束：压缩窗口并生成待确认的收尾建议。"""
        if self.window.state is not WindowState.ACTIVE:
            raise WindowStateError("仅执行中的窗口可提前结束")
        if not (now < new_end < self.window.current_end):
            raise WindowStateError("提前结束时间必须晚于当前时刻且早于当前结束时间")
        self.window.current_end = new_end
        self.window.state = WindowState.COMPRESSED
        self.window.revision += 1
        self.early_exit_required = True
        self._log("window_compressed", now, by=by,
                  new_end=new_end.isoformat(), revision=self.window.revision)
        suggestion = self.generate_closing_suggestion(now, note="窗口提前结束，生成收尾建议")
        self._reevaluate(now)
        return suggestion

    def restore_window(self, new_end: datetime, now: datetime, by: str) -> ClosingSuggestion:
        """压缩后恢复窗口。注意：恢复不会自动清除异常协调状态，
        也不会恢复已失效的开工许可。"""
        if self.window.state is not WindowState.COMPRESSED:
            raise WindowStateError("仅压缩状态的窗口可恢复")
        if not (self.window.current_end < new_end <= self.window.planned_end):
            raise WindowStateError("恢复后的结束时间必须晚于当前结束时间且不晚于计划结束时间")
        self.window.current_end = new_end
        self.window.state = WindowState.RESTORED
        self.window.revision += 1
        self.early_exit_required = False
        self._log("window_restored", now, by=by,
                  new_end=new_end.isoformat(), revision=self.window.revision)
        suggestion = self.generate_closing_suggestion(now, note="窗口恢复，重新评估收尾建议")
        self._reevaluate(now)
        return suggestion

    # ------------------------------------------------------------ 交接班

    def initiate_handover(
        self,
        *,
        new_shift: str,
        new_responsible: str,
        now: datetime,
        accept_due_at: datetime,
    ) -> ShiftHandover:
        if self.handover is not None and not self.handover.is_closed:
            raise HandoverStateError("存在未闭合交接，禁止再次发起")
        self.handover = ShiftHandover(
            old_shift=self.shift,
            new_shift=new_shift,
            old_responsible=self.responsible,
            new_responsible=new_responsible,
            offered_at=now,
            accept_due_at=accept_due_at,
        )
        self._log("handover_initiated", now, old_shift=self.shift,
                  new_shift=new_shift, accept_due_at=accept_due_at.isoformat())
        return self.handover

    def accept_handover(self, now: datetime, by_shift: str) -> None:
        """新班确认接收后责任才转移；迟到确认会被标记。"""
        h = self.handover
        if h is None or h.is_closed:
            raise HandoverStateError("无待接收的交接")
        if by_shift != h.new_shift:
            raise HandoverStateError("只有新班可以确认接收")
        h.accepted_at = now
        self.shift = h.new_shift
        self.responsible = h.new_responsible
        self._log("handover_accepted", now, late=h.is_late, responsible=self.responsible)

    # ------------------------------------------------------------ 保护与许可

    def revoke_protection(self, protection_id: str, now: datetime, by: str) -> None:
        """部分保护确认被撤销：许可失效（版本递增），重新评估。"""
        p = self._protection(protection_id)
        if not p.active:
            raise DomainError(f"保护 {protection_id} 已处于撤销状态")
        p.active = False
        self._log("protection_revoked", now, by=by, protection=protection_id)
        self._change_permit(PermitStatus.INVALIDATED, now,
                            note=f"保护 {p.name} 被撤销，开工资格失效")
        self.generate_closing_suggestion(now, note="保护确认被部分撤销，重新评估收尾建议")
        self._reevaluate(now)

    def apply_confirmation(self, c: Confirmation, now: datetime) -> None:
        """登记确认。过期确认、版本不符的确认一律拒绝；
        任何确认都不得恢复已失效的开工资格。"""
        if c.permit_version != self.permit.version:
            self._log("confirmation_rejected", now, confirmation_id=c.confirmation_id,
                      reason="stale_version", permit_version=c.permit_version,
                      current_version=self.permit.version)
            raise StaleConfirmationError(
                f"确认对应许可版本 {c.permit_version}，当前版本 {self.permit.version}，过期确认无效"
            )
        if now > c.expires_at:
            self._log("confirmation_rejected", now, confirmation_id=c.confirmation_id,
                      reason="expired", expires_at=c.expires_at.isoformat())
            raise ExpiredConfirmationError("确认已超过有效期，不能作为有效依据")
        if self.permit.status is not PermitStatus.VALID:
            self._log("confirmation_rejected", now, confirmation_id=c.confirmation_id,
                      reason="permit_invalidated")
            raise PermitInvalidError("许可已失效，确认不得恢复已失效的开工资格")
        self._log("confirmation_accepted", now,
                  confirmation_id=c.confirmation_id, by=c.confirmed_by)

    def reinstate_permit(self, now: datetime, by: str, note: str) -> None:
        """重新签发许可：人工流程，系统只记录，版本递增。"""
        self.permit = Permit(self.permit.permit_id, self.permit.version + 1,
                             PermitStatus.VALID, now, self.window.current_end, note)
        self._log("permit_reinstated", now, by=by,
                  version=self.permit.version, note=note)
        self._reevaluate(now)

    # ------------------------------------------------------------ 步骤状态（只记录现场报告）

    def record_step_status(self, step_id: str, status: StepStatus, now: datetime, by: str) -> None:
        """记录现场报告的步骤状态。系统不会主动把任何步骤改成完成；
        开工/完工记录必须持有有效许可，安全类状态（停点/撤离）随时允许。"""
        step = self._step(step_id)
        if status in (StepStatus.IN_PROGRESS, StepStatus.COMPLETED) \
                and self.permit.status is not PermitStatus.VALID:
            self._log("step_status_rejected", now, step_id=step_id,
                      to=status.value, reason="permit_invalidated")
            raise PermitInvalidError("开工资格已失效，禁止记录开工/完工；系统不会强制改写成完成")
        step.status = status
        self._log("step_status_recorded", now, step_id=step_id, to=status.value, by=by)
        self._reevaluate(now)

    # ------------------------------------------------------------ 收尾建议

    def _suggest_item(self, step: Step) -> SuggestionItem:
        if step.status is StepStatus.COMPLETED:
            return SuggestionItem(step.step_id, SuggestionAction.NO_ACTION, "已完成，按实际记录")
        if step.status is StepStatus.EVACUATED:
            return SuggestionItem(step.step_id, SuggestionAction.NO_ACTION, "已安全撤离")
        if step.status is StepStatus.PENDING:
            return SuggestionItem(step.step_id, SuggestionAction.DO_NOT_START,
                                  "未开始；窗口收尾或许可失效期间禁止开工")
        if step.status is StepStatus.PAUSED_AT_INTERRUPTIBLE_POINT:
            return SuggestionItem(step.step_id, SuggestionAction.EVACUATE,
                                  f"已停于可中断点，按配置 {step.evacuation_time} 撤离")
        if not step.interruptible_points:
            return SuggestionItem(step.step_id, SuggestionAction.CANNOT_SAFELY_EXIT,
                                  "无可安全中断点，需现场协调，禁止强制完工")
        nearest = min(step.interruptible_points, key=lambda p: p.time_to_reach)
        return SuggestionItem(
            step.step_id, SuggestionAction.PARK_AT_INTERRUPTIBLE_POINT,
            f"建议退至可中断点「{nearest.name}」（约 {nearest.time_to_reach}），"
            f"再按配置 {step.evacuation_time} 撤离",
        )

    def generate_closing_suggestion(self, now: datetime, note: str = "") -> ClosingSuggestion:
        """生成待负责人确认的收尾建议；旧的待确认建议被取代。"""
        for s in self.suggestions:
            if s.status is SuggestionStatus.PENDING_CONFIRMATION:
                s.status = SuggestionStatus.SUPERSEDED
        self._suggestion_seq += 1
        items = tuple(self._suggest_item(step) for step in self.steps.values())
        suggestion = ClosingSuggestion(f"SG-{self._suggestion_seq}", now, items, note)
        self.suggestions.append(suggestion)
        self._log("closing_suggestion_generated", now,
                  suggestion_id=suggestion.suggestion_id, note=note)
        return suggestion

    def confirm_suggestion(self, suggestion_id: str, now: datetime, by: str) -> None:
        """收尾建议须由当前负责人确认。"""
        if by != self.responsible:
            raise NotResponsibleError(f"收尾建议须由当前负责人 {self.responsible} 确认")
        suggestion = next((s for s in self.suggestions if s.suggestion_id == suggestion_id), None)
        if suggestion is None:
            raise DomainError(f"未知建议 {suggestion_id}")
        if suggestion.status is not SuggestionStatus.PENDING_CONFIRMATION:
            raise DomainError("建议已被取代或已确认")
        suggestion.status = SuggestionStatus.CONFIRMED
        suggestion.confirmed_by = by
        suggestion.confirmed_at = now
        self._log("closing_suggestion_confirmed", now, suggestion_id=suggestion_id, by=by)

    # ------------------------------------------------------------ 异常协调与销记

    def resolve_exception(self, now: datetime, by: str, note: str) -> None:
        """负责人显式闭合异常协调：许可必须有效且退出条件已满足。"""
        if by != self.responsible:
            raise NotResponsibleError(f"异常协调须由当前负责人 {self.responsible} 闭合")
        if self.coordination_state is not CoordinationState.EXCEPTION_COORDINATION:
            raise CoordinationStateError("当前不在异常协调状态")
        if self.permit.status is not PermitStatus.VALID:
            raise PermitInvalidError("许可未恢复，异常协调不能闭合")
        self._reevaluate(now)
        if self.last_problems:
            raise UnsafeExitError(f"退出条件仍未满足：{list(self.last_problems)}")
        self.coordination_state = CoordinationState.NORMAL
        self._log("coordination_state_changed", now,
                  to=self.coordination_state.value, note=note)

    def sign_off_completion(self, now: datetime, by: str) -> SignOffRecord:
        """完成销记。交接未闭合、异常协调未闭合、建议待确认时一律禁止；
        销记按步骤实际状态记录，系统不强制改写任何步骤。"""
        if by != self.responsible:
            raise NotResponsibleError(f"销记须由当前负责人 {self.responsible} 进行")
        if self.handover is not None and not self.handover.is_closed:
            self._log("sign_off_rejected", now, reason="handover_open")
            raise HandoverNotClosedError("交接未闭合，禁止完成销记")
        if self.coordination_state is not CoordinationState.NORMAL:
            self._log("sign_off_rejected", now, reason="exception_coordination")
            raise CoordinationStateError("异常协调未闭合，禁止完成销记")
        pending = [s for s in self.suggestions if s.status is SuggestionStatus.PENDING_CONFIRMATION]
        if pending:
            raise DomainError(f"收尾建议 {pending[0].suggestion_id} 待负责人确认，禁止销记")
        record = SignOffRecord(
            signed_at=now,
            signed_by=by,
            step_statuses={sid: s.status for sid, s in self.steps.items()},
            permit_version=self.permit.version,
            handover_late=self.handover.is_late if self.handover else None,
            window_state=self.window.state,
            window_revision=self.window.revision,
            note="按实际状态销记；系统未强制改写任何步骤",
        )
        self.sign_off_record = record
        self.window.state = WindowState.CLOSED
        self._log("completion_signed_off", now, by=by,
                  permit_version=self.permit.version,
                  handover_late=record.handover_late)
        return record

    # ------------------------------------------------------------ 系统边界

    def issue_equipment_command(self, *args, **kwargs):
        """系统只辅助记录和检查，不自行下达设备操作指令。"""
        raise EquipmentCommandForbidden("系统只辅助记录和检查，不自行下达设备操作指令")
