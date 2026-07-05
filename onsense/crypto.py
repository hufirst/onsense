"""onSense body encryption — AES-256-GCM (P2). Byte-for-byte the same contract as Android Crypto.kt.

Key derivation: master secret = the token string's UTF-8 bytes (not decoded — consistent across old/new tokens and languages).
  auth_key = HKDF-SHA256(ikm=token_utf8, salt="", info="onsense/auth/v1", len=32)  # For HMAC signing
  aead_key = HKDF-SHA256(ikm=token_utf8, salt="", info="onsense/aead/v1", len=32)  # AES-256-GCM

AEAD: AES-256-GCM. Wire = nonce(12) || ciphertext || tag(16). The nonce is random per message (never reused).
  AAD binds request/response and algorithm (prevents downgrade).

Iron rule: never consume decrypted bytes before tag verification (successful open).
"""
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC = "aes256gcm/v1"  # X-Enc header value
_NONCE_LEN = 12
_TAG_LEN = 16


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF(SHA-256), empty salt (= hash-length worth of zeros). Uses stdlib hmac only."""
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    i = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def auth_key(token: str) -> bytes:
    return hkdf_sha256(token.encode("utf-8"), b"onsense/auth/v1")


def aead_key(token: str) -> bytes:
    return hkdf_sha256(token.encode("utf-8"), b"onsense/aead/v1")


def canon_path(path: str) -> str:
    """Canonical path for signing/AAD: includes the query string, sorted per `k=v` token.
    If the query is excluded from the signature, an on-path attacker can swap just the parameters
    (?id=/?fps= etc.) of a validly signed request to bypass integrity. PC and Android (Crypto.canonPath)
    must share the same contract. To avoid encoding mismatches, raw tokens are sorted as-is without re-encoding."""
    p, sep, q = path.partition("?")
    if not sep or not q:
        return p
    return p + "?" + "&".join(sorted(q.split("&")))


def req_aad(method: str, path: str, ts: str, nonce: str, enc: str = ENC) -> bytes:
    return f"req\n{method.upper()}\n{canon_path(path)}\n{ts}\n{nonce}\n{enc}".encode("utf-8")


def resp_aad(method: str, path: str, req_nonce: str, enc: str = ENC) -> bytes:
    """Response body AAD — binds to request nonce, method, canonical path, and algorithm (prevents response substitution/confusion)."""
    return f"resp\n{method.upper()}\n{canon_path(path)}\n{req_nonce}\n{enc}".encode("utf-8")


def seal(key: bytes, aad: bytes, plaintext: bytes, nonce: bytes = None) -> bytes:
    """Returns nonce||ciphertext||tag. If nonce is unset, uses a random 12B nonce."""
    if nonce is None:
        nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)  # ciphertext || tag(16)
    return nonce + ct


def open_(key: bytes, aad: bytes, blob: bytes) -> bytes:
    """Verify and decrypt. Raises InvalidTag on tag mismatch (do not consume)."""
    if len(blob) < _NONCE_LEN + _TAG_LEN:
        raise ValueError("blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)
