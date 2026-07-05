"""0.3.2 security-hardening regression tests — H2 (query signing), H1 (pairing channel binding)."""
from onsense import auth, crypto


TOKEN = "testtoken123456"


# ── H2: signature/AAD canonical path (query included) ────────────────────────
def test_canon_path_sorts_query():
    assert crypto.canon_path("/photo") == "/photo"
    assert crypto.canon_path("/photo?id=5&w=1024") == "/photo?id=5&w=1024"
    # Order-independent (sorted) — same signature even if PC/phone parameter order differs
    assert crypto.canon_path("/photo?w=1024&id=5") == "/photo?id=5&w=1024"
    assert crypto.canon_path("/photos?") == "/photos"


def test_signature_covers_query():
    # Normal: verification passes for the same path
    h = auth.sign(TOKEN, "GET", "/photo?id=5&w=1024")
    assert auth.verify(TOKEN, "GET", "/photo?id=5&w=1024", h.get)
    # The same request differing only in parameter order also passes (canonicalization)
    assert auth.verify(TOKEN, "GET", "/photo?w=1024&id=5", h.get)


def test_tampered_query_rejected():
    # An on-path attacker swaps id → rejected because the signature covers the path
    h = auth.sign(TOKEN, "GET", "/photo?id=5&w=1024")
    assert not auth.verify(TOKEN, "GET", "/photo?id=999&w=1024", h.get)
    # fps tampering is also rejected
    h2 = auth.sign(TOKEN, "POST", "/settings/cam_fps?fps=2")
    assert not auth.verify(TOKEN, "POST", "/settings/cam_fps?fps=15", h2.get)


def test_resp_aad_binds_canon_path():
    # The response AAD is also bound to the canonical path → the response to a tampered request can't be decrypted
    key = crypto.aead_key(TOKEN)
    aad = crypto.resp_aad("GET", "/photo?id=5&w=1024", "nonce123")
    blob = crypto.seal(key, aad, b"real-photo")
    # The same request (order-independent) decrypts successfully
    ok = crypto.open_(key, crypto.resp_aad("GET", "/photo?w=1024&id=5", "nonce123"), blob)
    assert ok == b"real-photo"
    # A tampered id fails to decrypt
    import pytest
    with pytest.raises(Exception):
        crypto.open_(key, crypto.resp_aad("GET", "/photo?id=999&w=1024", "nonce123"), blob)


# ── H1: pairing listener gate ────────────────────────────────────────────────
def test_pairing_gates():
    from onsense import pair
    assert pair._lan_ok("192.168.0.5")
    assert pair._lan_ok("127.0.0.1")
    assert pair._lan_ok("10.1.2.3")
    assert not pair._lan_ok("8.8.8.8")          # Reject public IP
    assert not pair._lan_ok("example.com")      # Reject non-IP
    assert pair._valid_base("http://192.168.0.5:8080")
    assert not pair._valid_base("http://8.8.8.8:8080")   # Block external-address injection
    assert not pair._valid_base("https://192.168.0.5")   # http only
    assert not pair._valid_base("file:///etc/passwd")
    assert not pair._valid_base("")
