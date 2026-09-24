"""交接班：新班确认接收前，旧班不得交出责任；确认迟到不生效。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from .errors import HandoverError, HandoverNotConfirmed, LateHandoverConfirmation


class HandoverState(Enum):
    OFFERED = auto()             # 旧班已发起，待新班确认接收
    RECEIPT_CONFIRMED = auto()   # 新班已确认接收
    CLOSED = auto()              # 旧班已交出责任，交接闭合


@dataclass(frozen=True)
class LateConfirmation:
    by: str
    at: datetime


class ShiftHandover:
    def __init__(self, from_shift: str, to_shift: str,
                 confirm_by: datetime, offered_at: datetime):
        self.from_shift = from_shift
        self.to_shift = to_shift
        self.confirm_by = confirm_by
        self.offered_at = offered_at
        self.state = HandoverState.OFFERED
        self.confirmed_by: str | None = None
        self.confirmed_at: datetime | None = None
        self.released_by: str | None = None
        self.released_at: datetime | None = None
        self.late_confirmation: LateConfirmation | None = None

    def confirm_receipt(self, by: str, at: datetime) -> None:
        """新班确认接收；超过确认时限的迟到确认只记录、不生效。"""
        if self.state is not HandoverState.OFFERED:
            raise HandoverError(f"当前交接状态为 {self.state.name}，不能确认接收")
        if at > self.confirm_by:
            self.late_confirmation = LateConfirmation(by, at)
            raise LateHandoverConfirmation(
                f"{by} 的接收确认迟到（截止 {self.confirm_by}，实际 {at}），本次确认不生效，交接保持未闭合")
        self.state = HandoverState.RECEIPT_CONFIRMED
        self.confirmed_by = by
        self.confirmed_at = at

    def release(self, by: str, at: datetime) -> None:
        """旧班交出责任：必须以新班已确认接收为前提。"""
        if self.state is not HandoverState.RECEIPT_CONFIRMED:
            raise HandoverNotConfirmed("新班尚未确认接收，旧班不得交出责任")
        self.state = HandoverState.CLOSED
        self.released_by = by
        self.released_at = at
