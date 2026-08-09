#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print("RAW", line[:200])
        continue
    t = obj.get("type")
    if t == "stream_event":
        ev = obj.get("event") or {}
        et = ev.get("type") if isinstance(ev, dict) else None
        delta = (ev.get("delta") if isinstance(ev, dict) else None) or {}
        print("STREAM", et, list(delta)[:8] if isinstance(delta, dict) else type(delta))
        if isinstance(delta, dict):
            for k in ("thinking", "text", "partial_json", "type"):
                if k in delta:
                    print("  DELTA", k, repr(delta[k])[:160])
    elif t == "assistant":
        msg = obj.get("message") or {}
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "thinking":
                print("THINK", repr((block.get("thinking") or "")[:240]))
            elif bt == "text":
                print("TEXT", repr((block.get("text") or "")[:240]))
    elif t == "result":
        print("RESULT", repr((obj.get("result") or "")[:240]))
    else:
        print("OTHER", t, obj.get("subtype"))
