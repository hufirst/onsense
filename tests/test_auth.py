"""auth.py unit tests — HMAC request signing/verification, rejecting replay/downgrade/expiry."""
import time

from onsense import auth

TOKEN = "testtoken123456"


def test_sign_verify_roundtrip():
    h = auth.sign(TOKEN, "POST", "/clip")
    assert h[auth.ENC_HEADER] == "aes256gcm/v1"
    assert auth.verify(TOKEN, "POST", "/clip", h.get, auth.NonceCache())


def test_replay_rejected():
    h = auth.sign(TOKEN, "GET", "/shot.jpg")
    nc = auth.NonceCache()
    assert auth.verify(TOKEN, "GET", "/shot.jpg", h.get, nc)
    assert not auth.verify(TOKEN, "GET", "/shot.jpg", h.get, nc)  # Reuse of the same nonce


def test_wrong_token_rejected():
    h = auth.sign(TOKEN, "GET", "/shot.jpg")
    assert not auth.verify("WRONGTOKEN", "GET", "/shot.jpg", h.get)


def test_method_and_path_binding():
    h = auth.sign(TOKEN, "GET", "/shot.jpg")
    assert not auth.verify(TOKEN, "POST", "/shot.jpg", h.get)
    assert not auth.verify(TOKEN, "GET", "/photo", h.get)


def test_missing_enc_rejected():
    h = auth.sign(TOKEN, "GET", "/shot.jpg")
    assert not auth.verify(TOKEN, "GET", "/shot.jpg",
                           lambda n: None if n == auth.ENC_HEADER else h.get(n))


def test_ts_window_rejected():
    h = auth.sign(TOKEN, "GET", "/shot.jpg")
    assert not auth.verify(TOKEN, "GET", "/shot.jpg", h.get, now=int(time.time()) + 9999)
