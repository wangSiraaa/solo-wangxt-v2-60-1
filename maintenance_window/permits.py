"""许可登记：所有许可变更（签发/部分撤销/恢复）产生严格递增的版本号。

开工资格 = 当前版本许可覆盖所需保护措施 + 存在针对当前版本且未过期的保护确认。
过期（旧版本或已过有效期）的确认不得恢复已失效的开工资格。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .clock import Clock
from .errors import DomainError, ProtectionNotCovered, StalePermitVersion
from .models import AuditEvent, EventKind


@dataclass(frozen=True)
class Permit:
    version: int
    protections: frozenset[str]
    issued_at: datetime
    valid_until: datetime


@dataclass(frozen=True)
class ProtectionConfirmation:
    """保护确认：只对确认时的许可版本有效，版本推进后自动失效。"""

    permit_version: int
    protections: frozenset[str]
    confirmed_by: str
    confirmed_at: datetime
    valid_until: datetime


class PermitRegistry:
    """许可与保护确认登记（只记录与检查，不对设备下发任何指令）。"""

    def __init__(self, clock: Clock, emit: Callable[[AuditEvent], None] | None = None):
        self._clock = clock
        self.events: list[AuditEvent] = []
        self._emit = emit if emit is not None else self.events.append
        self._permits: list[Permit] = []
        self._confirmations: list[ProtectionConfirmation] = []

    @property
    def current_permit(self) -> Permit | None:
        return self._permits[-1] if self._permits else None

    @property
    def permits(self) -> tuple[Permit, ...]:
        return tuple(self._permits)

    @property
    def confirmations(self) -> tuple[ProtectionConfirmation, ...]:
        return tuple(self._confirmations)

    def issue(self, protections: Iterable[str], valid_until: datetime) -> Permit:
        """签发许可，版本号递增。"""
        at = self._clock.now()
        version = self._permits[-1].version + 1 if self._permits else 1
        permit = Permit(version, frozenset(protections), at, valid_until)
        self._permits.append(permit)
        self._emit(AuditEvent(at, EventKind.PERMIT_ISSUED,
                              f"许可 v{version} 签发，保护措施: {sorted(permit.protections)}"))
        return permit

    def revoke(self, protections_to_remove: Iterable[str]) -> Permit:
        """部分撤销保护措施：产生新版本，旧版本的所有确认随之失效。"""
        current = self._require_current()
        removed = frozenset(protections_to_remove)
        remaining = current.protections - removed
        at = self._clock.now()
        permit = Permit(current.version + 1, remaining, at, current.valid_until)
        self._permits.append(permit)
        self._emit(AuditEvent(at, EventKind.PERMIT_REVOKED,
                              f"许可 v{permit.version}：撤销 {sorted(removed)}，剩余 {sorted(remaining)}"))
        return permit

    def confirm(self, permit_version: int, protections: Iterable[str],
                by: str, valid_until: datetime) -> ProtectionConfirmation:
        """登记保护确认；只允许针对当前许可版本确认。"""
        at = self._clock.now()
        current = self._require_current()
        if permit_version != current.version:
            self._emit(AuditEvent(at, EventKind.STALE_CONFIRMATION_REJECTED,
                                  f"{by} 针对 v{permit_version} 的确认被拒绝（当前版本 v{current.version}）"))
            raise StalePermitVersion(
                f"确认针对已过期的许可版本 v{permit_version}，当前版本为 v{current.version}；"
                "过期确认不得恢复已失效的开工资格")
        requested = frozenset(protections)
        if not requested <= current.protections:
            raise ProtectionNotCovered(
                f"确认的保护 {sorted(requested - current.protections)} 不在当前许可范围内")
        confirmation = ProtectionConfirmation(
            permit_version=permit_version,
            protections=requested,
            confirmed_by=by,
            confirmed_at=at,
            valid_until=min(valid_until, current.valid_until),
        )
        self._confirmations.append(confirmation)
        self._emit(AuditEvent(at, EventKind.PERMIT_CONFIRMED,
                              f"{by} 确认 v{permit_version} 保护 {sorted(requested)}"))
        return confirmation

    def is_work_eligible(self, required_protections: Iterable[str],
                         at: datetime | None = None) -> bool:
        """开工资格检查：只认可当前版本许可 + 当前版本且未过期的确认。"""
        at = at if at is not None else self._clock.now()
        required = frozenset(required_protections)
        if not required:
            return True
        current = self.current_permit
        if current is None or at >= current.valid_until:
            return False
        if not required <= current.protections:
            return False
        for confirmation in reversed(self._confirmations):
            if confirmation.permit_version != current.version:
                continue  # 旧版本确认已过期，不得恢复资格
            if not required <= confirmation.protections:
                continue
            if at < confirmation.valid_until:
                return True
        return False

    def _require_current(self) -> Permit:
        current = self.current_permit
        if current is None:
            raise DomainError("尚无有效许可")
        return current
