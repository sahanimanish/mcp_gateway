from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime

# ── Tool ────────────────────────────────────────────────────────
class ToolOut(BaseModel):
    id:           str
    name:         str
    raw_name:     str
    description:  str
    input_schema: dict

    class Config:
        from_attributes = True

# ── Resource ────────────────────────────────────────────────────
class ResourceOut(BaseModel):
    id:          str
    uri:         str
    name:        str
    description: str
    mime_type:   str

    class Config:
        from_attributes = True

# ── Resource Template ───────────────────────────────────────────
class ResourceTemplateOut(BaseModel):
    id:           str
    uri_template: str
    name:         str
    description:  str
    mime_type:    str

    class Config:
        from_attributes = True

# ── Prompt ──────────────────────────────────────────────────────
class PromptOut(BaseModel):
    id:          str
    name:        str   # 🔴 FIXED: Pydantic will look for 'name', not 'title'
    description: str
    arguments:   list

    class Config:
        from_attributes = True

# ── Server ──────────────────────────────────────────────────────
class ServerCreate(BaseModel):
    name:         str
    url:          str
    description:  Optional[str] = ""
    upstream_key: Optional[str] = ""

class ServerUpdate(BaseModel):
    url:          Optional[str] = None
    description:  Optional[str] = None
    upstream_key: Optional[str] = None
    status:       Optional[str] = None

class ServerOut(BaseModel):
    id:               str
    name:             str
    url:              str
    description:      str
    status:           str
    protocol_version: str
    server_info:      dict
    capabilities:     dict
    created_at:       datetime
    last_seen:        Optional[datetime]
    tools:              List[ToolOut]             = []
    resources:          List[ResourceOut]         = []
    resource_templates: List[ResourceTemplateOut] = []
    prompts:            List[PromptOut]           = []

    class Config:
        from_attributes = True

class DiscoveryPreview(BaseModel):
    reachable:               bool
    error:                   str = ""
    protocol_version:        str
    server_info:             dict
    capabilities:            dict
    tool_count:              int
    resource_count:          int
    prompt_count:            int
    resource_template_count: int = 0
    tools:              List[dict] = []
    resources:          List[dict] = []
    prompts:            List[dict] = []
    resource_templates: List[dict] = []

# ── Client ──────────────────────────────────────────────────────
class ClientCreate(BaseModel):
    name:        str
    description: Optional[str] = ""
    api_key:     str

class ClientUpdate(BaseModel):
    name:        Optional[str]  = None
    description: Optional[str]  = None
    is_active:   Optional[bool] = None

class ClientOut(BaseModel):
    id:          str
    name:        str
    description: str
    api_key:     str
    is_active:   bool
    created_at:  datetime

    class Config:
        from_attributes = True

# ── Permissions ─────────────────────────────────────────────────
class PermissionSet(BaseModel):
    permissions: dict[str, List[str]]   # server_id → [tool_name, ...]

class ClientPermsSummary(BaseModel):
    client_id:   str
    client_name: str
    permissions: dict[str, List[str]]

# ── Logs ────────────────────────────────────────────────────────
class LogOut(BaseModel):
    id:          int
    timestamp:   datetime
    method:      str
    client_name: str
    tool:        str
    status:      int
    detail:      str

    class Config:
        from_attributes = True

# ── Stats ────────────────────────────────────────────────────────
class StatsOut(BaseModel):
    servers:     int
    clients:     int
    tools:       int
    resources:   int
    prompts:     int
    permissions: int

# ── MCP JSON-RPC ─────────────────────────────────────────────────
class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    id:      Optional[int | str] = None
    method:  str
    params:  Optional[dict] = {}