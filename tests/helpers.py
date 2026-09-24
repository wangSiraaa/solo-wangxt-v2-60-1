"""测试共享构造：三步检修计划（A 可快速中断、B 中断耗时长、C 无需保护）。"""

from datetime import datetime, timedelta

from maintenance_window import (
    InterruptiblePoint,
    MaintenanceCoordinator,
    MaintenancePlan,
    Step,
    StepExitConfig,
    Window,
)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 24, hour, minute)


def make_coordinator(clock) -> MaintenanceCoordinator:
    plan = MaintenancePlan(
        id="PLAN-001",
        steps=[
            Step(
                id="A", name="道岔检修",
                required_protections=frozenset({"P1"}),
                exit_config=StepExitConfig(
                    interruptible_points=(InterruptiblePoint("A-检查点1", timedelta(minutes=10)),),
                    evacuation_time=timedelta(minutes=10),
                ),
            ),
            Step(
                id="B", name="信号机更换",
                required_protections=frozenset({"P2"}),
                exit_config=StepExitConfig(
                    interruptible_points=(InterruptiblePoint("B-检查点1", timedelta(minutes=40)),),
                    evacuation_time=timedelta(minutes=15),
                ),
            ),
            Step(
                id="C", name="轨道巡检",
                required_protections=frozenset(),
                exit_config=StepExitConfig(
                    interruptible_points=(),
                    evacuation_time=timedelta(minutes=5),
                ),
            ),
        ],
    )
    window = Window(start=at(8), planned_end=at(12), end=at(12))
    return MaintenanceCoordinator(plan, window, clock)
