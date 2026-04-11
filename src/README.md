# MCP Gateway

A production-ready HTTP gateway for the Model Context Protocol.  
Connect multiple MCP servers, control exactly which clients can call which tools, and manage everything from a built-in admin UI.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed demo data (optional — gives you 3 servers and 3 clients to play with)
python seed.py

# 3. Start the gateway
python main.py
# or
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** → Admin UI  
Open **http://localhost:8000/docs** → Swagger API docs

---

## Environment variables

| Variable    | Default                      | Description                          |
|-------------|------------------------------|--------------------------------------|
| `ADMIN_KEY` | `admin-secret-change-me`     | Key required for all `/admin/*` calls |
| `DATABASE_URL` | `sqlite+aiosqlite:///./mcp_gateway.db` | SQLAlchemy async DB URL |

Set them in a `.env` file or export before running:
```bash
export ADMIN_KEY="my-secure-admin-key"
```

---

## Project structure

```
mcp-gateway/
├── main.py                  # FastAPI app, startup, routing
├── seed.py                  # Populate demo data
├── example_upstream.py      # Minimal test MCP server
├── requirements.txt
├── mcp_gateway.db           # SQLite DB (auto-created)
├── ui/
│   └── index.html           # Admin UI (served at /)
└── gateway/
    ├── __init__.py
    ├── database.py          # SQLAlchemy models + async engine
    ├── schemas.py           # Pydantic request/response models
    ├── auth.py              # API key validation middleware
    ├── registry.py          # In-memory tool → server URL map
    ├── admin.py             # Admin CRUD API (/admin/*)
    └── proxy.py             # MCP JSON-RPC proxy (/mcp)
```

---

## How it works

```
Client  →  POST /mcp  (X-Api-Key: key-xxx)
             │
             ├── Auth middleware validates key → loads allowed tools
             ├── initialize      → returns gateway capabilities
             ├── tools/list      → returns only permitted tools
             └── tools/call
                   ├── Check tool in allowed_tools → 403 if not
                   ├── Look up server URL from registry
                   └── Forward full JSON-RPC body → upstream /mcp
```

---

## Admin API reference

All admin endpoints require `X-Admin-Key` header.

### Servers
| Method | Path | Description |
|--------|------|-------------|
| GET    | `/admin/servers` | List all servers |
| POST   | `/admin/servers` | Register a server |
| PATCH  | `/admin/servers/{id}` | Update server URL/status |
| DELETE | `/admin/servers/{id}` | Remove server (cascades tools + perms) |
| POST   | `/admin/servers/{id}/refresh-tools` | Re-discover tools from upstream |

### Clients
| Method | Path | Description |
|--------|------|-------------|
| GET    | `/admin/clients` | List all clients |
| POST   | `/admin/clients` | Add client |
| PATCH  | `/admin/clients/{id}` | Update name / deactivate |
| DELETE | `/admin/clients/{id}` | Remove client |

### Permissions
| Method | Path | Description |
|--------|------|-------------|
| GET    | `/admin/clients/{id}/permissions` | Get client's permissions |
| PUT    | `/admin/clients/{id}/permissions` | Replace all permissions |
| GET    | `/admin/permissions/matrix` | Full matrix: all clients × servers × tools |

### Other
| Method | Path | Description |
|--------|------|-------------|
| GET    | `/admin/stats` | Counts of servers/clients/tools/permissions |
| GET    | `/admin/logs` | Activity log (last 100 entries) |
| DELETE | `/admin/logs` | Clear log |
| GET    | `/admin/export` | Export full config as JSON |
| GET    | `/health` | Gateway health + registry tool count |

---

## Testing with example upstream servers

Start three test MCP servers in separate terminals:

```bash
python example_upstream.py --name gdrive   --port 8001
python example_upstream.py --name github   --port 8002
python example_upstream.py --name database --port 8003
```

Then test the gateway:

```bash
# Initialize
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: key-analyst-xk9p2m" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# List visible tools
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: key-analyst-xk9p2m" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# Call an allowed tool
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: key-analyst-xk9p2m" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"database__query","arguments":{"message":"SELECT 1"}}}'

# Try a forbidden tool → 403
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: key-analyst-xk9p2m" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"database__delete","arguments":{}}}'
```

---

## Connecting Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "my-gateway": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/mcp-client-http"],
      "env": {
        "MCP_SERVER_URL": "http://localhost:8000/mcp",
        "MCP_API_KEY": "key-analyst-xk9p2m"
      }
    }
  }
}
```

---

## Production checklist

- [ ] Change `ADMIN_KEY` to a strong random value
- [ ] Set `DATABASE_URL` to PostgreSQL for multi-instance deployments
- [ ] Put Nginx in front and terminate TLS
- [ ] Tighten `CORSMiddleware` `allow_origins` to your UI domain
- [ ] Add rate limiting (e.g. `slowapi`) to `/mcp`
- [ ] Rotate client API keys periodically
- [ ] Set up log shipping (the `activity_logs` table → your SIEM)
