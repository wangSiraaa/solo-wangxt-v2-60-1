"""领域规则错误。

系统只辅助记录和检查：所有违背调度安全规则的操作都以异常形式拒绝，
并留下审计记录，而不是由系统代为执行或强行放行。
"""


class DomainError(Exception):
    """领域规则错误基类。"""


class WindowError(DomainError):
    """窗口调整请求不合法（压缩/恢复的时间关系错误）。"""


class InvalidStepTransition(DomainError):
    """步骤状态流转不合法（含：禁止把无法安全退出的步骤强制改为完成）。"""


class WorkNotEligible(DomainError):
    """开工资格已失效（许可/保护确认不满足当前版本要求）。"""


class HandoverError(DomainError):
    """交接班相关错误基类。"""


class HandoverNotConfirmed(HandoverError):
    """新班未确认接收，旧班不得交出责任。"""


class LateHandoverConfirmation(HandoverError):
    """交班确认迟到：确认被记录但不生效，交接保持未闭合。"""


class HandoverNotClosed(DomainError):
    """交接未闭合，禁止完成销记。"""


class UnresolvedExceptionCoordination(DomainError):
    """异常协调状态未解除，禁止销记。"""


class StepsNotClosed(DomainError):
    """存在未闭合（未完成也未安全撤离）的步骤。"""


class RecommendationNotConfirmed(DomainError):
    """最新收尾建议未经负责人确认。"""


class RecommendationNotActionable(DomainError):
    """建议已被新版本取代或已处理，不能再确认。"""


class StalePermitVersion(DomainError):
    """确认针对的是已过期的许可版本，不得恢复已失效的开工资格。"""


class ProtectionNotCovered(DomainError):
    """确认的保护措施超出当前许可范围。"""
