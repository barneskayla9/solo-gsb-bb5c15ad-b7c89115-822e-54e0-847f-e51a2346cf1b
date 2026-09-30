"""领域错误：service 层抛出，路由层映射为对应的 HTTP 状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class VersionConflict(Conflict):
    """原料更正携带的期望版本与当前版本不一致。"""

    code = "version_conflict"


class AlreadyFinalized(Conflict):
    """已定版批次的来源/声明不可再修改。"""

    code = "already_finalized"


class ValidationFailure(DomainError):
    """空来源、未知来源、循环引用等输入/图校验失败。"""

    status_code = 400
    code = "validation_error"
