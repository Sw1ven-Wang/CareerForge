class DomainError(Exception):
    """A stable business error safe to expose without leaking provider or DB details."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class PauseBoundary(Exception):
    """The current worker reached a cooperative workflow checkpoint."""


class LeaseLost(DomainError):
    def __init__(self):
        super().__init__("LEASE_LOST", "运行已取消或执行租约失效。")


class GraphCheckpointUnavailable(RuntimeError):
    """Transient checkpoint transport loss; retain lease for existing recovery/reconciliation."""
