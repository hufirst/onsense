"""Local-only beta metrics must be useful without collecting sensitive data."""
from datetime import datetime, timezone
import json
import os

import pytest

from onsense import metrics


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSENSE_HOME", str(tmp_path))
    return tmp_path


def test_records_pair_first_value_and_active_days(isolated_home):
    day1 = datetime(2026, 7, 10, 1, 2, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 11, 3, 4, tzinfo=timezone.utc)
    metrics.record_pair(day1)
    metrics.record_tool("get_live_frame", True, day1)
    metrics.record_tool("read_sensors", True, day2)
    metrics.record_tool("get_photo", False, day2)

    data = metrics.snapshot(day2)
    assert data["pair"]["successes"] == 1
    assert data["tools"]["calls"] == 3
    assert data["tools"]["successes"] == 2
    assert data["tools"]["failures"] == 1
    assert data["active_days"] == ["2026-07-10", "2026-07-11"]
    assert data["summary"]["active_days_last_7"] == 2
    assert data["summary"]["automatic_upload"] is False


def test_usage_file_contains_no_arguments_or_identifiers(isolated_home):
    metrics.record_pair()
    metrics.record_tool("get_reference", True)
    raw = metrics.usage_path()
    body = open(raw, encoding="utf-8").read()
    data = json.loads(body)

    assert set(data) == {"schema", "created_at", "pair", "tools", "active_days"}
    assert "token" not in body.lower()
    assert "192.168." not in body
    assert "filename" not in body.lower()
    if os.name != "nt":
        assert os.stat(raw).st_mode & 0o777 == 0o600


def test_tool_decorator_records_success_and_failure(isolated_home):
    @metrics.track_tool("demo")
    def demo(ok):
        if not ok:
            raise RuntimeError("expected")
        return "ok"

    assert demo(True) == "ok"
    with pytest.raises(RuntimeError, match="expected"):
        demo(False)
    data = metrics.snapshot()
    assert data["tools"]["by_name"]["demo"] == {"successes": 1, "failures": 1}


def test_reset_clears_counters(isolated_home):
    metrics.record_pair()
    metrics.record_tool("get_live_frame", True)
    metrics.reset()
    data = metrics.snapshot()
    assert data["pair"]["successes"] == 0
    assert data["tools"]["calls"] == 0
