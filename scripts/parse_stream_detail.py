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
        continue
    t = obj.get("type")
    if t == "system":
        print("SYSTEM", obj.get("subtype"), {k: obj.get(k) for k in obj if k not in ("uuid", "session_id", "tools", "slash_commands", "agents", "skills", "plugins", "memory_paths", "capabilities")})
    elif t == "stream_event":
        ev = obj.get("event") or {}
        delta = ev.get("delta") or {}
        et = ev.get("type")
        if et == "content_block_delta":
            print("DELTA", delta)
        elif et == "content_block_start":
            print("START", ev.get("content_block"))
    elif t == "assistant":
        for block in (obj.get("message") or {}).get("content") or []:
            print("ASSIST", block.get("type"), repr((block.get("thinking") or block.get("text") or "")[:180]))
    elif t == "result":
        print("RESULT", repr((obj.get("result") or "")[:180]))
