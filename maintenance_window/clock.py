"""时钟抽象：便于在模拟中精确控制时间（如交班确认迟到、确认过期）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class ManualClock:
    """手动时钟，测试/模拟中通过 set/advance 推进时间。"""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, delta: timedelta) -> None:
        self._now += delta
