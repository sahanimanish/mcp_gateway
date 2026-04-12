import requests
import json

# Configuration
url = "http://localhost:8000/mcp"
headers = {
    "Content-Type": "application/json",
    "X-Api-Key": "key-q4bvxn-ik788n"
}

# JSON-RPC Payload
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "test__ping",
        "arguments": {
            "message": "SELECT 1"
        }
    }
}

try:
    # Sending the POST request
    response = requests.post(url, headers=headers, json=payload)
    
    # Raise an exception for HTTP error codes (4xx, 5xx)
    response.raise_for_status()
    
    # Parse and print the JSON response
    result = response.json()
    print(json.dumps(result, indent=2))

except requests.exceptions.RequestException as e:
    print(f"An error occurred: {e}")

from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams,StreamableHTTPConnectionParams

# URL of your MCP Server container.
# If you are using docker-compose, this might be 'http://<service_name>:<port>'
# For example: 'http://mcp-server:8081'
mcp_server_url = "http://localhost:8000/mcp"  # Replace with your server's actual network address and port

mcp_toolset = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(url=mcp_server_url,
                                          headers={"X-Api-Key": "key-q4bvxn-ik788n",
                                                   "Content-Type": "application/json"},),
    
)

# You can then add this toolset to your agent, for example:
# agent = LlmAgent(..., tools=[mcp_toolset])
from google.adk.agents.llm_agent import Agent
from google.adk.models.google_llm import Gemini
from google.adk.tools.google_search_tool import GoogleSearchTool , enterprise_search_tool

root_agent = Agent(
    model=Gemini(
        model="gemini-flash-latest",
        # use_interactions_api=True,  # Enable Interactions API
    ),
    name="interactions_test_agent",
    tools=[
        mcp_toolset],
)

from dotenv import load_dotenv


import asyncio
import os

from google.genai import types
from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService # Optional
from google.adk.planners import BasePlanner, BuiltInPlanner, PlanReActPlanner
from google.adk.models import LlmRequest

from google.genai.types import ThinkingConfig
from google.genai.types import GenerateContentConfig

import datetime
from zoneinfo import ZoneInfo
# Session and Runner
AGENT_NAME = "calculator_agent"
APP_NAME = "calculator"
USER_ID = "user1234"
SESSION_ID = "session_code_exec_async"
GEMINI_MODEL = "gemini-2.0-flash"

session_service = InMemorySessionService()
session = await session_service.create_session(
    app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
)
runner = Runner(agent=root_agent, app_name=APP_NAME,
                session_service=session_service)

# Agent Interaction (Async)
async def call_agent_async(query):
    content = types.Content(role="user", parts=[types.Part(text=query)])
    print(f"\n--- Running Query: {query} ---")
    final_response_text = "No final text response captured."
    try:
        # Use run_async
        async for event in runner.run_async(
            user_id=USER_ID, session_id=SESSION_ID, new_message=content
        ):
            print(f"Event ID: {event.id}, Author: {event.author}")

            # --- Check for specific parts FIRST ---
            has_specific_part = False
            if event.content and event.content.parts:
                for part in event.content.parts:  # Iterate through all parts
                    if part.executable_code:
                        # Access the actual code string via .code
                        print(
                            f"  Debug: Agent generated code:\n```python\n{part.executable_code.code}\n```"
                        )
                        has_specific_part = True
                    elif part.code_execution_result:
                        # Access outcome and output correctly
                        print(
                            f"  Debug: Code Execution Result: {part.code_execution_result.outcome} - Output:\n{part.code_execution_result.output}"
                        )
                        has_specific_part = True
                    # Also print any text parts found in any event for debugging
                    elif part.text and not part.text.isspace():
                        print(f"  Text: '{part.text.strip()}'")
                        # Do not set has_specific_part=True here, as we want the final response logic below

            # --- Check for final response AFTER specific parts ---
            # Only consider it final if it doesn't have the specific code parts we just handled
            if not has_specific_part and event.is_final_response():
                if (
                    event.content
                    and event.content.parts
                    and event.content.parts[0].text
                ):
                    final_response_text = event.content.parts[0].text.strip()
                    print(f"==> Final Agent Response: {final_response_text}")
                else:
                    print(
                        "==> Final Agent Response: [No text content in final event]")

    except Exception as e:
        print(f"ERROR during agent run: {e}")
    print("-" * 30)


# Main async function to run the examples
async def main():
    await call_agent_async("Calculate the value of (5 + 7) * 3")
    

await main()