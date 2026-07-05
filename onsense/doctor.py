"""onsense doctor — diagnose install/connection problems in one pass.

Checks: Python · uv/uvx · dependencies (mcp/httpx/cryptography/zeroconf) · Claude CLI and onsense registration ·
      mDNS phone discovery · phone HTTP reachability (/shot.jpg, token) · (Windows) firewall guidance.
Reports each item as ✅/⚠️/❌ + next action. Exit code 1 if any ❌ is present.
"""
import os
import platform
import shutil
import subprocess
import sys

from . import __version__, PAIR_PORT, PHONE_PORT, auth

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


class Report:
    def __init__(self):
        self.failed = False

    def line(self, mark, title, detail="", hint=""):
        msg = f"{mark} {title}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        if hint:
            print(f"     → {hint}")
        if mark == FAIL:
            self.failed = True


def _which(name):
    return shutil.which(name)


def _try_import(mod):
    try:
        m = __import__(mod)
        return getattr(m, "__version__", "?")
    except Exception:
        return None


def check_python(r: Report):
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        r.line(OK, "Python", ver)
    else:
        r.line(FAIL, "Python", ver, "Python 3.10 or newer is required.")


def check_uv(r: Report):
    # uvx is 'recommended', not 'required' — without it, pair auto-registers with the current Python
    # (python -m onsense) instead (pip/pipx install path). So a missing uvx is a WARN, not a FAIL.
    if platform.system() == "Windows":
        install = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    else:
        install = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    for name in ("uv", "uvx"):
        p = _which(name)
        if p:
            r.line(OK, name, p)
        else:
            r.line(WARN, name, "not found",
                   f"recommended install: {install}  | or without uvx: pipx install onsense (or pip install onsense) then 'onsense pair'")


def check_deps(r: Report):
    for mod, hard in (("mcp", True), ("httpx", True), ("cryptography", True),
                      ("zeroconf", False), ("qrcode", False)):
        ver = _try_import(mod)
        if ver:
            r.line(OK, f"dependency {mod}", ver)
        elif hard:
            r.line(FAIL, f"dependency {mod}", "not installed", "It is installed automatically when run via uvx.")
        else:
            r.line(WARN, f"dependency {mod}", "not installed (optional)",
                   "zeroconf=auto-recovery on IP change, qrcode=pairing QR. Installed automatically when using uvx.")


def check_claude(r: Report):
    cli = _which("claude")
    if not cli:
        r.line(WARN, "Claude CLI", "not found",
               "Installing Claude Code enables auto-registration (pair) of onsense. The server itself works without it.")
        return
    r.line(OK, "Claude CLI", cli)
    try:
        v = subprocess.run([cli, "mcp", "get", "onsense"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
        out = (v.stdout or "") + (v.stderr or "")
        if v.returncode == 0 and "onsense" in out:
            r.line(OK, "onsense MCP registration", "registered")
        else:
            r.line(WARN, "onsense MCP registration", "not registered",
                   "Pairing with the phone via uvx onsense pair registers it automatically.")
    except Exception as e:
        r.line(WARN, "onsense MCP registration", f"check failed: {e}")


def check_slash_command(r: Report):
    """Whether the /onsense slash-command file (~/.claude/commands/onsense.md) is installed."""
    path = os.path.join(os.path.expanduser("~"), ".claude", "commands", "onsense.md")
    if os.path.exists(path):
        r.line(OK, "/onsense slash command", path)
    else:
        r.line(WARN, "/onsense slash command", "not installed",
               "Running uvx onsense pair again installs it automatically (MCP registration alone doesn't create the slash command).")


def discover(r: Report):
    """Discover the phone via mDNS. Returns a list of reachable bases."""
    try:
        from .server import discover_bases
    except Exception as e:
        r.line(WARN, "mDNS discovery", f"unavailable: {e}")
        return []
    bases = discover_bases(timeout=4.0)
    if bases:
        r.line(OK, "mDNS phone discovery", ", ".join(bases))
    else:
        r.line(WARN, "mDNS phone discovery", "not found",
               "Check that the phone onSense app is [Started]/sharing and on the same Wi-Fi. "
               "(Some routers block mDNS — you can specify --base directly)")
    return bases


def check_reach(r: Report, bases, base_arg, token):
    targets = []
    if base_arg:
        targets.append(base_arg)
    targets += [b for b in bases if b not in targets]
    if not targets:
        r.line(WARN, "phone HTTP reach", "no target address",
               f"You can retry by specifying --base http://<phone-IP>:{PHONE_PORT} directly.")
        return
    try:
        import httpx
    except Exception:
        r.line(WARN, "phone HTTP reach", "skipped (httpx not installed)")
        return
    for base in targets:
        url = f"{base}/shot.jpg"
        headers = auth.sign(token, "GET", "/shot.jpg") if token else {}  # A fresh signature for each base
        try:
            resp = httpx.get(url, headers=headers,
                             timeout=httpx.Timeout(5.0, connect=2.0))
            if resp.status_code == 200 and resp.content:
                r.line(OK, "phone HTTP reach", f"{base} (200, {len(resp.content)} bytes)")
                _report_phone_version(r, base)
                return
            if resp.status_code in (401, 403):
                r.line(FAIL, "phone HTTP reach", f"{base} ({resp.status_code} auth failure)",
                       "Token/signature mismatch. Check the --token value, and if you just re-paired, "
                       "an already-running Claude MCP process may still be holding the old PHONE_TOKEN, so "
                       "restart Claude or kill the onsense serve process and retry.")
                return
            r.line(WARN, "phone HTTP reach", f"{base} (HTTP {resp.status_code})")
        except Exception as e:
            r.line(WARN, "phone HTTP reach", f"{base} failed: {type(e).__name__}")
    r.line(FAIL, "phone HTTP reach", "all addresses failed",
           "Check the phone's [Started] state, same Wi-Fi, and firewall, then retry.")


def _report_phone_version(r: Report, base: str):
    """Also report the phone app version (/version, no token needed) — shown alongside the mcp version to avoid debugging confusion."""
    try:
        import httpx
        resp = httpx.get(f"{base}/version", timeout=httpx.Timeout(3.0, connect=2.0))
        if resp.status_code == 200:
            j = resp.json()
            r.line(OK, "phone app version",
                   f"onSense {j.get('versionName', '?')} (code {j.get('versionCode', '?')})")
    except Exception:
        pass  # Older apps don't support /version — ignore


def check_clip_daemon(r: Report):
    """Check clip daemon (8770) presence and version match — surfaces a stale daemon (e.g. from the uv cache) squatting."""
    from . import clip
    info = clip._health()
    if info is None:
        r.line(WARN, "clip daemon", "not running (8770)",
               "Run `onsense clip` (or it auto-starts when serve starts) to make the phone↔PC clipboard work.")
        return
    if info.get("app") != "onsense-clip":
        r.line(FAIL, "clip daemon", "port 8770 is held by another process",
               "Kill the process holding the port, then run `onsense clip`.")
        return
    ver = info.get("version") or "old version (0.1.x)"
    if info.get("version") == __version__:
        r.line(OK, "clip daemon", f"v{ver} · pull={'ON' if info.get('allow_pull') else 'OFF'} · "
               f"remote_settings={'ON' if info.get('allow_remote_settings') else 'OFF'}")
    else:
        r.line(WARN, "clip daemon", f"version mismatch: daemon {ver} ≠ installed v{__version__}",
               "Running `onsense clip` auto-replaces it with the same version (version-aware singleton).")


def check_firewall(r: Report):
    if platform.system() != "Windows":
        return
    r.line(WARN, "Windows firewall", f"pairing port {PAIR_PORT} inbound required",
           "On the first pair run, click 'Allow private network' in the firewall prompt. "
           f"If blocked: netsh advfirewall firewall add rule name=onsense-pair "
           f"dir=in action=allow protocol=TCP localport={PAIR_PORT}")


def main(args) -> int:
    print(f"=== onsense doctor (mcp v{__version__}) ===\n")
    r = Report()
    check_python(r)
    check_uv(r)
    check_deps(r)
    check_claude(r)
    check_slash_command(r)
    print()
    bases = discover(r)
    token = getattr(args, "token", None) or os.environ.get("PHONE_TOKEN")
    base_arg = getattr(args, "base", None) or os.environ.get("PHONE_BASE")
    # After a normal pairing, read pair.json as a fallback so diagnostics work without --token/PHONE_TOKEN
    # (the same source of truth as serve). If absent, run unauthenticated diagnostics as before.
    if not token or not base_arg:
        try:
            import json
            from . import clip
            with open(clip.config_path(), encoding="utf-8") as f:
                pj = json.load(f) or {}
            token = token or pj.get("token")
            base_arg = base_arg or pj.get("base")
        except Exception:
            pass
    check_reach(r, bases, base_arg, token)
    check_clip_daemon(r)
    check_firewall(r)
    print()
    if r.failed:
        print(f"{FAIL} Some checks failed — follow the → guidance above and run again.")
        return 1
    print(f"{OK} Core checks passed. (⚠️ items can be ignored depending on your situation)")
    return 0
