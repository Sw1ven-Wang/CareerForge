from typing import Final

TERMINAL: Final = frozenset(
    {"succeeded", "partially_succeeded", "failed", "budget_stopped", "cancelled"}
)
TRANSITIONS: Final = {
    "queued": {"running", "cancelled"},
    "running": {"queued", "waiting_user", *TERMINAL},
    "waiting_user": {"queued", "cancelled"},
    **{status: set() for status in TERMINAL},
}


class LeaseLost(RuntimeError):
    """The caller must stop; it no longer owns execution rights."""
