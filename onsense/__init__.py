"""onSense — a PC-side MCP broker that turns your phone into the AI's eyes and sensors.

Subcommands: serve (MCP server) / pair (pairing + registration) / doctor (diagnostics).
The phone (Android app) is the HTTP provider; this package is a stdio MCP broker that connects to AI clients.
"""

# 버전은 패키지 메타데이터(pyproject)에서 동적으로 읽는다 — 하드코딩 문자열이 범프 때 어긋나는 것을 방지.
from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PkgNotFound

try:
    __version__ = _pkg_version("onsense")
except _PkgNotFound:  # 메타데이터 없는 소스 체크아웃(미설치)일 때만
    __version__ = "0.0.0+dev"

# Constants agreed with the phone app (android: CameraService.PORT / Auth / NSD)
PHONE_PORT = 8080
MDNS_TYPE = "_onsense._tcp.local."
TOKEN_HEADER = "X-Token"
PAIR_PORT = 8765
CLIP_PORT = 8770  # Phone→PC clipboard/file daemon (clip.py). Bidirectional GET uses the same port.
