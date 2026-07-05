"""crypto.py unit tests + cross-language vectors against Android Crypto.kt (byte-for-byte match).

The vectors are identical to Android `CryptoTest.kt` — pinning Kotlin↔Python interop on both sides.
Run: cd pc && python -m pytest (or uv run pytest)
"""
import pytest
from cryptography.exceptions import InvalidTag

from onsense import crypto

TOKEN = "testtoken123456"
AEAD_KEY = "3545f6c2dbc67a7f1fd75bcd988d4d18346566d2dedfd5a7bb931c7e39db5bdd"
AUTH_KEY = "cabe2cc07d194d55b4da9f85323e309ba0aeeee189db32ac0b65deab2c0585bf"
NONCE = bytes(range(12))
WIRE = "000102030405060708090a0b8b598950b9c6451be16dfb1d62aa45d720c85a7339a8704f04aae7b5bf"


def _aad():
    return crypto.req_aad("POST", "/clip", "1700000000", "abc123nonce")


def test_key_derivation_matches_vector():
    assert crypto.aead_key(TOKEN).hex() == AEAD_KEY
    assert crypto.auth_key(TOKEN).hex() == AUTH_KEY
    assert crypto.aead_key(TOKEN) != crypto.auth_key(TOKEN)  # Domain separation


def test_seal_matches_vector():
    blob = crypto.seal(crypto.aead_key(TOKEN), _aad(), b"hello onSense", nonce=NONCE)
    assert blob.hex() == WIRE


def test_open_vector():
    assert crypto.open_(crypto.aead_key(TOKEN), _aad(), bytes.fromhex(WIRE)) == b"hello onSense"


def test_roundtrip_random_nonce():
    ak = crypto.aead_key(TOKEN)
    a = crypto.seal(ak, _aad(), b"data")
    b = crypto.seal(ak, _aad(), b"data")
    assert a != b  # Random nonce → different every time
    assert crypto.open_(ak, _aad(), a) == b"data"


def test_tamper_rejected():
    bad = bytearray(bytes.fromhex(WIRE))
    bad[20] ^= 1
    with pytest.raises(InvalidTag):
        crypto.open_(crypto.aead_key(TOKEN), _aad(), bytes(bad))


def test_aad_mismatch_rejected():
    wrong = crypto.req_aad("GET", "/clip", "1700000000", "abc123nonce")
    with pytest.raises(InvalidTag):
        crypto.open_(crypto.aead_key(TOKEN), wrong, bytes.fromhex(WIRE))
