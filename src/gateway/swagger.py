import json
import os
import re
import time
import httpx
import jsonref
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from gateway.auth import require_admin

router = APIRouter()

VIRTUAL_APIS_FILE = "virtual_apis.json"

def load_virtual_apis():
    if os.path.exists(VIRTUAL_APIS_FILE):
        try:
            with open(VIRTUAL_APIS_FILE, "r") as f:
                return json.load(f)
        except Exception: pass
    return {}

VIRTUAL_APIS = load_virtual_apis()

def save_virtual_apis():
    with open(VIRTUAL_APIS_FILE, "w") as f:
        json.dump(VIRTUAL_APIS, f)

def rpc_ok(rpc_id, result): return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
def rpc_error(rpc_id, code, message): return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}

# --- CORE PARSING ENGINE ---
async def parse_swagger_schema(swagger_url, swagger_json):
    """Parses a Swagger URL or JSON object and extracts MCP tools."""
    if swagger_json:
        spec = jsonref.replace_refs(swagger_json)
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(swagger_url)
            spec = jsonref.loads(resp.text)

    target_base_url = ""
    if "servers" in spec and len(spec["servers"]) > 0:
        target_base_url = spec["servers"][0]["url"]
    elif "host" in spec:
        target_base_url = f"https://{spec['host']}{spec.get('basePath', '')}"
    
    if not target_base_url and swagger_url:
        match = re.match(r"(https?://[^/]+)", swagger_url)
        target_base_url = match.group(1) if match else swagger_url

    tools = []
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        for method, details in methods.items():
            if method.lower() not in ["get", "post", "put", "delete", "patch"]: continue

            op_id = details.get("operationId")
            if not op_id:
                clean_path = path.strip("/").replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
                op_id = f"{method.lower()}_{clean_path}"
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', op_id)[:64]

            properties = {}
            required = []
            
            for p in details.get("parameters", []):
                name = p.get("name")
                if not name: continue
                properties[name] = {"type": p.get("schema", {}).get("type", "string"), "description": p.get("description", "")}
                if p.get("required"): required.append(name)

            req_body = details.get("requestBody")
            if req_body and "content" in req_body:
                json_schema = req_body["content"].get("application/json", {}).get("schema", {})
                
                if json_schema.get("type") == "object" and "properties" in json_schema:
                    for k, v in json_schema["properties"].items():
                        properties[k] = {"type": v.get("type", "string"), "description": v.get("description", "")}
                        if k in json_schema.get("required", []): required.append(k)
                else:
                    properties["request_body"] = {"type": "object", "description": "JSON payload"}
                    if req_body.get("required"): required.append("request_body")

            tools.append({
                "name": safe_name,
                "description": details.get("summary", details.get("description", "")),
                "method": method.upper(),
                "path": path,
                "inputSchema": {"type": "object", "properties": properties, "required": required}
            })
            
    return target_base_url, tools

# --- ENDPOINTS ---

@router.post("/admin/swagger/preview", dependencies=[Depends(require_admin)])
async def preview_mcp_from_swagger(request: Request):
    """Parses a schema and returns the discovered tools WITHOUT saving."""
    payload = await request.json()
    swagger_url = payload.get("swagger_url")
    swagger_json = payload.get("swagger_json")

    if not swagger_url and not swagger_json:
        return JSONResponse({"reachable": False, "error": "Missing swagger URL or JSON file data"})

    try:
        _, tools = await parse_swagger_schema(swagger_url, swagger_json)
        return JSONResponse({
            "reachable": True,
            "protocol_version": "OpenAPI Spec",
            "tool_count": len(tools),
            "resource_count": 0,
            "prompt_count": 0,
            "tools": tools,
            "resources": [],
            "prompts": []
        })
    except Exception as e:
        return JSONResponse({"reachable": False, "error": f"Failed to parse schema: {str(e)}"})


@router.post("/admin/swagger/generate", dependencies=[Depends(require_admin)])
async def generate_mcp_from_swagger(request: Request):
    """Parses a schema and saves it as a virtual server."""
    payload = await request.json()
    api_id = payload.get("api_id")
    swagger_url = payload.get("swagger_url")
    swagger_json = payload.get("swagger_json")
    custom_headers = payload.get("headers", {})

    if not api_id: return JSONResponse({"error": "Missing Server Name"}, status_code=400)

    try:
        target_base_url, tools = await parse_swagger_schema(swagger_url, swagger_json)

        VIRTUAL_APIS[api_id] = {
            "base_url": target_base_url,
            "headers": custom_headers,
            "auto_refresh": None, 
            "tools": tools
        }
        save_virtual_apis()

        virtual_url = str(request.base_url).rstrip("/") + f"/virtual/{api_id}"
        return JSONResponse({"message": "Success", "base_url": virtual_url})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.patch("/admin/swagger/{api_id}/headers", dependencies=[Depends(require_admin)])
async def update_virtual_api_headers(api_id: str, request: Request):
    payload = await request.json()
    new_headers = payload.get("headers", {})
    auto_refresh = payload.get("auto_refresh", None)

    if api_id not in VIRTUAL_APIS: return JSONResponse({"error": f"Virtual API '{api_id}' not found"}, status_code=404)

    VIRTUAL_APIS[api_id]["headers"] = new_headers
    VIRTUAL_APIS[api_id]["auto_refresh"] = auto_refresh
    save_virtual_apis()
    return JSONResponse({"message": "Authentication updated successfully"})


@router.post("/virtual/{api_id}/mcp")
async def virtual_mcp_endpoint(api_id: str, request: Request):
    if api_id not in VIRTUAL_APIS:
        return JSONResponse(rpc_error(0, -32601, f"Virtual API '{api_id}' not found."), status_code=404)

    api_data = VIRTUAL_APIS[api_id]
    body = await request.json()
    method = body.get("method", "")
    rpc_id = body.get("id")

    if method == "initialize":
        return JSONResponse(rpc_ok(rpc_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"openapi-{api_id}", "version": "1.0.0"},
        }))

    if method == "tools/list":
        return JSONResponse(rpc_ok(rpc_id, {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in api_data["tools"]]}))

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        tool_conf = next((t for t in api_data["tools"] if t["name"] == tool_name), None)
        if not tool_conf: return JSONResponse(rpc_error(rpc_id, -32601, "Tool not found"))

        # --- JUST-IN-TIME TOKEN REFRESHER ---
        if api_data.get("auto_refresh"):
            ar = api_data["auto_refresh"]
            now = time.time()
            if now - ar.get("last_fetched", 0) > (ar.get("expiry_seconds", 3600) - 60):
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        t_resp = await client.post(ar["endpoint"], json=ar.get("payload", {}))
                        t_resp.raise_for_status()
                        t_data = t_resp.json()
                        new_token = t_data.get(ar["extract_key"])
                        if new_token:
                            api_data["headers"][ar["header_name"]] = ar.get("header_prefix", "") + str(new_token)
                            api_data["auto_refresh"]["last_fetched"] = now
                            save_virtual_apis()
                except Exception as e: print(f"Token auto-refresh failed for {api_id}: {e}")
        # ----------------------------------------

        url = api_data["base_url"].rstrip("/") + tool_conf["path"]
        args_copy = arguments.copy()

        path_vars = re.findall(r'\{(.*?)\}', url)
        for pv in path_vars:
            if pv in args_copy: url = url.replace(f"{{{pv}}}", str(args_copy.pop(pv)))

        query_params, json_body = None, None
        if "request_body" in args_copy: json_body = args_copy.pop("request_body")
        
        if args_copy:
            if tool_conf["method"] in ["GET", "DELETE"]: query_params = args_copy
            else: json_body = json_body or args_copy

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(method=tool_conf["method"], url=url, headers=api_data["headers"], params=query_params, json=json_body)
                output = f"Status: {resp.status_code}\n\n{resp.text}"
                return JSONResponse(rpc_ok(rpc_id, {"content": [{"type": "text", "text": output}]}))
        except Exception as e:
            return JSONResponse(rpc_error(rpc_id, -32000, f"API Error: {str(e)}"))

    return JSONResponse(rpc_error(rpc_id, -32601, "Method not supported"))