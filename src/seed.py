"""
seed.py — populate the gateway database with demo servers and clients.
Run once:  python seed.py
"""
import asyncio
from gateway.database import init_db, AsyncSessionLocal, MCPServer, MCPTool, Client, Permission, new_id


SERVERS = [
    {
        "name": "gdrive",
        "url": "http://localhost:8001",
        "description": "Google Drive files & docs",
        "tools": ["search_files", "read_file", "write_file", "list_folders"],
    },
    {
        "name": "github",
        "url": "http://localhost:8002",
        "description": "GitHub repos, PRs & issues",
        "tools": ["list_prs", "create_issue", "merge_pr", "list_repos"],
    },
    {
        "name": "database",
        "url": "http://localhost:8003",
        "description": "PostgreSQL read/write access",
        "tools": ["query", "list_tables", "insert", "delete"],
    },
]

CLIENTS = [
    {
        "name": "Data Analyst",
        "description": "BI team, read-only data access",
        "api_key": "key-analyst-xk9p2m",
        "perms": {
            "gdrive":   ["search_files", "read_file"],
            "database": ["query", "list_tables"],
        },
    },
    {
        "name": "DevOps Bot",
        "description": "CI/CD automation agent",
        "api_key": "key-devops-r7t3qw",
        "perms": {
            "github": ["list_prs", "create_issue", "merge_pr"],
        },
    },
    {
        "name": "Read-only Bot",
        "description": "Monitoring & reporting",
        "api_key": "key-readonly-9a2bx",
        "perms": {
            "gdrive":   ["search_files"],
            "database": ["list_tables"],
        },
    },
]


async def seed():
    await init_db()
    async with AsyncSessionLocal() as db:
        # ── servers ──
        server_map = {}   # name → id
        for s in SERVERS:
            srv = MCPServer(
                id=new_id(),
                name=s["name"],
                url=s["url"],
                description=s["description"],
                status="online",
            )
            db.add(srv)
            await db.flush()
            server_map[s["name"]] = srv.id
            for t in s["tools"]:
                prefixed = f"{s['name']}__{t}"
                db.add(MCPTool(id=new_id(), server_id=srv.id, name=prefixed, raw_name=t))

        await db.flush()

        # ── clients ──
        for c in CLIENTS:
            cli = Client(
                id=new_id(),
                name=c["name"],
                description=c["description"],
                api_key=c["api_key"],
            )
            db.add(cli)
            await db.flush()
            for srv_name, tools in c["perms"].items():
                sid = server_map.get(srv_name)
                if not sid:
                    continue
                for t in tools:
                    prefixed = f"{srv_name}__{t}"
                    db.add(Permission(
                        id=new_id(),
                        client_id=cli.id,
                        server_id=sid,
                        tool_name=prefixed,
                        permission_type="tool",
                        permission_value=prefixed,
                    ))

        await db.commit()
        print("✓ Seeded 3 servers, 3 clients, and permissions.")
        print("\nDemo API keys:")
        for c in CLIENTS:
            print(f"  {c['name']:20s}  {c['api_key']}")


if __name__ == "__main__":
    asyncio.run(seed())
