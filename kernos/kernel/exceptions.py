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


class ReasoningTransientError(ReasoningError):
    """Transient server-side error that should be retried."""


class LLMChainExhausted(ReasoningError):
    """Every provider in a named chain failed on this turn.

    Raised by ``ReasoningService._call_chain`` after exhausting the chain.
    The handler catches this exception specifically and delivers a
    pre-rendered failure message to the user via the platform adapter,
    *instead of* an LLM reply — on turns where this exception fires, the
    agent never produces an LLM response at all.

    Attributes:
        chain_name: The named chain that failed (e.g. "primary").
        attempts:   A list of ``(provider_name, model, reason)`` tuples,
                    one per entry in the chain. Useful for diagnostics
                    and for the pre-rendered message.
    """

    def __init__(
        self,
        chain_name: str,
        attempts: list[tuple[str, str, str]],
    ) -> None:
        self.chain_name = chain_name
        self.attempts = list(attempts)
        summary = ", ".join(
            f"{p}/{m} ({r[:80]})" for p, m, r in self.attempts
        ) or "no entries"
        super().__init__(
            f"Chain '{chain_name}' exhausted: {summary}"
        )
