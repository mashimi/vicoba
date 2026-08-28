import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app import auth


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """The failed-login tracker is process-global. Reset it around every test
    so attempts from the TestClient (ip='testclient') never leak across cases."""
    auth.clear_login_failures("testclient")
    yield
    auth.clear_login_failures("testclient")
