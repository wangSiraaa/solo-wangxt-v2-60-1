"""领域错误：所有被系统检查拦截的违规操作。"""


class DomainError(Exception):
    """领域错误基类。"""


class WindowStateError(DomainError):
    """窗口状态不允许该操作。"""


class HandoverStateError(DomainError):
    """交接班状态非法（重复发起、非新班接收等）。"""


class HandoverNotClosedError(DomainError):
    """交接未闭合，禁止完成销记。"""


class NotResponsibleError(DomainError):
    """操作者不是当前负责人。"""


class StaleConfirmationError(DomainError):
    """确认对应的许可版本已过期（版本不匹配）。"""


class ExpiredConfirmationError(DomainError):
    """确认已超过有效期。"""


class PermitInvalidError(DomainError):
    """开工许可已失效：禁止开工/完工记录，确认不得恢复资格。"""


class UnsafeExitError(DomainError):
    """已配置的退出条件未满足，异常协调不能闭合。"""


class CoordinationStateError(DomainError):
    """异常协调状态不允许该操作（或当前不在异常协调状态）。"""


class EquipmentCommandForbidden(DomainError):
    """系统只辅助记录和检查，不自行下达设备操作指令。"""
