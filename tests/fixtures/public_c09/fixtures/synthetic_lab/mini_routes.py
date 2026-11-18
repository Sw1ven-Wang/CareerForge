"""Synthetic routing example for exercise grading."""

ALLOWED = {"review", "end"}

def route(value: str) -> str:
    if value not in ALLOWED:
        raise ValueError("invalid destination")
    return value
