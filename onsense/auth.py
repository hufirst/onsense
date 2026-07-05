"""onSense request signing — authenticate and prevent replay via HMAC-SHA256 without sending the token in plaintext.

Signature: HMAC-SHA256(token, f"{METHOD}\\n{path}\\n{ts}\\n{nonce}") → hex
Headers: X-Ts (unix seconds), X-Nonce (hex), X-Sig (hex). (The old plaintext X-Token transport is dropped.)
Verification: ts window (±WINDOW) + reject duplicate nonces (in-memory cache) + constant-time sig comparison.

Standard library only. The data body is still plaintext (full confidentiality via TLS later) —
the signature blocks token theft, request forgery, and replay. Same contract as Android Auth.kt.
"""
import hashlib
import hmac
import os
import time

from . import crypto

WINDOW = 300  # Allowed clock skew / replay window (seconds)
TS_HEADER = "X-Ts"
NONCE_HEADER = "X-Nonce"
SIG_HEADER = "X-Sig"
ENC_HEADER = "X-Enc"  # Body encryption scheme (included in the canonical string → prevents stripping/downgrade)


def _canon(method: str, path: str, ts: str, nonce: str, enc: str) -> bytes:
    # Canonical path (with sorted query) — blocks integrity bypass via parameter tampering. Same as crypto.canon_path.
    return f"{method.upper()}\n{crypto.canon_path(path)}\n{ts}\n{nonce}\n{enc}".encode("utf-8")


def sign(token: str, method: str, path: str) -> dict:
    """Signature header dict for a single request (keyed by auth_key, includes X-Enc)."""
    ts = str(int(time.time()))
    nonce = os.urandom(16).hex()
    enc = crypto.ENC
    sig = hmac.new(crypto.auth_key(token), _canon(method, path, ts, nonce, enc),
                   hashlib.sha256).hexdigest()
    return {TS_HEADER: ts, NONCE_HEADER: nonce, SIG_HEADER: sig, ENC_HEADER: enc}


class NonceCache:
    """Nonce cache for replay prevention (in-memory, window expiry)."""

    def __init__(self, cap: int = 1024):
        self.cap = cap
        self._d = {}  # nonce -> expiry time

    def check_and_add(self, nonce: str, now: int) -> bool:
        """Returns True (reject) if the nonce was already seen. If new, registers it and returns False."""
        if len(self._d) > self.cap:
            for k in [k for k, e in self._d.items() if e < now]:
                self._d.pop(k, None)
        exp = self._d.get(nonce)
        if exp is not None and exp >= now:
            return True
        self._d[nonce] = now + WINDOW
        return False


def verify(token: str, method: str, path: str, get_header, nonce_cache=None, now=None) -> bool:
    """Verify a signature. get_header(name)->value|None. If nonce_cache is given, reject replays."""
    if not token:
        return False
    ts = get_header(TS_HEADER)
    nonce = get_header(NONCE_HEADER)
    sig = get_header(SIG_HEADER)
    enc = get_header(ENC_HEADER)
    if not ts or not nonce or not sig:
        return False
    if enc != crypto.ENC:  # Reject missing/downgraded enc (hard cutover)
        return False
    try:
        tsi = int(ts)
    except (TypeError, ValueError):
        return False
    now = int(time.time()) if now is None else now
    if abs(now - tsi) > WINDOW:
        return False
    expect = hmac.new(crypto.auth_key(token), _canon(method, path, ts, nonce, enc),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    if nonce_cache is not None and nonce_cache.check_and_add(nonce, now):
        return False
    return True
