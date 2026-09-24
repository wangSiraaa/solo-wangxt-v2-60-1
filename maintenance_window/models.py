"""领域模型：封锁窗口、检修计划、步骤（含可中断点与撤离时间配置）、收尾建议、审计事件。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto


class StepState(Enum):
    PENDING = auto()             # 未开工
    IN_PROGRESS = auto()         # 作业中
    AT_CHECKPOINT = auto()       # 已到达某个已配置的可中断点
    COMPLETED = auto()           # 作业完成
    EXITED = auto()              # 已安全撤离（可能未完工，遗留事项另行记录）
    CANNOT_SAFELY_EXIT = auto()  # 经评估：剩余时间不足以满足退出条件


class PlanState(Enum):
    ACTIVE = auto()                  # 正常执行
    EXCEPTION_COORDINATION = auto()  # 异常协调状态：存在无法安全退出的步骤
    SIGNED_OFF = auto()              # 已销记


@dataclass(frozen=True)
class InterruptiblePoint:
    """可中断点：从当前作业位置到达该点所需的时间（计划配置项）。"""

    name: str
    reach_within: timedelta


@dataclass(frozen=True)
class StepExitConfig:
    """步骤退出配置：可中断点列表 + 撤离所需时间（计划配置项）。"""

    interruptible_points: tuple[InterruptiblePoint, ...]
    evacuation_time: timedelta


@dataclass
class Step:
    id: str
    name: str
    required_protections: frozenset[str]  # 该步骤开工所需的保护措施
    exit_config: StepExitConfig
    state: StepState = StepState.PENDING
    at_checkpoint: str | None = None

    def exit_requirement(self) -> timedelta | None:
        """从此刻起到安全撤离所需的最短时间；无法中断时返回 None。"""
        if self.state == StepState.AT_CHECKPOINT:
            return self.exit_config.evacuation_time
        if self.state == StepState.PENDING:
            return timedelta(0)  # 未进场，无需撤离
        if not self.exit_config.interruptible_points:
            return None  # 未配置可中断点：作业中无法安全中断
        nearest = min(p.reach_within for p in self.exit_config.interruptible_points)
        return nearest + self.exit_config.evacuation_time

    def nearest_checkpoint(self) -> InterruptiblePoint | None:
        if not self.exit_config.interruptible_points:
            return None
        return min(self.exit_config.interruptible_points, key=lambda p: p.reach_within)


class ClosingAdvice(Enum):
    EXIT_AT_CHECKPOINT = auto()     # 建议在指定可中断点中断并撤离
    CONTINUE_WORK = auto()          # 窗口恢复，可继续作业
    HOLD_FOR_COORDINATION = auto()  # 无法安全退出，保持现场并等待协调
    NO_ACTION = auto()              # 无需动作（已完成/未开工）


class ExecutionResponsibility(Enum):
    """系统只辅助记录和检查，不自行下达设备操作指令：

    所有收尾建议均为待确认的记录，执行主体始终是现场人员。
    """

    ONSITE_PERSONNEL = auto()


@dataclass(frozen=True)
class ClosingItem:
    step_id: str
    advice: ClosingAdvice
    rationale: str
    checkpoint: str | None = None
    evacuate_by: datetime | None = None
    execution: ExecutionResponsibility = ExecutionResponsibility.ONSITE_PERSONNEL


class RecommendationStatus(Enum):
    PENDING_CONFIRMATION = auto()  # 待负责人确认
    CONFIRMED = auto()             # 负责人已确认
    SUPERSEDED = auto()            # 已被更新版本取代（过期建议不得再确认）


@dataclass
class ClosingRecommendation:
    """收尾建议：系统生成的记录，须经负责人确认，系统不据此操作设备。"""

    id: str
    version: int
    created_at: datetime
    window_end: datetime
    items: tuple[ClosingItem, ...]
    status: RecommendationStatus = RecommendationStatus.PENDING_CONFIRMATION
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


@dataclass
class MaintenancePlan:
    id: str
    steps: list[Step]
    state: PlanState = PlanState.ACTIVE

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(f"未知步骤: {step_id}")


@dataclass
class Window:
    """封锁窗口：end 为当前生效结束时间（压缩/恢复会改变它），planned_end 为原计划。"""

    start: datetime
    planned_end: datetime
    end: datetime


class EventKind(Enum):
    EARLY_END_REQUESTED = auto()          # 窗口提前结束请求
    WINDOW_RESTORED = auto()              # 窗口恢复
    RECOMMENDATION_GENERATED = auto()     # 收尾建议已生成
    RECOMMENDATION_CONFIRMED = auto()     # 收尾建议已确认
    EXCEPTION_COORDINATION_ENTERED = auto()   # 进入异常协调状态
    EXCEPTION_COORDINATION_RESOLVED = auto()  # 异常协调状态解除
    HANDOVER_OFFERED = auto()             # 旧班发起交接
    HANDOVER_RECEIPT_CONFIRMED = auto()   # 新班确认接收
    HANDOVER_CONFIRMATION_LATE = auto()   # 交班确认迟到（不生效）
    HANDOVER_CLOSED = auto()              # 旧班交出责任，交接闭合
    PERMIT_ISSUED = auto()                # 许可签发（新版本）
    PERMIT_REVOKED = auto()               # 许可部分撤销（新版本）
    PERMIT_CONFIRMED = auto()             # 保护确认
    STALE_CONFIRMATION_REJECTED = auto()  # 过期版本确认被拒绝
    STEP_STATE_RECORDED = auto()          # 步骤状态登记
    SIGN_OFF_COMPLETED = auto()           # 销记完成


@dataclass(frozen=True)
class AuditEvent:
    at: datetime
    kind: EventKind
    detail: str
