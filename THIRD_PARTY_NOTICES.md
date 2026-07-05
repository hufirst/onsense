# Third-Party Notices

This file summarizes third-party open source components used by the PC-side
onSense MCP package.

The PC-side onSense MCP package is licensed under the MIT License. The Android
app, onSense brand assets, logos, store assets, and product documentation are
not covered by that license unless explicitly stated.

## Direct Dependencies

| Package | License | Notes |
|---|---|---|
| `mcp` | MIT | MCP Python SDK/runtime dependency |
| `httpx` | BSD-3-Clause | HTTP client |
| `zeroconf` | LGPL-2.1-or-later | mDNS discovery |
| `qrcode` | BSD | QR output for pairing |
| `cryptography` | Apache-2.0 OR BSD-3-Clause | AES-256-GCM body encryption (P2) |

## Notable Transitive Dependencies

| Package | License | Notes |
|---|---|---|
| `anyio` | MIT | Async compatibility layer |
| `httpcore` | BSD-3-Clause | HTTP transport used by `httpx` |
| `certifi` | MPL-2.0 | CA certificate bundle |
| `idna` | BSD-3-Clause | Internationalized domain name support |
| `h11` | MIT | HTTP/1.1 protocol library |
| `attrs` | MIT | Utility dependency used by dependency tree |
| `pydantic` | MIT | Data validation used by dependency tree |
| `starlette` | BSD-3-Clause | ASGI framework used by dependency tree |
| `uvicorn` | BSD-3-Clause | ASGI server used by dependency tree |

## LGPL Dependency Note

`zeroconf` is licensed under LGPL-2.1-or-later. onSense depends on it as an
external Python package and does not copy its source code into this package.
If onSense is redistributed as a bundled executable or packaged with vendored
dependencies, the distributor should review the LGPL-2.1-or-later obligations,
including license notice, license text availability, and user ability to
replace or relink the LGPL-covered component where applicable.

## Verification

Before publishing, re-check dependency metadata with the locked environment:

```bash
uv run --project pc python - <<'PY'
from importlib.metadata import metadata
for name in ["mcp", "httpx", "zeroconf", "qrcode", "certifi"]:
    m = metadata(name)
    print(name, m.get("Version"), m.get("License") or m.get("License-Expression"))
PY
```
