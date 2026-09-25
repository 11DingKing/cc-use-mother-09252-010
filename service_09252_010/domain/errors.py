"""领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "domain_error"


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class AuthenticationError(DomainError):
    status = 401
    code = "unauthenticated"


class AuthorizationError(DomainError):
    status = 403
    code = "forbidden"
