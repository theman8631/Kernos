"""KERNOS reasoning exception hierarchy.

The handler imports only these — never the provider SDK's own exceptions.
"""


class ReasoningError(Exception):
    """Base class for all reasoning service errors."""


class ReasoningTimeoutError(ReasoningError):
    """Provider call timed out."""


class ReasoningConnectionError(ReasoningError):
    """Could not connect to provider."""


class ReasoningRateLimitError(ReasoningError):
    """Provider rate limit exceeded."""


class ReasoningProviderError(ReasoningError):
    """API status error or unexpected provider error."""
