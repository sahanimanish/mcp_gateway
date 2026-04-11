from sqlalchemy import Column, String, Boolean, DateTime, Integer, Text, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from datetime import datetime
import uuid

DATABASE_URL = "sqlite+aiosqlite:///./mcp_gateway.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


def new_id():
    return str(uuid.uuid4())[:8]


class MCPServer(Base):
    __tablename__ = "servers"

    id               = Column(String, primary_key=True, default=new_id)
    name             = Column(String, unique=True, nullable=False)
    url              = Column(String, nullable=False)
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
    description = Column(String, default="")
    mime_type   = Column(String, default="")

    server = relationship("MCPServer", back_populates="resources")


class MCPPrompt(Base):
    __tablename__ = "prompts"

    id          = Column(String, primary_key=True, default=new_id)
    server_id   = Column(String, ForeignKey("servers.id"), nullable=False)
    name        = Column(String, nullable=False)
    description = Column(String, default="")
    arguments   = Column(JSON, default=list)

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

    id        = Column(String, primary_key=True, default=new_id)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    server_id = Column(String, ForeignKey("servers.id"), nullable=False)
    tool_name = Column(String, nullable=False)

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


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
