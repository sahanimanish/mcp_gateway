import os
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Boolean, DateTime, Integer, Text, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mcp_gateway.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


def new_id():
    return str(uuid.uuid4())[:8]


class MCPServer(Base):
    __tablename__ = "servers"

    id               = Column(String, primary_key=True, default=new_id)
    name             = Column(String, unique=True, nullable=False)
    url              = Column(String, nullable=False,unique=True)
    description      = Column(String, default="")
    upstream_key     = Column(String, default="")
    status           = Column(String, default="unknown")   # online | offline | unknown
    protocol_version = Column(String, default="")
    server_info      = Column(JSON, default=dict)          # {name, version} from initialize
    capabilities     = Column(JSON, default=dict)          # raw capabilities block
    created_at       = Column(DateTime, default=datetime.utcnow)
    last_seen        = Column(DateTime, nullable=True)

    tools     = relationship("MCPTool",     back_populates="server", cascade="all, delete-orphan")
    resources = relationship("MCPResource", back_populates="server", cascade="all, delete-orphan")
    resource_templates = relationship("MCPResourceTemplate", back_populates="server", cascade="all, delete-orphan")
    prompts   = relationship("MCPPrompt",   back_populates="server", cascade="all, delete-orphan")


class MCPTool(Base):
    __tablename__ = "tools"

    id           = Column(String, primary_key=True, default=new_id)
    server_id    = Column(String, ForeignKey("servers.id"), nullable=False)
    name         = Column(String, nullable=False)   # prefixed: server__tool
    raw_name     = Column(String, nullable=False)   # original name from server
    description  = Column(String, default="")
    input_schema = Column(JSON, default=dict)

    server = relationship("MCPServer", back_populates="tools")


class MCPResource(Base):
    __tablename__ = "resources"

    id          = Column(String, primary_key=True, default=new_id)
    server_id   = Column(String, ForeignKey("servers.id"), nullable=False)
    uri         = Column(String, nullable=False)
    name        = Column(String, default="")
    title       = Column(String, default="")
    description = Column(String, default="")
    mime_type   = Column(String, default="")
    size        = Column(Integer, nullable=True)
    icons       = Column(JSON, default=list)
    annotations = Column(JSON, default=dict)

    server = relationship("MCPServer", back_populates="resources")


class MCPResourceTemplate(Base):
    __tablename__ = "resource_templates"

    id           = Column(String, primary_key=True, default=new_id)
    server_id    = Column(String, ForeignKey("servers.id"), nullable=False)
    uri_template = Column(String, nullable=False)
    name         = Column(String, default="")
    title        = Column(String, default="")
    description  = Column(String, default="")
    mime_type    = Column(String, default="")
    icons        = Column(JSON, default=list)
    annotations  = Column(JSON, default=dict)

    server = relationship("MCPServer", back_populates="resource_templates")


class MCPPrompt(Base):
    __tablename__ = "prompts"

    id          = Column(String, primary_key=True, default=new_id)
    server_id   = Column(String, ForeignKey("servers.id"), nullable=False)
    name        = Column(String, nullable=False)
    title       = Column(String, default="")
    description = Column(String, default="")
    arguments   = Column(JSON, default=list)
    icons       = Column(JSON, default=list)

    server = relationship("MCPServer", back_populates="prompts")


class Client(Base):
    __tablename__ = "clients"

    id          = Column(String, primary_key=True, default=new_id)
    name        = Column(String, nullable=False)
    description = Column(String, default="")
    api_key     = Column(String, unique=True, nullable=False)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    permissions = relationship("Permission", back_populates="client", cascade="all, delete-orphan")


class Permission(Base):
    __tablename__ = "permissions"

    id               = Column(String, primary_key=True, default=new_id)
    client_id        = Column(String, ForeignKey("clients.id"), nullable=False)
    server_id        = Column(String, ForeignKey("servers.id"), nullable=False)
    tool_name        = Column(String, nullable=False, default="")
    permission_type  = Column(String, nullable=False, default="tool")
    permission_value = Column(String, nullable=False, default="")

    client = relationship("Client", back_populates="permissions")
    server = relationship("MCPServer")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    timestamp   = Column(DateTime, default=datetime.utcnow)
    method      = Column(String, nullable=False)
    client_name = Column(String, default="admin")
    tool        = Column(String, default="—")
    status      = Column(Integer, default=200)
    detail      = Column(Text, default="")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name() == "sqlite":
            await _apply_sqlite_migrations(conn)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def _apply_sqlite_migrations(conn):
    async def column_names(table_name: str) -> set[str]:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in result.fetchall()}

    resources_columns = await column_names("resources")
    if "title" not in resources_columns:
        await conn.exec_driver_sql("ALTER TABLE resources ADD COLUMN title VARCHAR DEFAULT ''")
    if "size" not in resources_columns:
        await conn.exec_driver_sql("ALTER TABLE resources ADD COLUMN size INTEGER")
    if "icons" not in resources_columns:
        await conn.exec_driver_sql("ALTER TABLE resources ADD COLUMN icons JSON")
    if "annotations" not in resources_columns:
        await conn.exec_driver_sql("ALTER TABLE resources ADD COLUMN annotations JSON")

    prompts_columns = await column_names("prompts")
    if "title" not in prompts_columns:
        await conn.exec_driver_sql("ALTER TABLE prompts ADD COLUMN title VARCHAR DEFAULT ''")
    if "icons" not in prompts_columns:
        await conn.exec_driver_sql("ALTER TABLE prompts ADD COLUMN icons JSON")

    permission_columns = await column_names("permissions")
    if "permission_type" not in permission_columns:
        await conn.exec_driver_sql("ALTER TABLE permissions ADD COLUMN permission_type VARCHAR DEFAULT 'tool'")
    if "permission_value" not in permission_columns:
        await conn.exec_driver_sql("ALTER TABLE permissions ADD COLUMN permission_value VARCHAR DEFAULT ''")
        await conn.exec_driver_sql("UPDATE permissions SET permission_value = tool_name WHERE permission_value = '' OR permission_value IS NULL")
