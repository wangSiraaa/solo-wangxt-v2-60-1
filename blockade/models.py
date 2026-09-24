"""领域模型：封锁窗口、检修步骤、许可、交接班、收尾建议。

关键配置：
- 每个步骤配置「可中断点」（到达该点所需时间）和「撤离所需时间」；
- 许可的每次变更产生递增版本；
- 收尾建议只由系统生成，必须经负责人确认，系统不据此操作设备。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta


# ---------------------------------------------------------------- 步骤

class StepStatus(enum.Enum):
    PENDING = "pending"                                # 未开始
    IN_PROGRESS = "in_progress"                        # 执行中
    PAUSED_AT_INTERRUPTIBLE_POINT = "paused_at_point"  # 已停在可中断点
    COMPLETED = "completed"                            # 已完成
    EVACUATED = "evacuated"                            # 已安全撤离


@dataclass(frozen=True)
class InterruptiblePoint:
    """可中断点配置：从当前状态安全停到该点所需时间。"""

    name: str
    time_to_reach: timedelta


@dataclass(frozen=True)
class ExitRequirement:
    """单个步骤的退出条件评估结果。"""

    feasible: bool               # 是否能安全中断/退出
    required: timedelta          # 安全退出所需时间（不可行时为 0）
    reason: str = ""


@dataclass
class Step:
    """检修步骤。可中断点与撤离时间作为配置随计划下发。"""

    step_id: str
    name: str
    interruptible_points: tuple[InterruptiblePoint, ...] = ()
    evacuation_time: timedelta = timedelta(0)
    required_protections: frozenset[str] = frozenset()
    status: StepStatus = StepStatus.PENDING

    def exit_requirement(self) -> ExitRequirement:
        """按配置计算该步骤当前的安全退出条件。"""
        if self.status in (StepStatus.PENDING, StepStatus.COMPLETED, StepStatus.EVACUATED):
            return ExitRequirement(True, timedelta(0))
        if self.status is StepStatus.PAUSED_AT_INTERRUPTIBLE_POINT:
            return ExitRequirement(True, self.evacuation_time)
        # 执行中：必须先到达某个可中断点，再撤离
        if not self.interruptible_points:
            return ExitRequirement(False, timedelta(0), "无可安全中断点，无法安全退出")
        nearest = min(p.time_to_reach for p in self.interruptible_points)
        return ExitRequirement(True, nearest + self.evacuation_time)


# ---------------------------------------------------------------- 窗口

class WindowState(enum.Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPRESSED = "compressed"    # 已提前结束（压缩）
    RESTORED = "restored"        # 压缩后又恢复
    CLOSED = "closed"


@dataclass
class BlockadeWindow:
    window_id: str
    planned_start: datetime
    planned_end: datetime
    current_end: datetime
    state: WindowState = WindowState.SCHEDULED
    revision: int = 0            # 窗口每次压缩/恢复递增


# ---------------------------------------------------------------- 许可与确认

class PermitStatus(enum.Enum):
    VALID = "valid"
    INVALIDATED = "invalidated"


@dataclass
class Permit:
    """开工许可。每次变更（失效/重新签发）版本严格递增。"""

    permit_id: str
    version: int
    status: PermitStatus
    valid_from: datetime
    valid_until: datetime
    note: str = ""


@dataclass(frozen=True)
class Confirmation:
    """针对某一许可版本的确认。过期或版本不符的确认一律无效，
    且任何确认都不能恢复已失效的开工资格。"""

    confirmation_id: str
    permit_version: int
    confirmed_by: str
    confirmed_at: datetime
    expires_at: datetime


@dataclass
class Protection:
    protection_id: str
    name: str
    active: bool = True


# ---------------------------------------------------------------- 交接班

class HandoverState(enum.Enum):
    PENDING_ACCEPTANCE = "pending_acceptance"   # 旧班已交出，待新班确认接收
    ACCEPTED = "accepted"                       # 新班已确认，交接闭合


@dataclass
class ShiftHandover:
    """旧班交出责任前，新班必须确认接收；确认前责任仍属旧班。"""

    old_shift: str
    new_shift: str
    old_responsible: str
    new_responsible: str
    offered_at: datetime
    accept_due_at: datetime
    accepted_at: datetime | None = None

    @property
    def is_closed(self) -> bool:
        return self.accepted_at is not None

    @property
    def is_late(self) -> bool:
        return self.is_closed and self.accepted_at > self.accept_due_at

    @property
    def state(self) -> HandoverState:
        return HandoverState.ACCEPTED if self.is_closed else HandoverState.PENDING_ACCEPTANCE


# ---------------------------------------------------------------- 收尾建议

class SuggestionAction(enum.Enum):
    NO_ACTION = "no_action"
    DO_NOT_START = "do_not_start"
    PARK_AT_INTERRUPTIBLE_POINT = "park_at_interruptible_point"
    EVACUATE = "evacuate"
    CANNOT_SAFELY_EXIT = "cannot_safely_exit"   # 需现场协调，禁止强制完工


@dataclass(frozen=True)
class SuggestionItem:
    step_id: str
    action: SuggestionAction
    detail: str


class SuggestionStatus(enum.Enum):
    PENDING_CONFIRMATION = "pending_confirmation"   # 待负责人确认
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"                       # 已被新评估取代


@dataclass
class ClosingSuggestion:
    """系统生成的收尾建议：只辅助记录和检查，必须经负责人确认。"""

    suggestion_id: str
    created_at: datetime
    items: tuple[SuggestionItem, ...]
    note: str = ""
    status: SuggestionStatus = SuggestionStatus.PENDING_CONFIRMATION
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    requires_human_confirmation: bool = True


# ---------------------------------------------------------------- 协调状态与记录

class CoordinationState(enum.Enum):
    NORMAL = "normal"
    EXCEPTION_COORDINATION = "exception_coordination"   # 明确的异常协调状态


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str
    detail: dict


@dataclass(frozen=True)
class ExitAssessmentItem:
    step_id: str
    feasible: bool
    required: timedelta
    reason: str


@dataclass(frozen=True)
class ExitAssessment:
    items: tuple[ExitAssessmentItem, ...]
    total_required: timedelta
    all_interruptible: bool


@dataclass(frozen=True)
class SignOffRecord:
    """完成销记：按步骤实际状态记录，系统不强制改写任何步骤。"""

    signed_at: datetime
    signed_by: str
    step_statuses: dict
    permit_version: int
    handover_late: bool | None
    window_state: WindowState
    window_revision: int
    note: str
