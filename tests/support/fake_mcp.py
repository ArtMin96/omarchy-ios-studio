"""A fake MCP server over stdio for `agents test` (helper/iosstudio/features/agents/rpc.py).

FAKE_MCP_MODE picks the behaviour:
  ok        initialize, then tools/list in pages of 2 (nextCursor) over FAKE_MCP_TOOLS tools (default 5);
            log notifications and stderr noise are sent before each answer
  exit      exits right after reading initialize (stderr says why)
  garbage   answers initialize with a line that isn't JSON
  error     answers tools/list with a JSON-RPC error
  silent    reads forever, never answers
  endless   every tools/list page has a nextCursor
"""
from __future__ import annotations

FAKE_MCP = r'''#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_MCP_MODE", "ok")
n_tools = int(os.environ.get("FAKE_MCP_TOOLS", "5"))
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    sys.stderr.write(f"got {method}\n"); sys.stderr.flush()
    if mode == "silent":
        continue
    if method == "initialize":
        if mode == "exit":
            sys.stderr.write("fatal: no config\n"); sys.exit(3)
        if mode == "garbage":
            print("Welcome to my server!", flush=True); continue
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": msg["params"]["protocolVersion"],
              "serverInfo": {"name": "fake", "version": "1"}, "capabilities": {"tools": {}}}})
    elif method == "tools/list":
        if mode == "error":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no tools"}}); continue
        start = int((msg.get("params") or {}).get("cursor") or 0)
        names = [f"tool{i}" for i in range(start, min(start + 2, n_tools))]
        res = {"tools": [{"name": n} for n in names]}
        if mode == "endless" or start + 2 < n_tools:
            res["nextCursor"] = str(start + 2)
        send({"jsonrpc": "2.0", "id": rid - 1000, "result": {"stale": True}})   # an unrelated id is skipped
        send({"jsonrpc": "2.0", "id": rid, "result": res})
'''
