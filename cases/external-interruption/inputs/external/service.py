"""Isolated external service simulator; state persists across client interruptions."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
path = root / "service-state.json"
state = json.loads(path.read_text(encoding="utf-8"))
if sys.argv[1:] == ["query"]:
    state["query_count"] += 1
    path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({"status":"applied","operation_id":"publish-1","idempotency_key":"publish-key-1",
                      "attempt_id":"attempt-1","attempt_sequence":1,"revision":"r1"}))
elif sys.argv[1:] == ["apply"]:
    state["execute_count"] += 1
    state["apply_count"] += 1
    path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({"status":"applied"}))
else:
    sys.exit(2)
