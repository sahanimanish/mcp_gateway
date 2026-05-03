KYC MCP Gateway - Technical Handoff & Knowledge Base

📖 Project Overview

The KYC MCP Gateway is an enterprise-grade middleware application that sits between MCP Clients (like Claude Desktop or custom AI agents) and upstream backend services.
It acts as a centralized control plane providing Authentication, Permission Management, Routing, a Testing Playground, and dynamic OpenAPI-to-MCP translation.

🏗️ Core Architecture

The system is built on a Python FastAPI backend using Async SQLAlchemy (SQLite by default) for persistence, and a Vanilla HTML/JS/CSS Single Page Application (SPA) for the frontend frontend.

Clients authenticate with the Gateway using an X-Api-Key header.

The Gateway routes incoming JSON-RPC MCP calls to the appropriate upstream server based on the requested tool, prompt, or resource.

Upstream Servers can be standard MCP servers (HTTP POST or SSE) OR standard REST APIs dynamically wrapped as "Virtual MCP Servers".

📂 File Manifest & Responsibilities

Backend (Python / FastAPI)

main.py: The application entry point. Mounts the routers (proxy.py, swagger.py, admin routes) and serves the static ui/ directory.

gateway/database.py: Contains the SQLAlchemy async models (Client, MCPServer, MCPTool, MCPResource, MCPPrompt, MCPResourceTemplate, Permission, ActivityLog). Note: Permissions are flattened where tool_name acts as the universal column for all capability names/URIs.

gateway/auth.py: Handles API key validation via get_client_by_key(). It returns the client object and a flattened Set of allowed capability names to ensure fast O(1) permission lookups.

gateway/proxy.py: The core routing engine. Handles /mcp endpoints for clients. Features a universal adapter that gracefully handles both SSE streams and standard application/json HTTP responses from upstream servers. Also handles dynamic routing for Resource Templates.

gateway/swagger.py: The Virtual Server Engine. Downloads OpenAPI/Swagger schemas (or accepts JSON file uploads), dynamically parses them into MCP Tools, saves them to virtual_apis.json, and exposes a /virtual/{api_id}/mcp endpoint to execute the translated REST calls.

Frontend (Vanilla JS)

ui/index.html: The UI layout. Contains modals for Server registration, Client creation, Auth/Token updates, and Swagger API imports.

ui/app.js: The SPA logic. Handles routing, permission matrix rendering, executing tools in the Playground, and background polling (silent refresh).

ui/styles.css: Design tokens and layout styling (dark mode, premium UI).

✨ Key Technical Decisions & Features

1. The "Virtual Server" Pattern (REST to MCP)

Instead of bloating the core proxy.py routing engine with REST translation logic, REST APIs are handled via a sidecar pattern within swagger.py.

Conversion: The UI posts a Swagger URL/JSON to /admin/swagger/generate. It extracts paths, methods, and schemas (flattening nested requestBody JSON into top-level MCP arguments) and saves them to virtual_apis.json.

Registration: It creates a virtual URL (e.g., http://localhost/virtual/my_api) which is seamlessly registered into the main SQLite database as a standard MCP server.

Execution: When proxy.py forwards an MCP call to this virtual URL, swagger.py intercepts it, maps the MCP arguments to REST path/query/body variables, fires the HTTP request, and wraps the REST response back into MCP format.

2. Just-In-Time (JIT) Auto-Refresh Tokens

To handle expiring OAuth/Bearer tokens for REST APIs, swagger.py implements a JIT refresher.

The UI allows admins to configure an automated token endpoint, payload, and expiry time.

Before swagger.py executes a translated REST call, it checks last_fetched vs expiry_seconds. If the token is near expiration, it pauses the tool execution, fetches a new token, updates the headers dict in memory, saves to disk, and then executes the tool.

3. Universal Proxy Parsing (_call_http)

Upstream MCP servers might return data via Server-Sent Events (SSE) or plain HTTP JSON responses. proxy.py explicitly checks the Content-Type header of the upstream response. If it contains application/json, it parses the raw bytes; if it contains text/event-stream, it yields chunks.

4. Resource Template Routing

When proxy.py processes a resources/read request (e.g., memo://note/123), an exact database match will fail. The proxy implements a fallback loop to extract the base URI from known MCPResourceTemplate rows (e.g., memo://note/{slug} -> memo://note/). If the requested URI startswith() the base URI, it routes the request to the owning server.

5. Silent Background Polling

To keep the Gateway UI live without disrupting the user experience (e.g., closing open dropdowns), app.js runs a silentBackgroundRefresh() loop every 3 seconds. It fetches new logs, server statuses, and client permissions in the background and gracefully updates the DOM only if the user is actively viewing those tables.

Instruction to AI Reading This: You now have full context of the KYC MCP Gateway project. You understand the Proxy, the Swagger/Virtual Server adapter, the JIT token refresh logic, and the UI implementation. Await user instructions.