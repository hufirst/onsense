# onsense — Wire Protocol & Security

This document specifies the on-the-wire protocol between the **phone** (Android app,
HTTP provider on :8080) and the **PC** (this package: MCP broker + clip daemon on :8770),
so the cryptography can be reviewed and reimplemented. The reference implementations are
`pc/onsense/auth.py` + `pc/onsense/crypto.py` (Python) and `Auth.kt` + `Crypto.kt` (Kotlin);
they are byte-for-byte compatible (verified by a shared test vector).

## 1. Pairing

The phone displays (or scans) a pairing URI `onsense://pair?base=<http://ip:8080>&token=<token>`.
The token is a 128-bit random value, base64url-encoded (no padding). It is exchanged **out of
band** (QR on screen / direct entry) and never transmitted over the network in cleartext.

The token string is the shared secret. **It is used as UTF-8 bytes as-is** (no base64 decode) as
input keying material — this keeps old and new token formats and both language implementations
consistent.

## 2. Key derivation (HKDF-SHA256)

```
ikm      = token.encode("utf-8")
auth_key = HKDF-SHA256(ikm, salt="", info="onsense/auth/v1", length=32)   # request signing
aead_key = HKDF-SHA256(ikm, salt="", info="onsense/aead/v1", length=32)   # body encryption
```

HKDF is RFC 5869 with an empty salt (32 zero bytes). The two `info` strings give independent
keys (domain separation).

## 3. Request authentication (HMAC-SHA256)

Every authenticated request carries:

| Header | Value |
|---|---|
| `X-Ts` | unix seconds (decimal string) |
| `X-Nonce` | 16 random bytes, hex |
| `X-Enc` | body encryption algorithm, currently `aes256gcm/v1` |
| `X-Sig` | `hex(HMAC-SHA256(auth_key, canonical))` |

```
canonical = "{METHOD}\n{path}\n{ts}\n{nonce}\n{enc}"
```

`METHOD` is uppercase. `path` is the **canonicalized** request path: if the request has a query
string, its `&`-separated `k=v` tokens are **sorted and included** (e.g. `/photo?id=5` signs the
`id=5`), so an on-path attacker cannot tamper with parameters like `?id=` / `?fps=` on an otherwise
valid signed request. Values are not re-encoded — raw tokens are sorted as-is, and PC and Android
(`Crypto.canonPath`) must apply the identical rule. A path with no query string signs the path
alone. Including `X-Enc` in the signed canonical prevents an attacker from stripping/downgrading
encryption.

Verification (constant-time):
1. `X-Enc` must equal the supported algorithm, else reject (downgrade protection).
2. `|now - ts|` ≤ 300 s, else reject (clock-skew / replay window).
3. recompute the signature and compare with `hmac.compare_digest`.
4. `X-Nonce` must not have been seen within the window (in-memory replay cache).

The `/health` (clip daemon) and `/version` (phone) endpoints are unauthenticated.

## 4. Body encryption (AES-256-GCM)

Sensitive bodies — camera frames, photos, sensor JSON, files, clipboard — are encrypted.
The wire body is:

```
nonce(12) || ciphertext || tag(16)
```

`nonce` is 12 random bytes per message (**never reused** with a key). `tag` is the 128-bit
GCM tag. The AEAD additional data (AAD) binds the body to its request/response:

```
request body  AAD = "req\n{METHOD}\n{path}\n{X-Ts}\n{X-Nonce}\n{X-Enc}"
response body AAD = "resp\n{METHOD}\n{path}\n{request X-Nonce}\n{X-Enc}"
```

A recipient **must not** use any decrypted bytes before the GCM tag verifies (decrypt to a
temporary buffer/file, verify, then commit). A tag mismatch (tampering, wrong key, or
mismatched AAD) is rejected.

### What is encrypted

- Phone → PC: camera (`/shot.jpg`), sensors (`/sensors.json`), photo list (`/photos`),
  photo (`/photo`) **response bodies**; clip push (`POST /clip`) **request body**;
  clip pull (`GET /clip`) **response body**.
- Plaintext (low-sensitivity): `/version`, `/health`, HTTP error responses, and the saved-file
  path string returned after a push. HTTP headers (filename, content-type, sizes) are plaintext.

## 5. Threat model & limitations

Protects against, on a shared/untrusted LAN: token theft, request forgery, replay, passive
eavesdropping of sensitive payloads, and body/response tampering or substitution.

Does **not** (yet) provide: full transport confidentiality of metadata/headers; protection of
the low-sensitivity plaintext endpoints. Current limitations:

- **Single-GCM buffering**: each message is encrypted/decrypted as one unit, so very large
  transfers buffer the body in memory. Chunked AEAD streaming for large files is a future step.
- TLS (transport-level encryption) is a possible future hardening.

Servers bind to all interfaces but reject non-private (non-RFC1918/loopback) client IPs at the
application layer; the daemon is intended for trusted private networks only.

## 6. Versioning

`X-Enc` carries an explicit `…/v1` tag so the algorithm can be rotated without ambiguity.
`info` strings in §2 are likewise versioned.
