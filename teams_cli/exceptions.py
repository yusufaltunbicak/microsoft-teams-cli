"""Structured exception hierarchy for microsoft-teams-cli."""

from __future__ import annotations


class TeamsCliError(Exception):
    """Base exception for all Teams CLI errors."""

    def __init__(self, message: str = "", code: str = ""):
        self.code = code
        super().__init__(message)


class AuthRequiredError(TeamsCliError):
    """No valid token available — login required."""

    def __init__(self, message: str = "Authentication required. Run: teams login"):
        super().__init__(message, code="auth_required")


class TokenExpiredError(TeamsCliError):
    """Token has expired — re-login required."""

    def __init__(self, message: str = "Token expired. Run: teams login"):
        super().__init__(message, code="token_expired")


class RateLimitError(TeamsCliError):
    """API returned 429 — too many requests."""

    def __init__(self, message: str = "Rate limited"):
        super().__init__(message, code="rate_limited")


class ResourceNotFoundError(TeamsCliError):
    """Requested resource (chat, message, user) not found."""

    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, code="not_found")


class ApiError(TeamsCliError):
    """Unexpected API error with status code."""

    def __init__(self, message: str = "API error", status_code: int = 0):
        self.status_code = status_code
        super().__init__(message, code="api_error")


class RetryableError(TeamsCliError):
    """Transient upstream or network failure that can be retried."""

    def __init__(self, message: str = "Retryable upstream error"):
        super().__init__(message, code="retryable")


class ConfigurationError(TeamsCliError):
    """Local config/cache/auth bundle problem."""

    def __init__(self, message: str = "Configuration error"):
        super().__init__(message, code="config_error")
