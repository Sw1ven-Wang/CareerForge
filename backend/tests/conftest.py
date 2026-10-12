import os

import pytest


def pytest_collection_modifyitems(items):
    if not os.getenv("TEST_DATABASE_URL"):
        marker = pytest.mark.skip(reason="TEST_DATABASE_URL missing; infrastructure not verified")
        for item in items:
            if "infrastructure" in item.keywords:
                item.add_marker(marker)
