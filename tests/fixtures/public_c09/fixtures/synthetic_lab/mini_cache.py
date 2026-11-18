"""Synthetic cache for exercise grading, not any external project's code."""

class TTLCache:
    def __init__(self):
        self.items = {}

    def put(self, key: str, value: str, ttl: float, now: float) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.items[key] = (value, now + ttl)

    def get(self, key: str, now: float):
        item = self.items.get(key)
        if item is None:
            return None
        value, expires_at = item
        if now >= expires_at:
            self.items.pop(key, None)
            return None
        return value
