"""Token source-of-truth priority regression tests.

Root cause covered: a stale exported PHONE_TOKEN silently overriding a fresh pairing
(pair.json), making the clip daemon reject every phone request with 401
("paired OK but send fails"). pair.json must win; env is a fallback for un-paired
dev setups only. doctor must diagnose with the same priority as the daemons.
"""
import json
import os
import errno
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from onsense import auth, clip, doctor, server


@pytest.fixture
def onsense_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSENSE_HOME", str(tmp_path))
    monkeypatch.delenv("ONSENSE_TOKEN", raising=False)
    monkeypatch.delenv("PHONE_TOKEN", raising=False)
    return tmp_path


def _write_pair(home, token):
    (home / "pair.json").write_text(
        json.dumps({"base": "http://192.168.0.5:8080", "token": token}), encoding="utf-8")


def test_pair_json_beats_stale_env(onsense_home, monkeypatch):
    """The original bug: exported test token must NOT override a fresh pairing."""
    _write_pair(onsense_home, "fresh-paired-token")
    monkeypatch.setenv("PHONE_TOKEN", "testtoken1234")
    assert clip.load_token() == "fresh-paired-token"
    monkeypatch.setenv("ONSENSE_TOKEN", "another-stale")
    assert clip.load_token() == "fresh-paired-token"


def test_env_fallback_when_not_paired(onsense_home, monkeypatch):
    """Un-paired dev setups keep working: env is used only when pair.json has no token."""
    monkeypatch.setenv("PHONE_TOKEN", "dev-token")
    assert clip.load_token() == "dev-token"
    monkeypatch.setenv("ONSENSE_TOKEN", "dev-token-2")  # ONSENSE_TOKEN wins over PHONE_TOKEN
    assert clip.load_token() == "dev-token-2"


def test_empty_pair_json_falls_back_to_env(onsense_home, monkeypatch):
    _write_pair(onsense_home, "")
    monkeypatch.setenv("PHONE_TOKEN", "dev-token")
    assert clip.load_token() == "dev-token"


def test_no_token_anywhere(onsense_home):
    assert clip.load_token() == ""
    assert clip.token_fp() == ""


def test_token_fp_stable_and_nonsecret(onsense_home):
    _write_pair(onsense_home, "fresh-paired-token")
    fp = clip.token_fp()
    assert len(fp) == 8
    assert fp == clip.token_fp("fresh-paired-token")   # deterministic
    assert fp != clip.token_fp("testtoken1234")        # distinguishes tokens
    assert "fresh" not in fp                            # not the token itself


def test_explicit_serve_token_beats_pair_and_env(onsense_home, monkeypatch):
    _write_pair(onsense_home, "paired-token")
    monkeypatch.setenv("PHONE_TOKEN", "ambient-token")
    monkeypatch.setenv("_ONSENSE_EXPLICIT_TOKEN", "explicit-token")
    assert clip.load_token() == "explicit-token"
    assert server._current_token() == "explicit-token"


@pytest.mark.parametrize("running_fp", [None, "wrong-fp"])
def test_spawn_detached_replaces_missing_or_wrong_token_fp(
        onsense_home, monkeypatch, running_fp):
    _write_pair(onsense_home, "paired-token")
    monkeypatch.setattr(clip, "_daemon_alive", lambda: True)
    monkeypatch.setattr(clip, "_health", lambda: {
        "app": "onsense-clip",
        "version": clip.__version__,
        **({} if running_fp is None else {"token_fp": running_fp}),
    })
    popen = Mock()
    monkeypatch.setattr(clip.subprocess, "Popen", popen)
    clip.spawn_detached()
    popen.assert_called_once()


def test_spawn_detached_keeps_matching_daemon(onsense_home, monkeypatch):
    _write_pair(onsense_home, "paired-token")
    monkeypatch.setattr(clip, "_daemon_alive", lambda: True)
    monkeypatch.setattr(clip, "_health", lambda: {
        "app": "onsense-clip",
        "version": clip.__version__,
        "token_fp": clip.token_fp(),
    })
    popen = Mock()
    monkeypatch.setattr(clip.subprocess, "Popen", popen)
    clip.spawn_detached()
    popen.assert_not_called()


def test_spawn_detached_never_downgrades_newer_daemon(onsense_home, monkeypatch):
    _write_pair(onsense_home, "paired-token")
    monkeypatch.setattr(clip, "_daemon_alive", lambda: True)
    monkeypatch.setattr(clip, "_health", lambda: {
        "app": "onsense-clip",
        "version": "99.0.0",
        "token_fp": clip.token_fp(),
    })
    popen = Mock()
    monkeypatch.setattr(clip.subprocess, "Popen", popen)
    clip.spawn_detached()
    popen.assert_not_called()


def test_clip_main_never_downgrades_newer_daemon(
        onsense_home, monkeypatch, capsys):
    _write_pair(onsense_home, "paired-token")

    def address_in_use(*_args, **_kwargs):
        raise OSError(errno.EADDRINUSE, "in use")

    monkeypatch.setattr(clip.http.server, "ThreadingHTTPServer", address_in_use)
    monkeypatch.setattr(clip, "_health", lambda _port: {
        "app": "onsense-clip",
        "version": "99.0.0",
        "token_fp": clip.token_fp(),
    })
    shutdown = Mock()
    kill = Mock()
    monkeypatch.setattr(clip, "_request_shutdown", shutdown)
    monkeypatch.setattr(clip, "_kill_port_owner", kill)
    assert clip.main(port=18770) == 0
    shutdown.assert_not_called()
    kill.assert_not_called()
    assert "will not downgrade" in capsys.readouterr().out


def test_doctor_fails_same_version_wrong_token(onsense_home, monkeypatch, capsys):
    _write_pair(onsense_home, "paired-token")
    monkeypatch.setattr(clip, "_health", lambda: {
        "app": "onsense-clip",
        "version": clip.__version__,
        "token_fp": "wrong-fp",
    })
    report = doctor.Report()
    doctor.check_clip_daemon(report)
    assert report.failed
    assert "token mismatch" in capsys.readouterr().out


def test_health_requires_valid_signature_for_authenticated_proof(onsense_home):
    _write_pair(onsense_home, "paired-token")
    httpd = clip.http.server.ThreadingHTTPServer(("127.0.0.1", 0), clip.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url) as response:
            assert json.load(response)["authenticated"] is False

        request = urllib.request.Request(
            url, headers=auth.sign("paired-token", "GET", "/health"))
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["authenticated"] is True

        bad = urllib.request.Request(
            url, headers=auth.sign("wrong-token", "GET", "/health"))
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad)
        assert exc.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
