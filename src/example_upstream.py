"""
example_upstream.py
───────────────────
A minimal MCP server you can run to test the gateway.
It exposes a few echo tools so you can verify proxying works.

Usage:
    python example_upstream.py --name database --port 8003

The gateway will proxy tools/call requests to this server.
"""
import argparse
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

parser = argparse.ArgumentParser()
parser.add_argument("--name", default="demo", help="Server name (used as tool prefix)")
parser.add_argument("--port", type=int, default=8001, help="Port to listen on")
args, _ = parser.parse_known_args()

app = FastAPI(title=f"MCP Server: {args.name}")

TOOLS = [
    {"name": "echo",       "description": "Echo back the input",      "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}}},
    {"name": "ping",       "description": "Returns pong",             "inputSchema": {"type": "object"}},
    {"name": "list_items", "description": "Returns a sample list",    "inputSchema": {"type": "object"}},
    {"name": "get_time",   "description": "Returns current UTC time", "inputSchema": {"type": "object"}},
]


@app.post("/mcp")
async def handle(request: Request):
    body = await request.json()
    method = body.get("method", "")
    rpc_id = body.get("id")

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": args.name, "version": "1.0.0"}
        }})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": TOOLS}})

    if method == "tools/call":
        tool = body.get("params", {}).get("name", "")
        arguments = body.get("params", {}).get("arguments", {})

        if tool.endswith("__echo"):
            content = arguments.get("message", "(no message)")
        elif tool.endswith("__ping"):
            content = "pong"
        elif tool.endswith("__list_items"):
            content = ["item_1", "item_2", "item_3"]
        elif tool.endswith("__get_time"):
            from datetime import datetime, timezone
            content = datetime.now(timezone.utc).isoformat()
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32601, "message": f"Unknown tool: {tool}"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "result": {"content": [{"type": "text", "text": str(content)}]}})

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    print(f"\n  Upstream MCP server '{args.name}' listening on port {args.port}")
    print(f"  Tools: {[t['name'] for t in TOOLS]}\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port)
