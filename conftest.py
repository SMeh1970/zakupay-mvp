"""Release unreachable SQLite connections before Windows removes test folders."""
import gc
import sys
import tempfile

if sys.platform == 'win32':
    _cleanup = tempfile.TemporaryDirectory.cleanup

    def _collect_then_cleanup(self):
        gc.collect()
        _cleanup(self)

    tempfile.TemporaryDirectory.cleanup = _collect_then_cleanup


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _listing_defaults(monkeypatch):
    """Tests use fixed request dates: list all dates unless a test sets LIST_RECENT_DAYS itself."""
    monkeypatch.setenv("LIST_RECENT_DAYS", "0")
    from saved_dashboard import reset_listing_cache
    reset_listing_cache()
    yield
    reset_listing_cache()
