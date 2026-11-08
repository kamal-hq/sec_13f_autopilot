"""Network and external I/O gateways.

Architectural invariant: Only classes in `autopilot.gateways` may perform network I/O
or interact with external APIs (SEC EDGAR, Google Gemini, manual file exchange).
"""

from __future__ import annotations

from autopilot.gateways.http import (
    HttpClient,
    HttpClientError,
    HttpConnectionError,
    HttpError,
    HttpRateLimitError,
    HttpResponse,
    HttpServerError,
    HttpTimeoutError,
    RateLimiter,
)
from autopilot.gateways.llm import (
    BudgetGuard,
    GeminiProvider,
    LLMBudgetExceededError,
    LLMConfigurationError,
    LLMDisabledError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMTimeoutError,
    ManualProvider,
    NeedsManualInput,
    NullProvider,
    select_llm_provider,
)
from autopilot.gateways.sec import (
    SecClientError,
    SecConfigurationError,
    SecError,
    SecGateway,
    SecNetworkError,
    SecNotFoundError,
    SecRateLimitError,
    SecServerError,
)

__all__ = [
    "BudgetGuard",
    "GeminiProvider",
    "HttpClient",
    "HttpClientError",
    "HttpConnectionError",
    "HttpError",
    "HttpRateLimitError",
    "HttpResponse",
    "HttpServerError",
    "HttpTimeoutError",
    "LLMBudgetExceededError",
    "LLMConfigurationError",
    "LLMDisabledError",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "ManualProvider",
    "NeedsManualInput",
    "NullProvider",
    "RateLimiter",
    "SecClientError",
    "SecConfigurationError",
    "SecError",
    "SecGateway",
    "SecNetworkError",
    "SecNotFoundError",
    "SecRateLimitError",
    "SecServerError",
    "select_llm_provider",
]
