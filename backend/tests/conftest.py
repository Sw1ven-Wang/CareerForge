import os

import pytest


def pytest_collection_modifyitems(items):
    if not os.getenv("TEST_DATABASE_URL"):
        marker = pytest.mark.skip(reason="TEST_DATABASE_URL missing; infrastructure not verified")
        for item in items:
            if "infrastructure" in item.keywords:
                item.add_marker(marker)


@pytest.fixture
def offline_python_sandbox(monkeypatch):
    """Explicit inventory for tests which already substitute the sandbox runner."""
    from careerforge.infrastructure import runtime_capabilities as runtime

    value = {
        "version": "sandbox-capabilities-1",
        "status": "ready",
        "ready": True,
        "tools": {name: name in {"python", "python3", "uv"} for name in runtime.TOOLS},
        "allowed_commands": sorted(runtime.ALLOWED),
        "limits": runtime.LIMITS,
    }
    monkeypatch.setattr(runtime, "capabilities", lambda: value)
    return value
