"""Reconstruct the exact request payload for attempt N from a payloads.jsonl delta log.

Usage: python scripts/reconstruct_payload.py <output-dir>/.arc_adapter/payloads.jsonl <request-number>
The history is append-only, so the payload of attempt N is the fold of all appends
up to (and including) its line. Full-snapshot fallback lines reset the prefix.
"""

import json
import sys
from pathlib import Path

log, want = Path(sys.argv[1]), int(sys.argv[2])
messages = []
for line in log.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if record["prefix_len"] < len(messages):  # full-snapshot fallback: reset prefix
        messages = messages[: record["prefix_len"]]
    messages.extend(record["append"])
    if record["request"] == want:
        print(
            json.dumps(
                {"request": want, "messages": messages}, ensure_ascii=False, indent=2
            )
        )
        break
else:
    raise SystemExit(f"request {want} not found in {log}")
