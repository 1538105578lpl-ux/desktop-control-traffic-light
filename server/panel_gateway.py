"""WorkBuddy stdio MCP facade. All actions go to the single local owner."""
import asyncio
import json
import os
import uuid
from panel_common import ensure_service, request_json, read_connection
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


async def run():
    connection = await asyncio.to_thread(ensure_service)
    client_id = str(uuid.uuid4())

    def refresh_connection():
        nonlocal connection, client_id
        current = read_connection()
        if current['instance'] != connection['instance']:
            connection = current
            client_id = str(uuid.uuid4())

    async def heartbeat():
        while True:
            try:
                refresh_connection()
                await asyncio.to_thread(request_json, '/api/heartbeat', connection, {'client_id': client_id, 'pid': os.getpid()})
            except Exception:
                pass
            await asyncio.sleep(10)

    async def list_tools(ctx, params):
        refresh_connection()
        tools = await asyncio.to_thread(request_json, '/api/tools', connection)
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def call_tool(ctx, params):
        try:
            refresh_connection()
            result = await asyncio.to_thread(request_json, '/api/tool', connection,
                                            {'name': params.name, 'arguments': params.arguments or {}, 'client_id': client_id}, 150)
            return types.CallToolResult.model_validate(result)
        except Exception as exc:
            # Never replay a timed-out action: input may have already happened.
            return types.CallToolResult(content=[types.TextContent(type='text', text=f'CONTROL_SERVICE_UNAVAILABLE: {exc}. Do not retry input blindly; reconnect then observe.')], is_error=True)

    server = Server('windows-mcp', version='0.8.5-traffic-light-v2', instructions=(
        'Desktop tools use the existing WorkBuddy traffic light. Start with BeginDesktopTask; locate the app automatically. No website, window picker or manual start is needed. '
        'Use TaskProgress for actual steps and EndDesktopTask after checking the UI. On CONTROL_PAUSED/CONTROL_INTERRUPTED stop; do not resume or poll. Only a fresh human 继续 in the original WorkBuddy task permits BeginDesktopTask to resume. '
        'After user resumes, get a new Snapshot/Screenshot before input. FocusWindow selects the target. '
        'Each input consumes the observation; reobserve after input. FocusWindow immediately before clicking. '
        'No arbitrary PowerShell or executable launch in panel mode.'), on_list_tools=list_tools, on_call_tool=call_tool)
    beat = asyncio.create_task(heartbeat())
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass


def main():
    asyncio.run(run())


if __name__ == '__main__':
    main()
