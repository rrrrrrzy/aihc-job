"""Exception types for the AIHC job tool.

Every failure path raises one of these so the CLI can map it to a stable exit
code (see ``aihc_job.cli.EXIT_*``) instead of leaking tracebacks to callers.
"""

from __future__ import annotations


class AihcError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(AihcError):
    """Credentials / region / pool / queue could not be resolved."""


class TemplateError(AihcError):
    """A job template (or CLI override) is malformed or incomplete."""


class ApiError(AihcError):
    """The AIHC OpenAPI returned a non-2xx response.

    Attributes mirror the BCE error envelope so callers can branch on ``code``
    (e.g. ``cce.warning.GetAIJobByJobIdFailed``) rather than parsing messages.
    """

    def __init__(
        self,
        status_code: int,
        code: str = "",
        message: str = "",
        request_id: str = "",
        action: str = "",
        raw: str = "",
    ) -> None:
        detail = message or raw or "<empty response body>"
        super().__init__(
            f"{action or 'request'} failed: HTTP {status_code}"
            f"{f' {code}' if code else ''}: {detail}"
            f"{f' (requestId={request_id})' if request_id else ''}"
        )
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.action = action
        self.raw = raw


class JobFailed(AihcError):
    """A job reached a terminal non-success state while being waited on."""

    def __init__(self, job_id: str, status: str, detail: str = "") -> None:
        super().__init__(f"job {job_id} ended in state {status}{f': {detail}' if detail else ''}")
        self.job_id = job_id
        self.status = status


class WaitTimeout(AihcError):
    """``wait``/``submit --wait`` exceeded its timeout."""

    def __init__(self, job_id: str, status: str, timeout: float) -> None:
        super().__init__(
            f"timed out after {timeout:.0f}s waiting for job {job_id} (last status: {status})"
        )
        self.job_id = job_id
        self.status = status
