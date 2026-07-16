"""Private, local-only activation metrics for onSense beta validation.

The file intentionally records only aggregate counters and UTC dates. It never
stores phone/PC addresses, tokens, filenames, tool arguments, response content,
or user identifiers, and nothing is uploaded automatically.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import os
import tempfile


SCHEMA = 1


def _home() -> str:
    return os.environ.get("ONSENSE_HOME") or os.path.join(os.path.expanduser("~"), ".onsense")


def usage_path() -> str:
    return os.path.join(_home(), "usage.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _blank(now: datetime | None = None) -> dict:
    now = now or _now()
    return {
        "schema": SCHEMA,
        "created_at": _stamp(now),
        "pair": {"successes": 0, "first_at": None, "last_at": None},
        "tools": {
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "first_success_at": None,
            "last_success_at": None,
            "by_name": {},
        },
        "active_days": [],
    }


def _load_unlocked() -> dict:
    try:
        with open(usage_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema") == SCHEMA:
            return data
    except Exception:
        pass
    return _blank()


def _write_unlocked(data: dict) -> None:
    home = _home()
    os.makedirs(home, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="usage.", suffix=".tmp", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, usage_path())
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    try:
        os.chmod(usage_path(), 0o600)
    except OSError:
        pass


@contextmanager
def _locked():
    """Serialize updates from multiple stdio MCP processes."""
    os.makedirs(_home(), exist_ok=True)
    path = usage_path() + ".lock"
    with open(path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update(mutator) -> None:
    """Best-effort only: metrics must never break pairing or an MCP tool."""
    try:
        with _locked():
            data = _load_unlocked()
            mutator(data)
            _write_unlocked(data)
    except Exception:
        pass


def record_pair(now: datetime | None = None) -> None:
    now = now or _now()
    stamp = _stamp(now)

    def mutate(data):
        pair = data["pair"]
        pair["successes"] += 1
        pair["first_at"] = pair["first_at"] or stamp
        pair["last_at"] = stamp

    _update(mutate)


def record_tool(name: str, success: bool, now: datetime | None = None) -> None:
    now = now or _now()
    stamp = _stamp(now)
    day = now.date().isoformat()

    def mutate(data):
        tools = data["tools"]
        tools["calls"] += 1
        key = "successes" if success else "failures"
        tools[key] += 1
        by_name = tools["by_name"].setdefault(name, {"successes": 0, "failures": 0})
        by_name[key] += 1
        if success:
            tools["first_success_at"] = tools["first_success_at"] or stamp
            tools["last_success_at"] = stamp
            days = set(data.get("active_days", []))
            days.add(day)
            data["active_days"] = sorted(days)[-90:]

    _update(mutate)


def track_tool(name: str):
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                value = func(*args, **kwargs)
            except Exception:
                record_tool(name, False)
                raise
            record_tool(name, True)
            return value

        return wrapped

    return decorate


def snapshot(now: datetime | None = None) -> dict:
    now = now or _now()
    try:
        with _locked():
            data = _load_unlocked()
    except Exception:
        data = _blank(now)
    cutoff = (now.date() - timedelta(days=6)).isoformat()
    data = json.loads(json.dumps(data))
    data["summary"] = {
        "active_days_last_7": sum(day >= cutoff for day in data.get("active_days", [])),
        "automatic_upload": False,
        "contains_personal_data": False,
    }
    return data


def reset() -> None:
    try:
        with _locked():
            _write_unlocked(_blank())
    except Exception:
        pass


def format_summary(data: dict) -> str:
    pair = data["pair"]
    tools = data["tools"]
    summary = data["summary"]
    return "\n".join([
        "onSense local usage stats (never uploaded automatically)",
        f"pair successes: {pair['successes']}",
        f"successful tool calls: {tools['successes']} / {tools['calls']}",
        f"active days (last 7): {summary['active_days_last_7']}",
        f"first value: {tools['first_success_at'] or 'not yet'}",
        f"last value: {tools['last_success_at'] or 'not yet'}",
        f"local file: {usage_path()}",
    ])
