"""Port-fallback regression test for `onsense pair` — a busy PAIR_PORT must not crash the CLI
(2026-07-08: r0b machine hit a raw OSError because another local service already held 8765)."""
import socket
from http.server import HTTPServer
from unittest.mock import Mock, patch

import pytest

from onsense import pair


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_binds_default_port_when_free():
    free_port = _free_port()
    httpd, bound_port = pair._bind_pair_server("127.0.0.1", free_port, pair.BaseHTTPRequestHandler)
    try:
        assert bound_port == free_port == httpd.server_address[1]
    finally:
        httpd.server_close()


def test_falls_back_to_next_free_port_when_busy():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        httpd, bound_port = pair._bind_pair_server("127.0.0.1", busy_port, pair.BaseHTTPRequestHandler)
        try:
            assert bound_port != busy_port
            assert bound_port > busy_port
        finally:
            httpd.server_close()
    finally:
        blocker.close()


def test_retries_on_non_linux_errno_too():
    # 2026-07-08: g14 (Windows 11) real-machine check found the first version of this fallback
    # only retried on errno==98 (Linux EADDRINUSE), so it never retried on Windows at all — a
    # .NET TcpListener blocker there raised WinError 10013 (WSAEACCES via SO_EXCLUSIVEADDRUSE),
    # which fell straight through to the "no free port" path on the very first candidate. Simulate
    # that with a fake handler class whose first bind attempt raises an arbitrary non-EADDRINUSE
    # OSError, to make sure the loop keeps trying the next port regardless of errno.
    free_port = _free_port()
    calls = []

    class FlakyHandler(pair.BaseHTTPRequestHandler):
        pass

    real_init = HTTPServer.__init__

    def fake_init(self, addr, handler):
        calls.append(addr[1])
        if len(calls) == 1:
            raise OSError(13, "Permission denied")  # arbitrary non-EADDRINUSE errno
        real_init(self, addr, handler)

    HTTPServer.__init__ = fake_init
    try:
        httpd, bound_port = pair._bind_pair_server("127.0.0.1", free_port, FlakyHandler)
        try:
            assert bound_port == free_port + 1
            assert calls == [free_port, free_port + 1]
        finally:
            httpd.server_close()
    finally:
        HTTPServer.__init__ = real_init


def test_pair_server_uses_exclusive_port_on_windows(monkeypatch):
    """A live older QR listener must never share the same Windows port."""
    httpd = object.__new__(pair.PairHTTPServer)
    httpd.socket = Mock()
    monkeypatch.setattr(pair.platform, "system", lambda: "Windows")
    monkeypatch.setattr(pair.socket, "SO_EXCLUSIVEADDRUSE", 0x100, raising=False)

    with patch.object(HTTPServer, "server_bind") as parent_bind:
        httpd.server_bind()

    assert httpd.allow_reuse_address is False
    httpd.socket.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 0x100, 1)
    parent_bind.assert_called_once_with()


def test_pair_server_keeps_fast_rebind_on_unix(monkeypatch):
    httpd = object.__new__(pair.PairHTTPServer)
    httpd.socket = Mock()
    monkeypatch.setattr(pair.platform, "system", lambda: "Linux")

    with patch.object(HTTPServer, "server_bind") as parent_bind:
        httpd.server_bind()

    assert httpd.allow_reuse_address is True
    httpd.socket.setsockopt.assert_not_called()
    parent_bind.assert_called_once_with()


def test_raises_friendly_error_when_all_candidates_busy():
    blockers = []
    try:
        base_port = None
        for _ in range(3):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if base_port is None:
                s.bind(("127.0.0.1", 0))
                base_port = s.getsockname()[1]
            else:
                s.bind(("127.0.0.1", base_port + len(blockers)))
            s.listen(1)
            blockers.append(s)
        with pytest.raises(OSError, match="Could not find a free port"):
            pair._bind_pair_server("127.0.0.1", base_port, pair.BaseHTTPRequestHandler, tries=len(blockers))
    finally:
        for s in blockers:
            s.close()
