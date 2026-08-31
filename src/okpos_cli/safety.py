"""Fail-closed exceptions shared by HTTP, pacing, and crawl orchestration."""


class SafetyStop(RuntimeError):
    """The crawl must stop rather than treating this as one bad screen."""


class RequestBudgetExceeded(SafetyStop):
    """The run consumed its explicit HTTP request budget."""


class CircuitOpen(SafetyStop):
    """Repeated infrastructure failures made further requests unsafe."""


class ServerBusy(SafetyStop):
    """The server stayed busy after bounded Retry-After/back-off retries."""


class AccessBlocked(SafetyStop):
    """The server denied access, possibly through an operator or WAF block."""


class AuthenticationUnavailable(SafetyStop):
    """Login could not safely establish a usable authenticated session."""


class RequiredResourceUnavailable(SafetyStop):
    """A required bootstrap resource could not be fetched or validated."""


class UnsafeRedirect(SafetyStop):
    """A redirect would escape the configured origin or exceed its hop limit."""
