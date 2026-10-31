"""Exception hierarchy used across the harness."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness errors."""


class ConfigError(HarnessError):
    """Configuration is missing or invalid."""


class PermissionDenied(HarnessError):
    """A change was attempted without a valid human approval (or in read-only mode)."""


class WorkflowError(HarnessError):
    """An approval-workflow rule was violated (e.g. executing a rejected proposal)."""


class StaleProposalError(WorkflowError):
    """The ticket changed after the proposal was reviewed; it must be re-reviewed."""


class TemplateError(HarnessError):
    """A closure template is invalid or cannot be rendered."""


class RedmineError(HarnessError):
    """An error returned by (or while talking to) the Redmine REST API."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        errors: list[str] | None = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []
        self.method = method
        self.url = url

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.errors:
            return f"{base} Details: {'; '.join(self.errors)}"
        return base


class RedmineConnectionError(RedmineError):
    """Network / TLS / timeout problem reaching Redmine."""


class AuthenticationError(RedmineError):
    """HTTP 401 - invalid credentials or REST API disabled."""


class ForbiddenError(RedmineError):
    """HTTP 403 - the account lacks permission for this resource/action."""


class NotFoundError(RedmineError):
    """HTTP 404 - resource does not exist or is not visible to the account."""


class ImpersonationError(RedmineError):
    """HTTP 412 - X-Redmine-Switch-User login does not exist or is inactive."""


class ValidationError(RedmineError):
    """HTTP 422 - Redmine rejected the payload; ``errors`` holds the messages."""


class ServerError(RedmineError):
    """HTTP 5xx - Redmine (or a proxy in front of it) failed."""
