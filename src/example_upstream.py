"""
example_upstream.py
-------------------
A local MCP test server for exercising gateway tools, prompts, resources,
and resource template flows.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

parser = argparse.ArgumentParser()
parser.add_argument("--name", default="demo", help="Server name")
parser.add_argument("--port", type=int, default=8001, help="Port to listen on")
args, _ = parser.parse_known_args()

app = FastAPI(title=f"MCP Server: {args.name}")

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "ping",
        "description": "Returns pong",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "list_items",
        "description": "Returns a sample list",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "get_time",
        "description": "Returns current UTC time",
        "inputSchema": {"type": "object"},
    },
]

PROMPTS = [
    {
        "name": "summarize_notes",
        "title": "Summarize Notes",
        "description": "Create a concise summary from free-form notes.",
        "arguments": [
            {"name": "notes", "description": "The notes to summarize", "required": True}
        ],
    },
    {
        "name": "draft_email",
        "title": "Draft Email",
        "description": "Draft a short email for the provided topic and audience.",
        "arguments": [
            {"name": "topic", "description": "What the email is about", "required": True},
            {"name": "audience", "description": "Who the email is for", "required": False},
        ],
    },
]

RESOURCES = [
    {
        "uri": "memo://welcome",
        "name": "welcome",
        "title": "Welcome Memo",
        "description": "Introductory text content for gateway resource testing.",
        "mimeType": "text/plain",
        "text": f"Welcome from the {args.name} test server.",
    },
    {
        "uri": "config://status",
        "name": "status",
        "title": "Gateway Status Example",
        "description": "A JSON configuration-like document.",
        "mimeType": "application/json",
        "text": '{"environment":"dev","healthy":true,"source":"example_upstream"}',
    },
]

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "memo://note/{slug}",
        "name": "note_template",
        "title": "Memo By Slug",
        "description": "Generate memo URIs dynamically by slug.",
        "mimeType": "text/plain",
    }
]

def jsonrpc_ok(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

def jsonrpc_error(rpc_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}

def get_prompt(name: str):
    return next((prompt for prompt in PROMPTS if prompt["name"] == name), None)

def get_resource(uri: str):
    resource = next((item for item in RESOURCES if item["uri"] == uri), None)
    if resource:
        return resource

    if uri.startswith("memo://note/"):
        slug = uri.removeprefix("memo://note/")
        return {
            "uri": uri,
            "name": slug,
            "title": f"Memo {slug}",
            "description": "Dynamically generated memo resource from a template.",
            "mimeType": "text/plain",
            "text": f"Generated note for slug '{slug}' from server '{args.name}'.",
        }
    return None

@app.post("/mcp")
async def handle(request: Request):
    body = await request.json()
    method = body.get("method", "")
    rpc_id = body.get("id")
    params = body.get("params", {}) or {}

    if method == "initialize":
        return JSONResponse(jsonrpc_ok(rpc_id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
            "serverInfo": {"name": args.name, "version": "1.1.0"},
        }))

    if method == "tools/list":
        return JSONResponse(jsonrpc_ok(rpc_id, {"tools": TOOLS}))

    if method == "tools/call":
        tool = params.get("name", "")
        arguments = params.get("arguments", {}) or {}

        print(f"Tool call: {tool} with arguments {arguments}")

        if tool == "echo":
            content = arguments.get("message", "(no message)")
        elif tool == "ping":
            content = "pong"
        elif tool == "list_items":
            content = ["item_1", "item_2", "item_3"]
        elif tool == "get_time":
            content = datetime.now(timezone.utc).isoformat()
        else:
            return JSONResponse(jsonrpc_error(rpc_id, -32601, f"Unknown tool: {tool}"))

        print(f"Tool response: {content}")
        
        # ✅ Perfectly formatted JSON-RPC return
        return JSONResponse(jsonrpc_ok(rpc_id, {
            "content": [{"type": "text", "text": str(content)}]
        }))

    if method == "prompts/list":
        return JSONResponse(jsonrpc_ok(rpc_id, {"prompts": PROMPTS}))

    if method == "prompts/get":
        prompt_name = params.get("name", "")
        prompt = get_prompt(prompt_name)
        if not prompt:
            return JSONResponse(jsonrpc_error(rpc_id, -32601, f"Unknown prompt: {prompt_name}"))

        arguments = params.get("arguments", {}) or {}
        if prompt_name == "summarize_notes":
            text = f"Summarize these notes:\n\n{arguments.get('notes', '')}"
        else:
            text = (
                f"Draft an email about '{arguments.get('topic', '')}' "
                f"for '{arguments.get('audience', 'a general audience')}'."
            )

        return JSONResponse(jsonrpc_ok(rpc_id, {
            "description": prompt["description"],
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": text},
                }
            ],
        }))

    if method == "resources/list":
        resources = [
            {
                "uri": resource["uri"],
                "name": resource["name"],
                "title": resource["title"],
                "description": resource["description"],
                "mimeType": resource["mimeType"],
            }
            for resource in RESOURCES
        ]
        return JSONResponse(jsonrpc_ok(rpc_id, {"resources": resources}))

    if method == "resources/templates/list":
        return JSONResponse(jsonrpc_ok(rpc_id, {"resourceTemplates": RESOURCE_TEMPLATES}))

    if method == "resources/read":
        uri = params.get("uri", "")
        resource = get_resource(uri)
        if not resource:
            return JSONResponse(jsonrpc_error(rpc_id, -32601, f"Unknown resource: {uri}"))

        return JSONResponse(jsonrpc_ok(rpc_id, {
            "contents": [
                {
                    "uri": resource["uri"],
                    "mimeType": resource["mimeType"],
                    "text": resource["text"],
                }
            ]
        }))

    return JSONResponse(jsonrpc_error(rpc_id, -32601, "Method not found"))

if __name__ == "__main__":
    print(f"\n  Upstream MCP server '{args.name}' listening on port {args.port}")
    print(f"  Tools: {[t['name'] for t in TOOLS]}")
    print(f"  Prompts: {[p['name'] for p in PROMPTS]}")
    print(f"  Resources: {[r['uri'] for r in RESOURCES]}")
    print(f"  Resource templates: {[t['uriTemplate'] for t in RESOURCE_TEMPLATES]}\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port)