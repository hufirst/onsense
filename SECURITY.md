# Security Policy

## Reporting a vulnerability

**Please report vulnerabilities privately — do not open a public GitHub issue for security reports.**

Preferred channel: **GitHub private vulnerability reporting** on this repository (the **Security** tab → **Report a vulnerability**).

We aim to acknowledge reports within about 5 business days and to share a remediation timeline after triage. Helpful details: affected version, OS/environment, a clear description, and a proof-of-concept or reproduction steps if you have one.

## Supported versions

onsense is developed on a rolling basis; security fixes land in the latest version published on [PyPI](https://pypi.org/project/onsense/). Please upgrade to the latest `onsense` before reporting.

## Threat model

onsense is a **local-network** tool. Its security model:

- The phone (onSense app) and the PC run on the **same trusted local Wi-Fi**. There is **no developer-operated cloud** — the phone talks only to your own paired PC.
- Access is gated by a **128-bit pairing token** exchanged out-of-band via QR code.
- Requests are **HMAC-SHA256 signed** — constant-time verified, with a ±300 s timestamp window and nonce replay rejection. The token is never sent in cleartext.
- Sensitive payloads (camera frames, photos, sensors, files, clipboard) are **AES-256-GCM encrypted** between phone and PC. Keys are derived from the pairing token via HKDF-SHA256 (separate keys for signing and encryption); the GCM AAD binds each message to its request, rejecting tampering and response substitution.
- PC-side services **reject non-private (non-RFC1918) client IPs** at the application layer. Phone→PC clipboard pull (`GET /clip`) and OS-clipboard injection are **off by default** and require an explicit flag.

Because the design does not rely on secrecy of the code, publishing this source does not weaken it — its security rests on the pairing token, which is never in this repository.

## Known limitations

These are documented design limitations we intend to improve, not undisclosed vulnerabilities:

- **Plaintext HTTP metadata.** `/version`, `/health`, HTTP error responses, and the saved-file path returned after a push are not encrypted. A passive attacker on the same Wi-Fi can observe this metadata — but cannot decrypt payloads or forge signed requests without the token. Use on trusted networks; transport-level TLS is a possible future hardening.
- **In-memory buffering** of large transfers during encrypt/decrypt (chunked streaming is a future step).

## Out of scope

- Attacks that require the pairing token, or physical/root access to the phone or PC.
- Exposing these services to the public internet (an unsupported configuration).
- The Android app is distributed separately (Google Play). App-specific issues may be reported through the same private channel.

## Secrets

This repository contains no secrets. The pairing token is generated at runtime and stored locally (`~/.onsense/pair.json`; on the phone, in the Android Keystore). If you believe a secret was committed, please report it privately.
