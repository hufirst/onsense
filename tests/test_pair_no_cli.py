"""Regression test for `onsense pair` in environments without the AI-client CLI —
missing `claude` (Codex-only or plain-MCP setups) must print guidance, not crash
(2026-07-14: register() raised FileNotFoundError via subprocess when the CLI was absent)."""
import pytest

from onsense import pair


def test_register_without_cli_prints_guidance_and_returns(monkeypatch, capsys):
    real_which = pair.shutil.which
    monkeypatch.setattr(pair.shutil, "which",
                        lambda name: None if name == "claude" else real_which(name))
    # Don't let the test touch the real ~/.onsense/pair.json
    import onsense.clip as clip
    monkeypatch.setattr(clip, "save_pair", lambda base, token: None)

    pair.register("http://192.0.2.1:8080", "testtoken1234", client="claude")

    out = capsys.readouterr().out
    assert "CLI not found" in out
    assert "codex mcp add onsense" in out
    assert "onsense serve" in out  # uvx or python fallback, depending on the machine


def test_register_without_cli_never_invokes_subprocess(monkeypatch):
    real_which = pair.shutil.which
    monkeypatch.setattr(pair.shutil, "which",
                        lambda name: None if name == "claude" else real_which(name))
    import onsense.clip as clip
    monkeypatch.setattr(clip, "save_pair", lambda base, token: None)

    def _boom(args):  # pragma: no cover - fails the test if reached
        raise AssertionError(f"_run must not be called without a CLI: {args}")

    monkeypatch.setattr(pair, "_run", _boom)
    pair.register("http://192.0.2.1:8080", "testtoken1234", client="claude")
