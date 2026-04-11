"""
MCP Gateway — Main Application
================================
Run:  uvicorn main:app --reload --host 0.0.0.0 --port 8000

Admin UI:     http://localhost:8000/
Gateway MCP:  POST http://localhost:8000/mcp   (requires X-Api-Key header)
Admin API:    http://localhost:8000/admin/*     (requires X-Admin-Key header)
API Docs:     http://localhost:8000/docs
"""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from gateway.database import init_db, AsyncSessionLocal
from gateway.admin import router as admin_router
from gateway.proxy import router as proxy_router
from gateway import registry

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("main")


# ── Startup / Shutdown ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database...")
    await init_db()

    logger.info("Loading tool registry from DB...")
    async with AsyncSessionLocal() as db:
        await registry.load_from_db(db)

    logger.info(f"Gateway ready — {len(registry.all_tools())} tools in registry")
    yield
    logger.info("Gateway shutting down")


# ── App ─────────────────────────────────────────────────────────

app = FastAPI(
    title="MCP Gateway",
    description="Multi-server MCP gateway with per-client permission control",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ─────────────────────────────────────────────────────

app.include_router(admin_router)
app.include_router(proxy_router)
# ── Serve Static Assets ──────────────────────────────────────────

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
# ── Serve Admin UI ───────────────────────────────────────────────

UI_FILE = os.path.join(os.path.dirname(__file__), "ui", "index.html")

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_ui():
    if os.path.exists(UI_FILE):
        with open(UI_FILE,encoding='utf-8') as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>UI not found — place index.html in ui/</h2>", status_code=404)


# ── Health ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tools_in_registry": len(registry.all_tools()),
    }


# ── Dev entrypoint ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
