import os
from unittest.mock import patch

from fastapi.testclient import TestClient


def test_slow_requests_are_logged_with_path_time_and_size(caplog):
    import main_ai
    with patch.object(main_ai.app.router, "on_startup", []), TestClient(main_ai.app) as client, \
            patch.dict(os.environ, {"SLOW_REQUEST_SECONDS": "0"}), caplog.at_level("WARNING"):
        client.get("/version")
    lines = [r.getMessage() for r in caplog.records if "slow request" in r.getMessage()]
    assert lines and "path=/version" in lines[0] and "server_seconds=" in lines[0] and "bytes=" in lines[0]


def test_fast_requests_are_not_logged(caplog):
    import main_ai
    with patch.object(main_ai.app.router, "on_startup", []), TestClient(main_ai.app) as client, \
            patch.dict(os.environ, {"SLOW_REQUEST_SECONDS": "60"}), caplog.at_level("WARNING"):
        client.get("/version")
    assert not [r for r in caplog.records if "slow request" in r.getMessage()]
