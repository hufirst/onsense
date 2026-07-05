---
description: "onSense — turn your phone into the AI's eyes and sensors. /onsense [see|sensors|photos|photo <id>|all]"
allowed-tools:
  - "mcp__onsense__get_live_frame"
  - "mcp__onsense__read_sensors"
  - "mcp__onsense__recent_photos"
  - "mcp__onsense__get_photo"
---

Branch on `$ARGUMENTS` (empty → **see**). Keep responses short.

- see / look / camera / (empty) → `get_live_frame` → describe what you see concisely (transcribe any text verbatim). If dark/blurry, add a one-liner: "hold the phone steady on the target".
- sensors / status → `read_sensors` → summarize as battery, illuminance, and posture (lying/upright/tilted).
- photos → `recent_photos` → a simple table of number, filename, date.
- photo <id> → `get_photo(id)` (if the id is unknown, run `recent_photos` first).
- all → `read_sensors` + `get_live_frame` together.

On failure ("phone unreachable"): a one-line note to check that the phone app is **sharing** and on the same Wi-Fi.

$ARGUMENTS
