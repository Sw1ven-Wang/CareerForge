from typing import Final

TERMINAL: Final = frozenset(
    {"succeeded", "partially_succeeded", "failed", "budget_stopped", "cancelled"}
)
TRANSITIONS: Final = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"queued", "waiting_user", "waiting_children", *TERMINAL},
    "waiting_user": {"queued", "cancelled"},
    "waiting_children": {"queued", "failed", "budget_stopped", "cancelled"},
    **{status: set() for status in TERMINAL},
}


class LeaseLost(RuntimeError):
    """The caller must stop; it no longer owns execution rights."""
