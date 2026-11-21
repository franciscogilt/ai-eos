"""Agent-step failures. The activity maps each to a non-retryable Temporal error type."""

from __future__ import annotations


class AgentStepError(RuntimeError):
    error_type = "AgentStepFailed"


class AgentNotAvailableError(AgentStepError):
    error_type = "AgentNotAvailable"


class ContractViolationError(AgentStepError):
    """The deployed contract or prompt differs from the published version the run was created with."""

    error_type = "ContractViolation"


class AgentOutputRejectedError(AgentStepError):
    """The agent kept producing output that fails validation or grounding after its retries."""

    error_type = "AgentOutputRejected"


class AgentLimitError(AgentStepError):
    """Turn, tool-call or time limit from the contract was reached."""

    error_type = "AgentLimitExceeded"
