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


def test_request_that_does_not_finish_reports_itself_with_thread_stacks(caplog, capfd):
    import threading
    import main_ai
    gate = threading.Event()

    @main_ai.app.get("/dashboard/_stuck_for_test")
    def stuck():
        gate.wait(5)
        return {"ok": True}

    with patch.object(main_ai.app.router, "on_startup", []), TestClient(main_ai.app) as client, \
            patch.dict(os.environ, {"STILL_RUNNING_SECONDS": "0"}), caplog.at_level("WARNING"):
        client.get("/dashboard/_stuck_for_test")
    gate.set()
    lines = [r.getMessage() for r in caplog.records if "request still running" in r.getMessage()]
    assert lines and "path=/dashboard/_stuck_for_test" in lines[0]
    assert "thread" in capfd.readouterr().err.lower()  # faulthandler printed the thread stacks


def test_fast_dashboard_request_stays_silent(caplog):
    import main_ai
    with patch.object(main_ai.app.router, "on_startup", []), TestClient(main_ai.app) as client, \
            patch.dict(os.environ, {"STILL_RUNNING_SECONDS": "30"}), caplog.at_level("WARNING"):
        client.get("/dashboard/automation", follow_redirects=False)
    assert not [r for r in caplog.records if "request still running" in r.getMessage()]
