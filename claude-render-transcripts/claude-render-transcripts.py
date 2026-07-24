#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Render a Claude Code .jsonl transcript to readable text."""
import json, sys

def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            out.append(b["text"])
        elif t == "thinking":
            out.append("[thinking]\n" + b.get("thinking", ""))
        elif t == "tool_use":
            inp = json.dumps(b.get("input", {}), indent=2)
            if len(inp) > 2000:
                inp = inp[:2000] + "\n... [truncated]"
            out.append(f"[tool_use: {b.get('name')}]\n{inp}")
        elif t == "tool_result":
            c = b.get("content")
            c = c if isinstance(c, str) else text_of(c)
            if len(c) > 2000:
                c = c[:2000] + "\n... [truncated]"
            out.append(f"[tool_result]\n{c}")
    return "\n\n".join(out)

for path in sys.argv[1:]:
    for line in open(path):
        d = json.loads(line)
        if d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message", {})
        body = text_of(msg.get("content"))
        if not body.strip():
            continue
        ts = d.get("timestamp", "")[11:19]
        print(f"\n{'=' * 70}\n{msg.get('role', '?').upper()}  {ts}\n{'=' * 70}\n{body}")
