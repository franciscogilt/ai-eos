"""Domain errors. The API maps them to HTTP status codes; nothing below the API knows HTTP."""

from __future__ import annotations


class DomainError(Exception):
    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class ForbiddenError(DomainError):
    code = "forbidden"


class AuthenticationError(DomainError):
    code = "unauthenticated"


class ServiceUnavailableError(DomainError):
    code = "unavailable"
