"""onSense clip daemon (PC-side, cross-OS) — a phone ↔ PC clipboard/file bridge.

Endpoints (all X-Token gated):
  POST /clip   Phone → PC.  Body = file bytes.  Headers: Content-Type, (optional) X-Filename
                 image/*  → save file. Load the OS clipboard image once only when --set-clipboard
                 text/*   → save file. Load the OS clipboard text once only when --set-clipboard
                 other    → save file + load a file reference into the OS clipboard once only when --set-clipboard
                            (pasteable with Ctrl/Cmd+V in Windows Explorer/macOS Finder. best-effort on Linux)
  GET  /clip   PC → Phone (bidirectional). The phone pulls the current PC clipboard only when --allow-pull.
                 200 text/plain if text is present, 200 image/png if an image is present, 204 otherwise
  GET  /        Health check (no auth) — for doctor/connection checks

Design principles (kept identical to the spike):
  - No side effects: set the clipboard exactly once (don't hold it). Don't touch OS settings or other apps.
  - best-effort: with clipboard-sync apps in the mix, the file path (①) is the guaranteed fallback.
  - Standard library only (no external dependencies).

Port 8770 (assumes a LAN). Standalone run: `onsense clip`  /  serve auto-starts it as a singleton.
"""
import errno
import http.server
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time

from . import CLIP_PORT, __version__, auth, crypto

OSNAME = platform.system()  # 'Windows' | 'Darwin' | 'Linux'

# Windows: 콘솔 없는 데몬이 powershell 같은 콘솔 프로그램을 띄우면 빈 콘솔 창이
# 화면에 나타난다 — CREATE_NO_WINDOW 로 억제한다.
_NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if OSNAME == "Windows" else {}


# ── Settings/token store (written by pair, read by the daemon) ───────────────
def _home() -> str:
    return os.environ.get("ONSENSE_HOME") or os.path.join(os.path.expanduser("~"), ".onsense")


def config_path() -> str:
    return os.path.join(_home(), "pair.json")


def settings_path() -> str:
    return os.path.join(_home(), "settings.json")


def _truthy(v: str) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def load_flags() -> dict:
    """Read the ALLOW_PULL / SET_CLIPBOARD / ALLOW_REMOTE_SETTINGS persistent flags LIVE at request time.

    Priority: environment variable (truthy forces True) > settings.json > default False.
    allow_remote_settings = whether the PC owner approved once so the phone app settings can remotely
    change allow_pull (POST /settings) (consent stays on the PC).
    """
    flags = {"allow_pull": False, "set_clipboard": False, "allow_remote_settings": False}
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
        for k in flags:
            flags[k] = bool(data.get(k, False))
    except Exception:
        pass
    if _truthy(os.environ.get("ONSENSE_CLIP_ALLOW_PULL", "")):
        flags["allow_pull"] = True
    if _truthy(os.environ.get("ONSENSE_CLIP_SET_CLIPBOARD", "")):
        flags["set_clipboard"] = True
    if _truthy(os.environ.get("ONSENSE_CLIP_ALLOW_REMOTE_SETTINGS", "")):
        flags["allow_remote_settings"] = True
    return flags


def save_flags(allow_pull=None, set_clipboard=None, allow_remote_settings=None) -> dict:
    """Update settings.json atomically (write tmp + os.replace), chmod 600.

    Only non-None keys are updated. Returns the persistent state (dict) after the update.
    """
    d = _home()
    os.makedirs(d, exist_ok=True)
    p = settings_path()
    data = {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    if allow_pull is not None:
        data["allow_pull"] = bool(allow_pull)
    if set_clipboard is not None:
        data["set_clipboard"] = bool(set_clipboard)
    if allow_remote_settings is not None:
        data["allow_remote_settings"] = bool(allow_remote_settings)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return {"allow_pull": bool(data.get("allow_pull", False)),
            "set_clipboard": bool(data.get("set_clipboard", False)),
            "allow_remote_settings": bool(data.get("allow_remote_settings", False))}


def save_pair(base: str, token: str) -> None:
    """Called on successful pairing — store in a dedicated settings file so the daemon can read the token."""
    d = _home()
    os.makedirs(d, exist_ok=True)
    p = config_path()
    data = {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data.update({"base": base, "token": token})
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_token() -> str:
    tok = os.environ.get("ONSENSE_TOKEN") or os.environ.get("PHONE_TOKEN")
    if tok:
        return tok
    try:
        with open(config_path(), encoding="utf-8") as f:
            return (json.load(f) or {}).get("token") or ""
    except Exception:
        return ""


# ── Save paths ───────────────────────────────────────────────────────────────
SAVE_DIR = os.environ.get("ONSENSE_CLIP_DIR") or os.path.join(tempfile.gettempdir(), "onsense")
LATEST_IMAGE = os.path.join(SAVE_DIR, "latest.jpg")
# Receive size limit (protects memory/disk). 0 or less means unlimited. Adjustable via environment variable.
MAX_MB = float(os.environ.get("ONSENSE_CLIP_MAX_MB", "200"))
MAX_BYTES = int(MAX_MB * 1024 * 1024) if MAX_MB > 0 else 0
_CHUNK = 65536
_NONCE_CACHE = auth.NonceCache()  # replay prevention
# Replace only path separators, control characters, and shell-dangerous characters — preserve Unicode filenames (e.g. non-ASCII)
_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\s]+')


def _private_client(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return False


def _safe_name(name: str, fallback: str) -> str:
    name = os.path.basename(name or "").strip()
    name = _SAFE.sub("_", name)
    return name or fallback


# ── Clipboard set (phone → PC) ───────────────────────────────────────────────
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Upper bound xclip can safely serve on X11. Measured (GNOME/mutter X11): 1,000,000B OK;
# from 1MiB (1,048,576B) the selection service wedges and all paste requests hang.
_X11_SAFE_MAX = 900_000


def _to_png(data: bytes, scale: float = 1.0) -> bytes:
    """Convert JPEG etc. to PNG (best-effort, using an external converter). Returns b'' on failure.

    Many Linux apps (GTK/Qt families) request only image/png when pasting, so loading as PNG when
    possible is what makes Ctrl+V actually work.
    scale < 1.0 downsizes and re-encodes (to avoid the xclip size limit).
    """
    if scale >= 1.0 and data[:8] == _PNG_MAGIC:
        return data
    pct = f"{int(scale * 100)}%"
    vf = ["-vf", f"scale=iw*{scale}:-2"] if scale < 1.0 else []
    for cmd in (["magick", "-", "-resize", pct, "png:-"] if scale < 1.0 else ["magick", "-", "png:-"],
                ["convert", "-", "-resize", pct, "png:-"] if scale < 1.0 else ["convert", "-", "png:-"],
                ["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1", *vf,
                 "-f", "image2pipe", "-c:v", "png", "pipe:1"]):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=15)
            if r.returncode == 0 and r.stdout[:8] == _PNG_MAGIC:
                return r.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return b""


# GTK holder: the only safe large-payload path for the X11 image clipboard.
#  - The GNOME X11 clipboard manager does not persist the image store (measured) → something must stay
#    alive to serve the selection for pasting to work.
#  - xclip's INCR transfer wedges while serving data over ~1MiB (measured) → GTK serves it instead.
#  - The holder exits by itself when it loses ownership (another app copies / a new holder starts) → at most one alive.
_GTK_HOLDER = r"""
import sys
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
data = sys.stdin.buffer.read()
loader = GdkPixbuf.PixbufLoader()
loader.write(data)
loader.close()
cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
cb.set_image(loader.get_pixbuf())
while Gtk.events_pending():
    Gtk.main_iteration()
sys.stdout.write("OK\n")
sys.stdout.flush()
sys.stdout.close()
CLIP = Gdk.Atom.intern("CLIPBOARD", False)
own = Gdk.selection_owner_get(CLIP)  # We just set it, so this is our window
own_xid = own.get_xid() if own else 0
def check():
    # Note: GDK wraps an external process's owning window as a foreign window too, so a None
    # comparison cannot detect loss of ownership → an XID comparison is needed.
    try:
        o = Gdk.selection_owner_get(CLIP)
        if o is None or o.get_xid() != own_xid:
            Gtk.main_quit()
    except Exception:
        Gtk.main_quit()  # In environments where this can't be determined (non-X11 etc.), exit rather than hold
    return True
GLib.timeout_add_seconds(2, check)
Gtk.main()
"""
_GI_PY = None      # Cached path to a Python with gi (GTK) available ('' = none)
_HOLDER = None     # Previous holder Popen (for zombie reaping)


def _gi_python() -> str:
    global _GI_PY
    if _GI_PY is None:
        import sys
        _GI_PY = ""
        for py in (sys.executable, "/usr/bin/python3"):
            try:
                if subprocess.run([py, "-c", "import gi; gi.require_version('Gtk','3.0')"],
                                  capture_output=True, timeout=10).returncode == 0:
                    _GI_PY = py
                    break
            except Exception:
                continue
    return _GI_PY


def _gtk_clip_image(data: bytes) -> bool:
    """Start the GTK holder process detached to load the image into the clipboard (True on success).

    pixbuf auto-detects JPEG/PNG and GTK offers multiple targets (image/png etc.), so no separate
    format conversion is needed. Holder liveness is confirmed via a stdout 'OK' handshake.
    """
    global _HOLDER
    py = _gi_python()
    if not py:
        return False
    if _HOLDER is not None:
        _HOLDER.poll()  # Reap the previous holder zombie (it exits by itself once the new holder takes ownership)
    try:
        p = subprocess.Popen([py, "-c", _GTK_HOLDER],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(data)
        p.stdin.close()
        import select
        r, _, _ = select.select([p.stdout], [], [], 15)
        line = p.stdout.readline() if r else b""
        if line.strip() == b"OK":
            _HOLDER = p
            return True
        p.kill()
        return False
    except Exception as e:
        print("[clip] GTK holder failed to start:", e)
        return False


def _linux_clip_set(data: bytes, mime: str = "") -> bool:
    """Load the clipboard via wl-copy/xclip (common Linux path).

    Note: both tools fork a child to serve the selection, and that child keeps holding the inherited
    stdout/stderr pipes, so capturing with capture_output waits for EOF → hangs until timeout.
    They must be run with DEVNULL.
    """
    wl = ["wl-copy"] + (["--type", mime] if mime else [])
    xc = ["xclip", "-selection", "clipboard", "-i"] + (["-t", mime] if mime else [])
    # Try the tool matching the session type first (the other is the fallback)
    cmds = [wl, xc] if os.environ.get("WAYLAND_DISPLAY") else [xc, wl]
    missing = 0
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            missing += 1
        except subprocess.TimeoutExpired:
            pass
    if missing == len(cmds):
        print("[clip] no clipboard tool — X11: `sudo apt install xclip`, "
              "Wayland: `sudo apt install wl-clipboard`")
    else:
        print("[clip] clipboard load failed — check the DISPLAY/WAYLAND_DISPLAY environment "
              f"(DISPLAY={os.environ.get('DISPLAY', '')!r}, "
              f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')!r})")
    return False


_PS_SET_IMG = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bytes = [System.IO.File]::ReadAllBytes('{path}')
$ms = New-Object System.IO.MemoryStream(,$bytes)
$img = [System.Drawing.Image]::FromStream($ms)
[System.Windows.Forms.Clipboard]::SetDataObject($img, $true)
$img.Dispose(); $ms.Dispose()
"""


def set_clipboard_image(path: str) -> bool:
    try:
        if OSNAME == "Windows":
            ps = _PS_SET_IMG.format(path=path.replace("'", "''"))
            r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=15, **_NOWIN)
            return r.returncode == 0
        if OSNAME == "Darwin":
            scr = f'set the clipboard to (read (POSIX file "{path}") as JPEG picture)'
            return subprocess.run(["osascript", "-e", scr], capture_output=True, timeout=15).returncode == 0
        with open(path, "rb") as f:
            data = f.read()
        if os.environ.get("WAYLAND_DISPLAY"):
            # Wayland: wl-copy is fd-based, so there's no size limit. Load as PNG (compatible with most apps).
            png = _to_png(data)
            if png and _linux_clip_set(png, "image/png"):
                return True
            return _gtk_clip_image(data)
        # X11: the GTK holder is the only safe large-payload path (xclip wedges over ~1MiB — see the constant's comment)
        if _gtk_clip_image(data):
            return True
        png = _to_png(data)
        if not png:
            print("[clip] no PNG converter (ImageMagick/ffmpeg) — loading as image/jpeg; some apps can't paste it")
            if len(data) <= _X11_SAFE_MAX:
                return _linux_clip_set(data, "image/jpeg")
            print(f"[clip] image {len(data)}B > {_X11_SAFE_MAX}B — skipping load to avoid an xclip wedge. "
                  "`sudo apt install python3-gi` recommended")
            return False
        for scale in (1.0, 0.7, 0.5, 0.35):
            p = png if scale >= 1.0 else _to_png(data, scale)
            if p and len(p) <= _X11_SAFE_MAX:
                if scale < 1.0:
                    print(f"[clip] loading downsized to {int(scale * 100)}% to stay under the xclip size limit "
                          "(for full quality, `sudo apt install python3-gi`)")
                return _linux_clip_set(p, "image/png")
        print(f"[clip] still over {_X11_SAFE_MAX}B after downsizing — skipping load. `sudo apt install python3-gi` recommended")
        return False
    except Exception as e:
        print("[clip] set image failed:", e)
        return False


_PS_SET_FILE = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Collections.Specialized.StringCollection
$f.Add('{path}')
[System.Windows.Forms.Clipboard]::SetFileDropList($f)
"""


def set_clipboard_file(path: str) -> bool:
    """Load a file reference (CF_HDROP etc.) into the PC clipboard — so it's pasteable with Ctrl/Cmd+V in Explorer/Finder.

    Binaries (PDF, video, etc.) can't be put on the clipboard as bytes, so only reference the already-saved
    file path in the OS's 'copied file' format (no side effects, once).
    """
    try:
        if OSNAME == "Windows":
            ps = _PS_SET_FILE.format(path=path.replace("'", "''"))
            r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=15, **_NOWIN)
            return r.returncode == 0
        if OSNAME == "Darwin":
            esc = path.replace("\\", "\\\\").replace('"', '\\"')
            scr = f'set the clipboard to (POSIX file "{esc}")'
            return subprocess.run(["osascript", "-e", scr], capture_output=True, timeout=15).returncode == 0
        from urllib.parse import quote
        uri = "file://" + quote(path)
        # text/uri-list (RFC 2483) is CRLF-terminated — using LF only breaks some implementations (Dolphin etc.).
        attempts = [
            (("copy\n" + uri).encode("utf-8"), "x-special/gnome-copied-files"),
            ((uri + "\r\n").encode("utf-8"), "text/uri-list"),
        ]
        # GNOME/Nautilus require gnome-copied-files first, while others (KDE/Dolphin etc.) use text/uri-list as
        # the standard — detect the desktop and try that one first (avoids a situation where a file manager that
        # doesn't understand the other format merely reports 'success' but nothing actually pastes).
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") or "").lower()
        if "gnome" not in desktop and "unity" not in desktop:
            attempts.reverse()
        for data, mime in attempts:
            if _linux_clip_set(data, mime):
                return True
        return False
    except Exception as e:
        print("[clip] set file failed:", e)
        return False


def set_clipboard_text(text: str) -> bool:
    try:
        data = text.encode("utf-8")
        if OSNAME == "Windows":
            # The PS 5.1 console's default codec is OEM (cp949 etc.) — stdin must be explicitly read as UTF-8
            # so non-ASCII text isn't corrupted (Console.In is initialized to InputEncoding on first access).
            ps = ("[Console]::InputEncoding=[System.Text.UTF8Encoding]::new();"
                  "$in=[Console]::In.ReadToEnd(); Set-Clipboard -Value $in")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               input=data, capture_output=True, timeout=15, **_NOWIN)
            return r.returncode == 0
        if OSNAME == "Darwin":
            return subprocess.run(["pbcopy"], input=data, capture_output=True, timeout=15).returncode == 0
        return _linux_clip_set(data)
    except Exception as e:
        print("[clip] set text failed:", e)
        return False


# ── Clipboard get (PC → phone, bidirectional) ────────────────────────────────
def get_clipboard_text() -> str:
    try:
        if OSNAME == "Windows":
            # stdout also defaults to the OEM codec — UTF-8 output must be set so non-ASCII text isn't mojibake
            ps = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
                  "Get-Clipboard -Raw")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=15, **_NOWIN)
            if r.returncode != 0:
                return ""
            out = r.stdout.decode("utf-8", "replace")
            # The PS console appends one trailing CRLF — a newline not in the original, so strip just one
            return out[:-2] if out.endswith("\r\n") else out
        if OSNAME == "Darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=15)
            return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
        for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                if r.returncode == 0:
                    return r.stdout.decode("utf-8", "replace")
            except FileNotFoundError:
                continue
        return ""
    except Exception:
        return ""


def get_clipboard_files() -> list:
    """List of local paths if the clipboard holds 'copied files', else []. (Ctrl+C in the file explorer)"""
    try:
        if OSNAME == "Windows":
            # Set UTF-8 output so non-ASCII paths aren't corrupted by the OEM codec and fail to read
            ps = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
                  "Add-Type -AssemblyName System.Windows.Forms;"
                  "$f=[System.Windows.Forms.Clipboard]::GetFileDropList();"
                  "if($f){$f -join \"`n\"}")
            r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=15, **_NOWIN)
            out = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
            return [ln.strip() for ln in out.splitlines() if ln.strip()]
        if OSNAME == "Darwin":
            r = subprocess.run(["osascript", "-e", 'POSIX path of (the clipboard as «class furl»)'],
                               capture_output=True, timeout=15)
            p = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
            return [p] if p and os.path.exists(p) else []
        # Linux: file managers provide it as text/uri-list or x-special/gnome-copied-files
        from urllib.parse import unquote, urlparse
        for tgt in ("x-special/gnome-copied-files", "text/uri-list"):
            for cmd in (["wl-paste", "--type", tgt],
                        ["xclip", "-selection", "clipboard", "-t", tgt, "-o"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=15)
                except FileNotFoundError:
                    continue
                if r.returncode != 0 or not r.stdout:
                    continue
                paths = []
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if line.startswith("file://"):
                        paths.append(unquote(urlparse(line).path))
                if paths:
                    return [p for p in paths if os.path.exists(p)]
        return []
    except Exception:
        return []


def get_clipboard_image() -> bytes:
    """The PC clipboard's image as PNG bytes (best-effort). Returns b'' if none."""
    try:
        if OSNAME == "Darwin":
            try:
                r = subprocess.run(["pngpaste", "-"], capture_output=True, timeout=15)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
            except FileNotFoundError:
                return b""
            return b""
        if OSNAME == "Windows":
            tmp = os.path.join(tempfile.gettempdir(), "onsense_pull.png")
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$i=[System.Windows.Forms.Clipboard]::GetImage();"
                  f"if($i){{$i.Save('{tmp}');'OK'}}else{{'NONE'}}")
            r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=15, **_NOWIN)
            if r.returncode == 0 and b"OK" in r.stdout and os.path.exists(tmp):
                with open(tmp, "rb") as f:
                    return f.read()
            return b""
        for cmd in (["wl-paste", "--type", "image/png"],
                    ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                # PNG magic check is required — an xclip-owned selection returns its stored content
                # verbatim for any requested target, so this prevents the silent failure of text being misclassified as an image.
                if r.returncode == 0 and r.stdout.startswith(_PNG_MAGIC):
                    return r.stdout
            except FileNotFoundError:
                continue
        return b""
    except Exception:
        return b""


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "onsense-clip"

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authed(self) -> bool:
        if not _private_client(self.client_address[0]):
            self._send(403, "private network only")
            return False
        token = load_token()
        if not token:
            self._send(503, "not paired: run onsense pair first")
            return False
        path = self.path.split("?", 1)[0]
        if auth.verify(token, self.command, path, lambda n: self.headers.get(n), _NONCE_CACHE):
            return True
        self._send(401, "unauthorized")
        return False

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path in ("/", "/health"):
            # Unauthenticated health — daemon presence + current flags (for the phone's situational-awareness probe). Flags are non-sensitive.
            fl = load_flags()
            body = json.dumps({"app": "onsense-clip", "os": OSNAME, "version": __version__,
                               "allow_pull": fl["allow_pull"], "set_clipboard": fl["set_clipboard"],
                               "allow_remote_settings": fl["allow_remote_settings"]})
            self._send(200, body, ctype="application/json")
            return
        if path == "/clip":
            if not self._authed():
                return
            if not load_flags()["allow_pull"]:
                self._send(403, "pull disabled: start onsense clip --allow-pull to enable")
                return
            # Encrypt the response body (resp_aad binds the request nonce). Single-shot GCM, so buffered.
            token = load_token()
            ak = crypto.aead_key(token)
            req_nonce = self.headers.get(auth.NONCE_HEADER, "")
            rpath = self.path.split("?", 1)[0]

            def _send_sealed(plain, ctype, fname=None, count=None):
                sealed = crypto.seal(ak, crypto.resp_aad(self.command, rpath, req_nonce), plain)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if fname is not None:
                    # Only ASCII is safe in HTTP headers — percent-encode non-ASCII filenames (UTF-8); the receiver decodes
                    from urllib.parse import quote
                    self.send_header("X-Filename", quote(fname))
                if count is not None:
                    self.send_header("X-File-Count", str(count))
                self.send_header("Content-Length", str(len(sealed)))
                self.end_headers()
                self.wfile.write(sealed)

            # Priority: copied files → image → text
            files = get_clipboard_files()
            if files:
                import mimetypes
                p = files[0]
                try:
                    sz = os.path.getsize(p)
                except OSError as e:
                    self._send(500, f"stat failed: {e}")
                    return
                # Same limit as POST — avoids reading a huge copied file whole and burning PC/phone memory
                if MAX_BYTES and sz > MAX_BYTES:
                    self._send(413, f"too large: {sz} bytes > limit {MAX_MB:g}MB (set ONSENSE_CLIP_MAX_MB)")
                    return
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError as e:
                    self._send(500, f"read failed: {e}")
                    return
                _send_sealed(data, mimetypes.guess_type(p)[0] or "application/octet-stream",
                             os.path.basename(p), len(files))
                return
            img = get_clipboard_image()
            if img:
                # Provide a filename hint so the receiver doesn't mistake the image for text (also guards older apps)
                _send_sealed(img, "image/png", "clipboard.png")
                return
            text = get_clipboard_text()
            if text:
                _send_sealed(text.encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(204)
            return
        self._send(404, "not found")

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/settings":
            self._post_settings()
            return
        if path == "/shutdown":
            # Version-aware singleton: used when a newer daemon on the same PC replaces an older one.
            # Loopback + HMAC signature required (token holder = owner) — cannot be shut down remotely (including by the phone).
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self._send(403, "loopback only")
                return
            if not self._authed():
                return
            self._send(200, "bye")
            print(f"[clip] /shutdown received — shutting down v{__version__} daemon (awaiting replacement).")
            import threading
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path != "/clip":
            self._send(404, "not found")
            return
        if not self._authed():
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            self._send(400, "empty body")
            return
        if MAX_BYTES and n > MAX_BYTES:
            self._send(413, f"too large: {n} bytes > limit {MAX_MB:g}MB (set ONSENSE_CLIP_MAX_MB)")
            return
        ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        from urllib.parse import unquote
        fname = unquote(self.headers.get("X-Filename", ""))   # Phone sends it percent-encoded (ASCII passes through)

        os.makedirs(SAVE_DIR, exist_ok=True)
        # A file explicitly chosen in the phone's 'file' mode is always treated as a file regardless of its
        # content (image/text) — turning it into a clipboard image/text would prevent pasting it as a file in the explorer.
        force_file = (self.headers.get("X-Ref-Kind", "") or "").strip().lower() == "file"
        if force_file:
            kind, saved = "file", os.path.join(SAVE_DIR, _safe_name(fname, "file.bin"))
        elif ctype.startswith("image/"):
            kind, saved = "image", LATEST_IMAGE
        elif ctype.startswith("text/"):
            kind, saved = "text", os.path.join(SAVE_DIR, _safe_name(fname, "clip.txt"))
        else:
            kind, saved = "file", os.path.join(SAVE_DIR, _safe_name(fname, "file.bin"))
        # Read the full ciphertext and decrypt (no plaintext consumed before tag verification). Single-shot GCM, so buffered.
        blob = self.rfile.read(n)
        rpath = self.path.split("?", 1)[0]
        aad = crypto.req_aad(self.command, rpath,
                             self.headers.get(auth.TS_HEADER, ""),
                             self.headers.get(auth.NONCE_HEADER, ""))
        try:
            data = crypto.open_(crypto.aead_key(load_token()), aad, blob)
        except Exception:
            self._send(400, "decrypt failed")  # Tampered/forged → reject
            return
        try:
            with open(saved, "wb") as f:
                f.write(data)
            written = len(data)
            # Make received files readable by the owner only (blocks other local users on the same PC). best-effort (harmless on Windows).
            try:
                os.chmod(SAVE_DIR, 0o700)
                os.chmod(saved, 0o600)
            except OSError:
                pass
        except OSError as e:
            print("[clip] save failed:", e)
            self._send(500, "save failed")
            return
        # Save done → respond first (so the clipboard subprocess doesn't exceed the phone's readTimeout).
        self._send(200, saved)
        # Clipboard loading is best-effort post-processing after the response (no side effects, once).
        clip_ok = False
        set_clip = load_flags()["set_clipboard"]
        if set_clip and kind == "image":
            clip_ok = set_clipboard_image(saved)
        elif set_clip and kind == "text":
            try:
                with open(saved, encoding="utf-8", errors="replace") as f:
                    clip_ok = set_clipboard_text(f.read())
            except OSError:
                pass
        elif set_clip and kind == "file":
            clip_ok = set_clipboard_file(saved)
        elif not set_clip:
            print("[clip] set_clipboard OFF — file saved only. To use Ctrl+V, enable it with `onsense clip --set-clipboard`")
        print(f"[clip] {self.client_address[0]} -> {written}B {kind} clip={'OK' if clip_ok else 'NO'} -> {saved}")

    def _post_settings(self):
        """Remotely change allow_pull from the phone (app settings) — only if the PC approved once with --allow-remote-settings.

        Body = AEAD-sealed JSON {"allow_pull": bool}. Signature/nonce verification is the same as /clip.
        The approval (allow_remote_settings) itself can be turned on only from the PC (preserving where consent lives).
        """
        if not self._authed():
            return
        if not load_flags()["allow_remote_settings"]:
            self._send(403, "remote settings disabled: run `onsense clip --allow-remote-settings` on the PC once")
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 4096:
            self._send(400, "bad body")
            return
        blob = self.rfile.read(n)
        aad = crypto.req_aad(self.command, self.path.split("?", 1)[0],
                             self.headers.get(auth.TS_HEADER, ""),
                             self.headers.get(auth.NONCE_HEADER, ""))
        try:
            req = json.loads(crypto.open_(crypto.aead_key(load_token()), aad, blob).decode("utf-8"))
        except Exception:
            self._send(400, "decrypt failed")
            return
        ap = req.get("allow_pull")
        if not isinstance(ap, bool):
            self._send(400, "allow_pull(bool) required")
            return
        state = save_flags(allow_pull=ap)
        print(f"[clip] remote settings (phone {self.client_address[0]}): pull={'ON' if state['allow_pull'] else 'OFF'}")
        self._send(200, json.dumps(state), ctype="application/json")

    def log_message(self, *a):
        pass


def main(port: int = CLIP_PORT, allow_pull: bool = False, set_clipboard: bool = False) -> int:
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)  # Flush logs immediately even when redirected to a file/pipe
    except Exception:
        pass
    # Flags are evaluated LIVE at request time from the settings.json persistent values + environment variables (option ①).
    # If the allow_pull/set_clipboard args are True, promote them to persistent state (False keeps the existing value).
    if allow_pull or set_clipboard:
        save_flags(allow_pull=True if allow_pull else None,
                   set_clipboard=True if set_clipboard else None)
    if not load_token():
        print("[clip] warning: token not set — /clip requests are rejected. Use it after `onsense pair`.")
    httpd = None
    for attempt in (1, 2):
        try:
            httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                raise
            # Version-aware singleton: yield only when the occupant is the same version of onsense-clip.
            # If it's a different version (including older), replace it — prevents a stale daemon (e.g. from the uv cache) from squatting.
            info = _health(port)
            theirs = (info or {}).get("version") or ""
            if info is None or info.get("app") != "onsense-clip":
                print(f"[clip] :{port} is held by a non-onsense process — aborting startup. "
                      f"Check what owns the port.")
                return 1
            if theirs == __version__:
                print(f"[clip] the same version (v{__version__}) is already running on :{port} — skipping extra startup (singleton).")
                return 0
            if attempt == 2:
                print(f"[clip] failed to replace the old daemon on :{port} — kill it manually and rerun "
                      f"(Linux: fuser -k {port}/tcp).")
                return 1
            print(f"[clip] a different-version (v{theirs or '?'}) daemon is on :{port} — replacing it with v{__version__}.")
            if not _request_shutdown(port):   # Signed shutdown request for newer daemons
                _kill_port_owner(port)        # Last resort for old daemons (no /shutdown)
            for _ in range(30):               # Wait up to 3s for shutdown
                if not _daemon_alive(port):
                    break
                time.sleep(0.1)
    if httpd is None:
        return 1
    flags = load_flags()
    print(f"[clip] onSense clip daemon on 0.0.0.0:{port} ({OSNAME}, save={SAVE_DIR}, "
          f"pull={'ON' if flags['allow_pull'] else 'OFF'}, "
          f"set_clipboard={'ON' if flags['set_clipboard'] else 'OFF'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _daemon_alive(port: int = CLIP_PORT) -> bool:
    """Check whether the clip daemon is already up via a fast TCP connection (prevents duplicate spawns)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _health(port: int = CLIP_PORT, timeout: float = 1.5) -> dict | None:
    """The local daemon's /health JSON. Returns None if unreachable/non-JSON (treated as not an onsense daemon)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _request_shutdown(port: int = CLIP_PORT) -> bool:
    """Request shutdown of the existing daemon via a signed POST /shutdown — succeeds only for daemons that support /shutdown (newer versions)."""
    try:
        import urllib.request
        token = load_token()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", data=b"",
                                     method="POST", headers=auth.sign(token, "POST", "/shutdown"))
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _kill_port_owner(port: int = CLIP_PORT) -> bool:
    """Last resort: kill the process holding the port — for replacing old versions that lack /shutdown.
    Use only after confirming via _health that it's onsense-clip (never kill an arbitrary process)."""
    try:
        if OSNAME == "Windows":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if f":{port}" in ln and "LISTENING" in ln.upper():
                    subprocess.run(["taskkill", "/PID", ln.split()[-1], "/F"],
                                   capture_output=True, timeout=5)
                    return True
            return False
        if shutil.which("fuser"):
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)
            return True
        if shutil.which("lsof"):
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5).stdout
            for pid in out.split():
                os.kill(int(pid), 15)
            return bool(out.strip())
    except Exception:
        pass
    return False


def spawn_detached() -> None:
    """Called by serve — ensures the daemon runs as a background singleton.

    Two-layer duplicate prevention: ① liveness check right before spawn (don't start at all if already up) →
    ② if a race still starts two at once, main()'s port-bind guard (EADDRINUSE) immediately exits the loser.
    Version-aware: even if one is alive, spawn a child if it's a different-version onsense-clip — the child's main() performs the replacement.
    """
    if _daemon_alive():
        info = _health()
        if not (info and info.get("app") == "onsense-clip"
                and info.get("version") != __version__):
            return  # Same version (or not onsense — don't touch it) — no extra startup
    try:
        import sys
        kwargs = {}
        if OSNAME == "Windows":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "onsense", "clip"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        import sys
        print("[clip] skipping auto-start:", e, file=sys.stderr)  # Don't pollute serve's stdout (MCP protocol)


if __name__ == "__main__":
    raise SystemExit(main())
