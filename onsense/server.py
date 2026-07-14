"""onSense MCP server (stdio) — turn your phone into the AI's eyes and sensors.

Environment variables:
  PHONE_BASE   e.g. http://192.168.0.9:8080  (initial address; auto-refreshed via mDNS on IP change)
  PHONE_TOKEN  Pairing token shown by the app (X-Token header)
  ONSENSE_TEST_FRAME  (optional) Path to a local test JPEG to return when the phone is unreachable

Robustness: on connection failure (IP change/drop), rediscover the current address via mDNS (_onsense._tcp) and retry.
Tools: get_live_frame / read_sensors / recent_photos / get_photo
"""
import os
import platform
import sys
import hashlib
import json
import signal
import subprocess
import time

from mcp.server.fastmcp import FastMCP, Image

from . import __version__, MDNS_TYPE, auth, crypto

mcp = FastMCP("onsense")
OSNAME = platform.system()  # 'Windows' | 'Darwin' | 'Linux'


def _truthy(v: str) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def _home() -> str:
    return os.environ.get("ONSENSE_HOME") or os.path.join(os.path.expanduser("~"), ".onsense")


def _pair_path() -> str:
    return os.path.join(_home(), "pair.json")


def _load_pair() -> dict:
    try:
        with open(_pair_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _init_phone_token() -> str:
    """Priority: environment variable > pair.json > empty"""
    env_token = os.environ.get("PHONE_TOKEN", "").strip()
    if env_token:
        return env_token
    pair = _load_pair()
    return pair.get("token", "")


PHONE_BASE = os.environ.get("PHONE_BASE")
PHONE_TOKEN = _init_phone_token()
TEST_FRAME = os.environ.get("ONSENSE_TEST_FRAME")

_base_cache = PHONE_BASE  # Last address that worked


def _current_base() -> str:
    return (_load_pair().get("base") or PHONE_BASE or "").strip()


def _current_token() -> str:
    # pair.json is read live so a long-running Claude MCP process follows re-pairing.
    return (_load_pair().get("token") or PHONE_TOKEN or "").strip()


def _token_diag(token: str) -> str:
    if not token:
        return "unset"
    token_fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    auth_fp = hashlib.sha256(crypto.auth_key(token)).hexdigest()[:12]
    return f"set len={len(token)} token_sha256={token_fp} auth_key_sha256={auth_fp}"


def _ancestor_pids() -> set[int]:
    pids = {os.getpid()}
    pid = os.getppid()
    while pid and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                stat = f.read()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return pids


def _is_serve_cmd(args: str) -> bool:
    return (
        "onsense serve" in args
        or "-m onsense serve" in args
        or "/bin/onsense serve" in args
    )


def _terminate_old_serve_processes() -> list[int]:
    """By default, leave duplicate serves alone (opt-in). Each stdio serve is 1:1 per client, and the
    clip daemon is already a singleton via port binding, so killing duplicates has little benefit. Meanwhile
    the former default, 'SIGTERM old serves', also killed other Claude sessions' serves and caused cross-session drops.
    → Clean up only when ONSENSE_SERVE_DEDUP is truthy."""
    if not _truthy(os.environ.get("ONSENSE_SERVE_DEDUP", "")):
        return []
    if _truthy(os.environ.get("ONSENSE_SERVE_ALLOW_DUPLICATES", "")):
        return []  # (backward compat) still honor explicit duplicate allowance
    try:
        current_user = subprocess.check_output(["id", "-un"], text=True).strip()
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,user=,args="],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return []

    protected = _ancestor_pids()
    targets: list[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        user, args = parts[1], parts[2]
        if user != current_user or pid in protected or not _is_serve_cmd(args):
            continue
        targets.append(pid)

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
    if targets:
        deadline = time.time() + 1.0
        while time.time() < deadline:
            alive = []
            for pid in targets:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if not alive:
                break
            time.sleep(0.05)
    return targets


def discover_bases(timeout: float = 1.5) -> list:
    """Return a list of all addresses (http://ip:port) of the _onsense._tcp service via mDNS.
    ⚠️ Synchronous zeroconf skips blocking I/O inside a running asyncio loop (MCP tools run inside the loop).
    → Run it in a separate thread without a loop. Returns multiple entries if the phone has multiple interfaces."""
    import concurrent.futures

    def _work():
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            return []
        import socket
        import time
        found = {"bases": []}

        class _L:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name, timeout=2000)
                if info:
                    for a in info.addresses:
                        found["bases"].append(f"http://{socket.inet_ntoa(a)}:{info.port}")

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        zc = Zeroconf()
        try:
            ServiceBrowser(zc, MDNS_TYPE, _L())
            t0 = time.time()
            while not found["bases"] and time.time() - t0 < timeout:
                time.sleep(0.2)
        finally:
            zc.close()
        return found["bases"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        try:
            return ex.submit(_work).result(timeout=timeout + 3)
        except Exception:
            return []


def _http(base: str, path: str, want_headers: bool = False):
    import httpx
    # HMAC signature headers — sign over the canonical path (with sorted query) (matches the phone's verification, blocks parameter tampering)
    token = _current_token()
    headers = auth.sign(token, "GET", path) if token else {}
    r = httpx.get(f"{base}{path}", headers=headers,
                  timeout=httpx.Timeout(5.0, connect=2.0))
    if r.status_code >= 400:
        # Surface the app's standard error {error, hint} as-is — actionable for the AI/user
        hint = ""
        try:
            hint = (r.json() or {}).get("hint", "")
        except Exception:
            hint = (r.text or "")[:200]
        raise RuntimeError(f"phone response {r.status_code}: {hint or '(no details)'}")
    if token:  # Decrypt the response body (respAad = request nonce + canonical path binding)
        aad = crypto.resp_aad("GET", path, headers[auth.NONCE_HEADER])
        data = crypto.open_(crypto.aead_key(token), aad, r.content)
    else:
        data = r.content
    return (data, r.headers) if want_headers else data


def _notify_phone(text: str) -> None:
    """Leave a short notification in the phone's activity log/toast (e.g. the PC path the AI saved). best-effort — ignored on failure."""
    try:
        import httpx
        from urllib.parse import quote

        base = _current_base()
        token = _current_token()
        if not base or not token:
            return
        path = f"/note?text={quote(text)}"
        headers = auth.sign(token, "POST", path)
        httpx.post(f"{base}{path}", headers=headers, timeout=httpx.Timeout(3.0, connect=2.0))
    except Exception:
        pass


_CONN_ERRS = None


def _plain_http(base: str, method: str, path: str, body: bytes = b""):
    """Sign, but return the response body as-is (no decryption) — for endpoints like /settings/cam_fps that
    return plaintext JSON (not wrapped in AEAD). Unlike _http(), does not attempt respAad decryption."""
    import httpx
    token = _current_token()
    headers = auth.sign(token, method, path) if token else {}
    r = (httpx.post(f"{base}{path}", content=body, headers=headers,
                     timeout=httpx.Timeout(5.0, connect=2.0))
         if method == "POST" else
         httpx.get(f"{base}{path}", headers=headers, timeout=httpx.Timeout(5.0, connect=2.0)))
    if r.status_code >= 400:
        hint = ""
        try:
            hint = (r.json() or {}).get("hint", "")
        except Exception:
            hint = (r.text or "")[:200]
        raise RuntimeError(f"phone response {r.status_code}: {hint or '(no details)'}")
    return r.content


def _plain(method: str, path: str, body: bytes = b""):
    """Try the cached address → on failure, rediscover via mDNS (retry) — same recovery pattern as _get()."""
    global _base_cache, _CONN_ERRS
    import httpx
    if _CONN_ERRS is None:
        _CONN_ERRS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)
    tried = []
    base = _current_base() or _base_cache
    if base:
        tried.append(base)
        try:
            return _plain_http(base, method, path, body)
        except _CONN_ERRS:
            pass
    for cand in discover_bases():
        if cand in tried:
            continue
        tried.append(cand)
        try:
            data = _plain_http(cand, method, path, body)
            _base_cache = cand
            return data
        except _CONN_ERRS:
            continue
    raise RuntimeError(
        "Cannot reach the phone. Check that the phone onSense app is [Started]/sharing and "
        "on the same Wi-Fi as the PC. (diagnose with uvx onsense doctor) "
        f"[addresses tried: {tried or 'none'}]"
    )


def _get(path: str, want_headers: bool = False):
    """Try the cached address → on failure, try every mDNS-discovered address until one succeeds (recovery on IP change).
    If want_headers=True, return a (bytes, httpx.Headers) tuple (e.g. the X-Ref-* metadata of /reference)."""
    global _base_cache, _CONN_ERRS
    import httpx
    if _CONN_ERRS is None:
        _CONN_ERRS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)
    tried = []
    base = _current_base() or _base_cache
    if base:
        tried.append(base)
        try:
            return _http(base, path, want_headers)
        except _CONN_ERRS:
            pass
    for cand in discover_bases():
        if cand in tried:
            continue
        tried.append(cand)
        try:
            data = _http(cand, path, want_headers)
            _base_cache = cand  # Cache the address that worked
            return data
        except _CONN_ERRS:
            continue
    raise RuntimeError(
        "Cannot reach the phone. Check that the phone onSense app is [Started]/sharing and "
        "on the same Wi-Fi as the PC. (diagnose with uvx onsense doctor) "
        f"[addresses tried: {tried or 'none'}]"
    )


@mcp.tool()
def get_live_frame() -> Image:
    """One current frame from the phone camera (JPEG). Falls back to a local test image (if configured) when the phone is unreachable."""
    try:
        return Image(data=_get("/shot.jpg"), format="jpeg")
    except Exception:
        if TEST_FRAME and os.path.exists(TEST_FRAME):
            with open(TEST_FRAME, "rb") as f:
                return Image(data=f.read(), format="jpeg")
        raise


@mcp.tool()
def read_sensors() -> str:
    """The phone's current sensor values (battery level/charging, illuminance lux, acceleration x/y/z) as JSON."""
    return _get("/sensors.json").decode("utf-8")


@mcp.tool()
def recent_photos(limit: int = 8) -> str:
    """JSON list of the device's recent photos (id, name, date_added, w, h). Get the image via get_photo(id).

    If the user designated content in the phone app (LIVE / Capture / File / Phone clipboard),
    prefer get_reference — it returns exactly what they picked; this gallery list may not include it.
    """
    raw = _get(f"/photos?limit={limit}").decode("utf-8")
    try:  # Save tokens: drop the rarely-used size field + serialize without whitespace
        data = json.loads(raw)
        for p in data.get("photos", []):
            p.pop("size", None)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return raw


@mcp.tool()
def get_photo(id: int, max_width: int = 1024) -> Image:
    """Fetch the photo for the id returned by recent_photos (downscaled to max_width)."""
    return Image(data=_get(f"/photo?id={id}&w={max_width}"), format="jpeg")


def _downloads_dir() -> str:
    """The user's Downloads folder (find the real location regardless of OS/locale).

    On Linux, xdg-user-dirs renames the folder per locale (e.g. a localized name instead of "Downloads"
    in a non-English environment) — hardcoding "~/Downloads" would create a wrong new folder instead of the actual one.
    """
    home = os.path.expanduser("~")
    if OSNAME == "Linux":
        cfg = os.path.join(home, ".config", "user-dirs.dirs")
        try:
            with open(cfg, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("XDG_DOWNLOAD_DIR="):
                        val = line.split("=", 1)[1].strip().strip('"')
                        val = val.replace("$HOME", home)
                        if val:
                            return val
        except OSError:
            pass
    return os.path.join(home, "Downloads")


@mcp.tool()
def get_reference() -> str:
    """Download whatever the phone currently designates as the 'share target' to the PC, save it, and return the path and metadata (JSON).

    Depends on the phone app's reference mode: LIVE = real-time camera frame, Capture = a fixed captured image,
    File = an arbitrary file the user chose. Non-image files (PDF, video, documents, etc.) are fetched as-is too.
    (If you only need an image preview, get_live_frame is simpler.)
    Returns: {"path", "mime", "bytes", "name"} — path is the PC-local save path (the user's Downloads folder,
    so it's easy to find and use later — not a temp folder).
    Always call this to fetch the phone's CURRENT designation — never reuse files already sitting in the
    save folder (they are stale copies from earlier fetches, not what the user has designated now).
    """
    import re

    data, headers = _get("/reference", want_headers=True)
    mime = headers.get("X-Ref-Mime") or "application/octet-stream"
    from urllib.parse import unquote
    name = unquote(headers.get("X-Ref-Name") or "reference.bin")  # Phone sends it percent-encoded
    # Replace only path separators, control characters, and whitespace — preserve Unicode filenames (e.g. non-ASCII)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]+', "_", os.path.basename(name)) or "reference.bin"
    outdir = os.path.join(_downloads_dir(), "onSense")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, safe)
    with open(path, "wb") as f:
        f.write(data)
    # Don't send the full path (which includes the local username) in the plaintext /note query — just the filename. (security: minimize metadata)
    _notify_phone(f"AI fetched a file · {os.path.basename(path)}")
    return json.dumps({"path": path, "mime": mime, "bytes": len(data), "name": name},
                      ensure_ascii=False)


@mcp.tool()
def get_cam_fps() -> str:
    """Query the phone's current camera FPS setting (15=high performance, 2=balanced, 0=on-demand)."""
    try:
        resp = _plain("GET", "/settings/cam_fps")
        data = json.loads(resp.decode("utf-8"))
        fps = data.get("fps", "?")
        label = data.get("label", "unknown")
        return f"current FPS: {fps} ({label})"
    except Exception as e:
        return f"FPS query failed: {e}"


@mcp.tool()
def set_cam_fps(fps: int) -> str:
    """Change the phone's camera FPS setting.

    fps: 15 (high performance), 2 (balanced, recommended), 0 (power-saving/on-demand)
    """
    if fps not in (0, 2, 15):
        return "❌ fps must be one of 0 (power-saving), 2 (balanced), 15 (high performance)"
    try:
        resp = _plain("POST", f"/settings/cam_fps?fps={fps}")
        data = json.loads(resp.decode("utf-8"))
        if data.get("status") == "ok":
            label = {15: "high performance", 2: "balanced", 0: "power-saving (on-demand)"}.get(fps, "")
            return f"✓ FPS setting changed: {fps} ({label})"
        return f"❌ setting failed: {data}"
    except Exception as e:
        return f"❌ FPS setting failed: {e}"


def main():
    old_pids = _terminate_old_serve_processes()
    # Avoid debugging confusion: log which server version started to stderr (stdout is MCP-protocol only)
    print(f"[onsense] serve v{__version__} (PHONE_BASE={_current_base() or 'mDNS auto'}, "
          f"PHONE_TOKEN={_token_diag(_current_token())}, pair={_pair_path()}, "
          f"dedup_killed={old_pids or 'none'}, module={__file__})",
          file=sys.stderr, flush=True)
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
