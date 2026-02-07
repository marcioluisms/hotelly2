"""Shared pytest fixtures for Hotelly V2 tests."""
import sys
sys.dont_write_bytecode = True

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_oidc_jwks_cache():
    """Reset global JWKS cache to avoid cross-test contamination.

    The OIDC JWKS cache is a module-level global that persists between tests.
    Without this reset, tests may fail intermittently with 401 errors when
    a cached JWKS from a previous test doesn't match the current test's keys.
    """
    import hotelly.api.auth as auth_module

    # Reset before test
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0
    yield
    # Reset after test (teardown)
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0
