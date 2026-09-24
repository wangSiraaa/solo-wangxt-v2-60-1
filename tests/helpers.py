"""测试共享构造：三步骤计划（一可中断、一不可安全退出）。"""

from datetime import datetime, timedelta

from blockade import (
    BlockadeOperation,
    BlockadeWindow,
    InterruptiblePoint,
    Permit,
    PermitStatus,
    Protection,
    Step,
)

T0 = datetime(2026, 9, 24, 8, 0, 0)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def build_operation() -> BlockadeOperation:
    """窗口 08:00-12:00；S2 可中断（20min 到点 + 15min 撤离），
    S3 焊接无可中断点（无法安全退出），S1 常规步骤。"""
    window = BlockadeWindow("W-1", T0, T0 + 4 * H, T0 + 4 * H)
    steps = [
        Step("S1", "拆除旧部件",
             interruptible_points=(InterruptiblePoint("停工点", 5 * M),),
             evacuation_time=10 * M,
             required_protections=frozenset({"P1"})),
        Step("S2", "更换密封件",
             interruptible_points=(InterruptiblePoint("临时封堵", 20 * M),),
             evacuation_time=15 * M,
             required_protections=frozenset({"P1"})),
        Step("S3", "焊接加固",
             interruptible_points=(),
             evacuation_time=10 * M,
             required_protections=frozenset({"P2"})),
    ]
    protections = [Protection("P1", "机械隔离"), Protection("P2", "接地保护")]
    permit = Permit("PERMIT-1", version=1, status=PermitStatus.VALID,
                    valid_from=T0, valid_until=T0 + 4 * H, note="初始签发")
    return BlockadeOperation(window=window, steps=steps, protections=protections,
                             permit=permit, shift="早班", responsible="负责人甲")


def start_all_steps(op: BlockadeOperation) -> None:
    from blockade import StepStatus
    op.activate_window(T0)
    op.record_step_status("S1", StepStatus.IN_PROGRESS, T0 + 5 * M, by="负责人甲")
    op.record_step_status("S2", StepStatus.IN_PROGRESS, T0 + 10 * M, by="负责人甲")
    op.record_step_status("S3", StepStatus.IN_PROGRESS, T0 + 15 * M, by="负责人甲")
    op.record_step_status("S1", StepStatus.COMPLETED, T0 + 60 * M, by="负责人甲")
