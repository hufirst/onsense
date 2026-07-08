"""onSense pairing — connect to the phone and register the PC-side MCP server with the AI client (claude) in one step.

v2 (default, recommended — no camera needed on the PC):
  uvx onsense pair
    → The PC shows a QR of its receive address in the terminal and waits as a listener.
    → In phone onSense, tap 'Scan PC QR' → the phone POSTs {base, token} to the PC → auto-registration.

v1 (phone shows the QR → PC reads it, when the PC has a camera/screenshot):
  uvx onsense pair "onsense://pair?base=...&token=..."
  uvx onsense pair --img screenshot.png      # Decode the QR from a screenshot (requires opencv)

MCP server launch command (called by the AI client every session):
  default            uvx onsense serve                 (after PyPI publish)
  ONSENSE_FROM=path  uvx --from <path> onsense serve   (pre-publish local/git source)
  --local            python -m onsense serve           (development, current interpreter)
"""
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import PAIR_PORT

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def parse_uri(uri: str):
    q = parse_qs(urlparse(uri).query)
    return unquote(q.get("base", [""])[0]), unquote(q.get("token", [""])[0])


def from_img(path: str) -> str:
    import cv2
    data, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imread(path))
    return data


def _run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def serve_command(local: bool = False) -> list:
    """Command the AI client runs to launch the MCP server. uvx is preferred for machine reproducibility.

    If uvx is not installed (or installed but not found on PATH), fall back automatically to the current
    interpreter (python -m onsense). In that case onsense must be installed in that Python environment for
    serve to start each session (pip/pipx install path). This keeps registration working even without uvx.
    """
    if local:
        return [sys.executable, "-m", "onsense", "serve"]
    src = os.environ.get("ONSENSE_FROM")
    if src:  # Pre-publish: run uvx from a local path/git URL source
        return ["uvx", "--from", src, "onsense", "serve"]
    if shutil.which("uvx"):
        return ["uvx", "onsense", "serve"]  # One-liner after PyPI publish (recommended)
    # uvx not installed → fall back to the current Python (warning)
    print("⚠️  uvx not found — registering the MCP server with the current Python instead.")
    print("     Installing uv is recommended for reproducibility: https://docs.astral.sh/uv/")
    return [sys.executable, "-m", "onsense", "serve"]


def claude_commands_dir() -> str:
    """Claude Code user slash-command directory (~/.claude/commands)."""
    return os.path.join(os.path.expanduser("~"), ".claude", "commands")


def install_slash_command(client: str = "claude") -> None:
    """Install the /onsense slash command (.md) into the client's commands directory.

    If pair only registers the MCP server, the mcp__onsense__* tools appear but the `/onsense` slash command
    is a separate file that a fresh machine lacks → "unknown command". Install the template bundled in the
    package here. Create if missing, update if the content differs, leave it as-is if identical (idempotent).
    """
    if client != "claude":
        return  # Slash-command installation is currently supported for claude only (other CLIs get MCP registration only)
    try:
        from importlib.resources import files
        body = files("onsense").joinpath("commands/onsense.md").read_text(encoding="utf-8")
    except Exception as e:
        print("[pair] /onsense command template not found — skipping install:", e)
        return
    dst_dir = claude_commands_dir()
    dst = os.path.join(dst_dir, "onsense.md")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        old = ""
        if os.path.exists(dst):
            with open(dst, encoding="utf-8") as f:
                old = f.read()
        if old == body:
            print(f"✅ /onsense slash command — already up to date ({dst})")
            return
        with open(dst, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"✅ /onsense slash command {'updated' if old else 'installed'}: {dst}")
    except Exception as e:
        print("[pair] /onsense command install failed:", e)


def is_registered(cli: str) -> bool:
    """Whether the onsense MCP server is already registered (at user scope). `mcp get` exits 0 when registered."""
    return _run([cli, "mcp", "get", "onsense"]).returncode == 0


def register(base: str, token: str, local: bool = False, client: str = "claude"):
    cli = shutil.which(client) or client

    # pair.json = single source of truth. The serve/clip daemons read it LIVE at request time
    # (server._current_token/_current_base prefer pair.json → a running session auto-follows re-pairing).
    # So updating the token/address needs no MCP re-registration.
    try:
        from . import clip
        clip.save_pair(base, token)
    except Exception as e:
        print("[pair] skipping clip token save:", e)

    if is_registered(cli):
        # Re-pairing: don't touch the MCP registration. remove/add would drop a running session's stdio
        # connection and force a restart (a relic of the old env-passing approach). Updating pair.json alone applies automatically.
        print(f"applying received data → updating pair.json (base={base} token={token[:4]}****)")
        install_slash_command(client)
        print("\n✅ Re-pairing complete — running sessions also use the new token/address from the next tool call (no restart needed).")
        return

    # First-time registration only: register the serve command at user scope. The token is not baked into env
    # (avoids staleness — pair.json is authoritative). PHONE_BASE is passed only as a first-connection hint (pair.json/mDNS refresh it afterward).
    cmd = serve_command(local)
    r = _run([cli, "mcp", "add", "onsense", "--scope", "user",
              "-e", f"PHONE_BASE={base}",
              "--", *cmd])
    print((r.stdout or r.stderr or "").strip())
    print(f"registration command: {' '.join(cmd)}")
    v = _run([cli, "mcp", "get", "onsense"])
    print((v.stdout or "").strip())
    # Install the /onsense slash command (MCP registration alone doesn't create the slash command)
    install_slash_command(client)
    print("\n✅ Registration complete. Restart the AI client (claude) once to load the onsense tools and the /onsense command.")


def pc_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _lan_ok(addr: str) -> bool:
    """Allow only loopback/private (RFC1918)/link-local — confines pairing to the LAN."""
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


def _valid_base(b: str) -> bool:
    """Validate that the phone-sent base is http + a private-IP host (blocks SSRF/external-address injection)."""
    try:
        u = urlparse(b or "")
    except ValueError:
        return False
    return u.scheme == "http" and bool(u.hostname) and _lan_ok(u.hostname)


def _bind_pair_server(ip: str, port: int, handler, tries: int = 10):
    """Bind the QR-pairing HTTPServer, auto-advancing past busy ports.

    A fixed port means any other program already squatting on it (a stale prior `pair` run, an
    unrelated local service, etc.) turns into a raw traceback for the user. Auto-fallback is safe
    here because the QR/URL is generated *after* the bind succeeds, so the phone always gets the
    port that's actually listening.

    Retries on ANY OSError rather than filtering by errno: the "port busy" error code isn't
    portable (Linux raises EADDRINUSE/98, Windows raises WSAEADDRINUSE/10048 *or* WSAEACCES/10013
    when the occupant holds the port with SO_EXCLUSIVEADDRUSE — verified against a .NET
    TcpListener blocker on Windows 11, which triggered the 10013 case). If a candidate fails for
    an unrelated reason, every other candidate fails the same way and the loop still ends in the
    friendly error below instead of a raw traceback.
    """
    last_err = None
    for candidate in range(port, port + tries):
        try:
            return HTTPServer((ip, candidate), handler), candidate
        except OSError as e:
            last_err = e
    raise OSError(
        f"Could not find a free port for pairing after trying {port}-{port + tries - 1}. "
        f"Last error: {last_err}. Pass --port to pick a different range, or free up a port "
        "(check with `ss -ltnp` / `lsof -i :<port>` for what's using it)."
    )


def serve_and_register(local: bool = False, client: str = "claude", port: int = PAIR_PORT,
                       timeout_s: int = 300):
    ip = pc_lan_ip()
    # One-time secret for channel binding — delivered to the phone only via the QR (face-to-face out-of-band channel).
    # The phone POSTs with the full scanned URL (query included), so the listener verifies s → blocking a
    # preemptive injection (registering a fake base/token) by a LAN attacker who never saw the QR.
    pair_secret = secrets.token_urlsafe(16)

    received = {}

    class H(BaseHTTPRequestHandler):
        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            # ① private-network origin only  ② channel-binding secret matches  ③ base validation  ④ body size limit
            if not _lan_ok(self.client_address[0]):
                self._reply(403, b'{"error":"private network only"}')
                return
            got = parse_qs(urlparse(self.path).query).get("s", [""])[0]
            if not secrets.compare_digest(got, pair_secret):
                self._reply(401, b'{"error":"bad pairing secret"}')
                return
            ln = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            try:
                d = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
            except Exception:
                d = {}
            base, token = d.get("base"), d.get("token")
            if not _valid_base(base) or not token:
                self._reply(400, b'{"error":"invalid base/token"}')
                return
            received["base"], received["token"] = base, token
            self._reply(200, b'{"ok":true}')

        def do_GET(self):
            if not _lan_ok(self.client_address[0]):
                self._reply(403, b'{"error":"private network only"}')
                return
            self._reply(200, b'{"app":"onsense-pair"}')

        def log_message(self, *a):
            pass

    # Bind to the LAN IP only instead of 0.0.0.0 (reduce exposure). The pairing window itself has a timeout.
    try:
        httpd, bound_port = _bind_pair_server(ip, port, H)
    except OSError as e:
        print(f"\nPairing failed: {e}")
        return
    if bound_port != port:
        print(f"[pair] port {port} was busy — using {bound_port} instead.")

    url = f"http://{ip}:{bound_port}/pair?s={pair_secret}"
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("[qrcode not installed — enter the address below into the phone directly]")
    print("\nStep 1 — install the onSense app on your phone (skip if already installed):")
    print("        https://play.google.com/store/apps/details?id=com.shdev.onsense")
    print(f"Step 2 — open it, tap [Scan PC QR], and scan the QR above.  Waiting...  ({url})")
    print("(If the firewall prompts or blocks, allow inbound port "
          f"{bound_port} on this PC's private network.)")

    httpd.timeout = 1.0
    deadline = time.monotonic() + timeout_s
    hinted = False
    while not received.get("base"):
        now = time.monotonic()
        if now > deadline:
            httpd.server_close()
            print(f"\nPairing timed out ({timeout_s}s). Make sure the onSense app is installed and "
                  "the phone is on the same Wi-Fi, then run `uvx onsense pair` again.")
            return
        if not hinted and now > deadline - timeout_s / 2:  # halfway reminder, listener stays live
            print("...still waiting. Install the onSense app and tap [Scan PC QR] if you haven't yet.")
            hinted = True
        httpd.handle_request()
    httpd.server_close()
    print(f"\nReceived: base={received['base']} token={received['token'][:4]}****")
    register(received["base"], received["token"], local=local, client=client)


def main(args) -> int:
    local = getattr(args, "local", False)
    client = getattr(args, "client", "claude")
    uri = None
    if getattr(args, "img", None):
        uri = from_img(args.img)
    elif getattr(args, "uri", None):
        uri = args.uri
    if uri:
        base, token = parse_uri(uri)
        if not base or not token:
            print("Pairing failed: could not parse base/token ->", uri)
            return 2
        print(f"pairing: base={base}  token={token[:4]}****")
        register(base, token, local=local, client=client)
        return 0
    serve_and_register(local=local, client=client, port=getattr(args, "port", None) or PAIR_PORT)
    return 0
