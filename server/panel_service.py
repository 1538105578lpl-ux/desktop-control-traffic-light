"""Single local execution owner, authenticated panel API and live read-only preview."""
import asyncio
from contextlib import asynccontextmanager
import ctypes
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path

from panel_common import ROOT, RUNTIME, CONNECTION, PORT
sys.path.insert(0, str(ROOT / 'vendor'))
os.environ['ANONYMIZED_TELEMETRY'] = 'false'
os.environ['WINDOWS_MCP_WATCHDOG'] = 'off'
from control_state import ControlState, ControlBlocked, is_read_only, INPUT_TOOLS, BOUND_LEASE
from reliability import install, rows, choose
install()
from input_guard import install_input_guard
from fastmcp import Client
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import mcp.types as types
from PIL import ImageGrab, ImageDraw, Image
import win32gui
from panel_hotkey import PauseHotkey

from task_lifecycle import DesktopState
STATE = DesktopState(RUNTIME / 'control-intent.json')
HOOK_RECORDS = RUNTIME / 'hook-events.json'
PREVIEW_DEMAND = 0.0
TOKEN = secrets.token_urlsafe(32)
INSTANCE = secrets.token_hex(12)
CLIENT = None
TOOLS = []
DISPATCH = asyncio.Lock()
PREVIEW = {'bytes': None, 'rect': None, 'captured_at': None, 'error': None, 'size': None}
CONNECTED = {}
HOTKEY = PauseHotkey(STATE)
LABELS = {'WindowList': '查找窗口', 'FocusWindow': '定位操作窗口', 'Snapshot': '观察界面', 'Screenshot': '查看画面', 'Click': '点击控件', 'Type': '填写内容', 'Shortcut': '执行快捷键', 'Scroll': '滚动内容', 'Move': '移动或拖动', 'ReadFocusedInput': '核对输入', 'App': '打开或切换应用', 'WaitFor': '等待界面变化'}


def error_result(message):
    return types.CallToolResult(content=[types.TextContent(type='text', text=message)], is_error=True).model_dump(by_alias=True, exclude_none=True)


def tool_result(value):
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(value, ensure_ascii=False))], structured_content=value).model_dump(by_alias=True, exclude_none=True)


def sync_hooks():
    if hasattr(STATE, 'ingest'):
        try:
            STATE.ingest(json.loads(HOOK_RECORDS.read_text(encoding='utf-8')).get('records', []))
        except (OSError, ValueError):
            pass


async def hook_loop():
    while True:
        sync_hooks()
        await asyncio.sleep(.3)


async def execute(name, arguments, client_id):
    sync_hooks()
    admitted_generation = STATE.admission_generation()
    async with DISPATCH:
        sync_hooks()
        if name in {'BeginDesktopTask', 'EndDesktopTask'}:
            try:
                if name == 'BeginDesktopTask':
                    value = STATE.begin(client_id, arguments.get('title', '电脑操作'), admitted_generation)
                    PREVIEW.update(bytes=None, captured_at=None, error=None)
                else:
                    if admitted_generation != STATE.generation:
                        raise ControlBlocked('STALE_ACTION: this task-ending call was queued before control changed.')
                    value = STATE.end(client_id, arguments.get('status', 'ended'))
                value['events'] = value['events'][-3:]
                return tool_result(value)
            except ControlBlocked as exc:
                return error_result(str(exc))
        if name == 'ControlStatus':
            snapshot = STATE.snapshot()
            snapshot['events'] = snapshot['events'][-3:]
            return tool_result(snapshot)
        if name == 'TaskProgress':
            with STATE.lock:
                if admitted_generation != STATE.generation:
                    return error_result('STALE_ACTION: this progress update was queued before control changed.')
                if STATE.mode != 'running':
                    return error_result('CONTROL_PAUSED: task progress cannot resume desktop input.')
                if getattr(STATE, 'owner_client', client_id) != client_id:
                    return error_result('DESKTOP_BUSY: another task owns this desktop operation.')
                STATE.task = dict(title=str(arguments.get('title', '当前任务'))[:200],
                                  steps=[str(s)[:100] for s in arguments.get('steps', [])[:12]],
                                  current=int(arguments.get('current', -1)),
                                  status=arguments.get('status', 'running'))
                STATE.current_action = str(arguments.get('message') or '任务进度已更新')[:200]
                STATE.event('progress', STATE.current_action)
                if hasattr(STATE, 'persist'):
                    STATE.persist()
            return tool_result({'updated': True})
        if name not in {t['name'] for t in TOOLS}:
            return error_result('Unknown tool')
        # Shell and arbitrary executable launch could escape the input gate.
        if name == 'PowerShell' or (name == 'App' and (arguments.get('mode') == 'launch_executable' or
                any(word in str(arguments.get('name', '')).casefold() for word in ('powershell', 'terminal', 'cmd.exe', '命令提示符', '终端')))):
            return error_result('UNSUPPORTED_IN_PANEL_V1: arbitrary scripts/executables bypass pausing. Use a separate explicitly authorized file/shell workflow.')
        lease = None
        binding = None
        started = time.monotonic()
        try:
            lease = STATE.admit(client_id, name, is_read_only(name, arguments), admitted_generation)
            binding = BOUND_LEASE.set(lease)
            if name == 'FocusWindow':
                row = choose(await asyncio.to_thread(rows, True), name=arguments.get('name'),
                             process_id=arguments.get('process_id'), window_handle=arguments.get('window_handle'))
                # Selection is owned by this admitted tool; keep its epoch valid.
                with STATE.lock:
                    STATE.target = row['handle']
                    STATE.target_title = row.get('title', '')[:100]
                    if row['handle'] != lease.target:
                        STATE.observations.clear()
                        PREVIEW.update(bytes=None, captured_at=None, error=None)
            if getattr(STATE, 'owner_client', None) == client_id:
                STATE.current_action = LABELS.get(name, name)
            STATE.event('start', LABELS.get(name, name), tool=name)
            result = await CLIENT.call_tool(name, arguments)
            result_dict = types.CallToolResult(content=result.content, structured_content=result.structured_content,
                is_error=result.is_error, _meta=result.meta).model_dump(by_alias=True, exclude_none=True)
            texts = [c.text for c in result.content if c.type == 'text']
            failed = result.is_error or any(t.startswith(('Error capturing', 'Error:')) for t in texts)
            if name in {'Snapshot', 'Screenshot'} and not failed:
                if name == 'Screenshot' or arguments.get('use_vision') in (True, 'true') or arguments.get('use_ui_tree', True) in (True, 'true'):
                    STATE.observe(client_id, lease.generation)
            if name in INPUT_TOOLS and not failed:
                with STATE.lock:
                    STATE.observations.clear()
            STATE.event('error' if failed else 'done', ('工具报告错误' if failed else '操作返回，结果以界面核验为准'),
                        tool=name, duration_ms=round((time.monotonic() - started) * 1000))
            return result_dict
        except Exception as exc:
            STATE.event('blocked' if isinstance(exc, ControlBlocked) else 'error', str(exc)[:350], tool=name)
            return error_result(str(exc))
        finally:
            if lease:
                STATE.finish(lease)
            if binding is not None:
                BOUND_LEASE.reset(binding)


def capture_preview():
    with STATE.lock:
        handle = STATE.target
    if not handle:
        return {'bytes': None, 'error': None, 'rect': None, 'size': None, 'captured_at': None}
    if not win32gui.IsWindow(handle) or win32gui.IsIconic(handle):
        raise ValueError('窗口已关闭或最小化，选择可见窗口后继续')
    left, top, right, bottom = win32gui.GetWindowRect(handle)
    if right <= left or bottom <= top:
        raise ValueError('窗口没有可预览区域')
    picture = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    picture.thumbnail((1440, 1000))
    stream = io.BytesIO()
    picture.convert('RGB').save(stream, format='JPEG', quality=78)
    return dict(bytes=stream.getvalue(), rect=[left, top, right, bottom], size=list(picture.size), captured_at=time.time(), error=None, target=handle)


async def preview_loop():
    while True:
        if time.monotonic() - PREVIEW_DEMAND > 3 or STATE.mode == 'takeover':
            await asyncio.sleep(.4)
            continue
        try:
            result = await asyncio.to_thread(capture_preview)
            with STATE.lock:
                if result.get('target') == STATE.target:
                    PREVIEW.update(result)
        except Exception as exc:
            PREVIEW.update(bytes=None, error=str(exc)[:180], captured_at=None)
        await asyncio.sleep(.65)


@asynccontextmanager
async def lifespan(app):
    global CLIENT, TOOLS
    install_input_guard(STATE)
    from windows_mcp.__main__ import _build_mcp
    CLIENT = _build_mcp()
    # Keep the existing backend lifespan open; invoke its public call_tool API
    # directly so the cancellation lease follows each worker's contextvars.
    async with Client(CLIENT) as lifecycle_client:
        registered = await lifecycle_client.list_tools()
        TOOLS = [t.model_dump(by_alias=True, exclude_none=True) for t in registered if t.name != 'PowerShell']
        TOOLS += [
            {'name': 'BeginDesktopTask', 'description': 'Start desktop work automatically for the recent WorkBuddy user task. Call first, then locate the app yourself. A pause can only be released after the human explicitly says 继续 in the same WorkBuddy task. This tool cannot grant itself resume permission.', 'inputSchema': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']}},
            {'name': 'EndDesktopTask', 'description': 'End this desktop operation after checking the actual UI, or report a failure. Does not claim all WorkBuddy tasks are complete and cannot clear a human pause.', 'inputSchema': {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['ended', 'failed']}}, 'required': ['status']}},
            {'name': 'ControlStatus', 'description': 'Read local panel mode, task progress and selected window. Never resume automatically.', 'inputSchema': {'type': 'object', 'properties': {}}, 'annotations': {'readOnlyHint': True}},
            {'name': 'TaskProgress', 'description': 'Report the current user task and real step progress to the panel. Describe intent before operating; mark completed only after UI verification. No desktop input.', 'inputSchema': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'steps': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12}, 'current': {'type': 'integer'}, 'status': {'type': 'string', 'enum': ['running', 'waiting', 'completed', 'failed']}, 'message': {'type': 'string'}}, 'required': ['title', 'steps', 'current', 'status']}}
        ]
        RUNTIME.mkdir(exist_ok=True)
        temp = CONNECTION.with_suffix('.tmp')
        temp.write_text(json.dumps({'token': TOKEN, 'instance': INSTANCE, 'pid': os.getpid(), 'port': PORT}), encoding='utf-8')
        temp.replace(CONNECTION)
        STATE.event('control', '红绿灯电脑控制已就绪，等待 WorkBuddy 任务')
        sync_hooks()
        HOTKEY.start()
        preview = asyncio.create_task(preview_loop())
        hooks = asyncio.create_task(hook_loop())
        try:
            yield
        finally:
            if STATE.mode == 'running':
                STATE.transition('paused')
            elif hasattr(STATE, 'persist'):
                STATE.persist()
            HOTKEY.stop()
            hooks.cancel()
            preview.cancel()
            try:
                await hooks
            except asyncio.CancelledError:
                pass
            try:
                await preview
            except asyncio.CancelledError:
                pass


async def health(request):
    return JSONResponse({'service': 'workbuddy-control-panel', 'ready': CLIENT is not None, 'instance': INSTANCE})


async def status(request):
    value = STATE.snapshot()
    value['preview'] = {k: v for k, v in PREVIEW.items() if k != 'bytes'}
    value['connections'] = [dict(client_id=k, **v) for k, v in CONNECTED.items() if time.time() - v['seen'] < 35]
    value['global_pause_hotkey'] = HOTKEY.registered
    return JSONResponse(value)


async def screenshot(request):
    global PREVIEW_DEMAND
    PREVIEW_DEMAND = time.monotonic()
    if STATE.mode == 'takeover':
        return JSONResponse({'error': '接管时画面已收起'}, status_code=409)
    if PREVIEW['bytes'] is None:
        return JSONResponse({'error': PREVIEW['error'] or '请选择窗口'}, status_code=409)
    if request.query_params.get('format') == 'png':
        picture = Image.open(io.BytesIO(PREVIEW['bytes']))
        picture.thumbnail((354, 194))
        stream = io.BytesIO()
        picture.save(stream, format='PNG')
        return Response(stream.getvalue(), media_type='image/png', headers={'Cache-Control': 'no-store'})
    return Response(PREVIEW['bytes'], media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


async def control(request):
    body = await request.json()
    action = body.get('action')
    if action == 'resume':
        return JSONResponse({'error': '请在原 WorkBuddy 任务中明确说“继续”来恢复电脑操作'}, status_code=409)
    if action in {'pause', 'takeover', 'stop'}:
        STATE.transition({'pause': 'paused', 'takeover': 'takeover', 'stop': 'stopped'}[action])
    else:
        return JSONResponse({'error': 'Unknown action'}, status_code=400)
    return await status(request)


async def list_tools(request):
    return JSONResponse(TOOLS)


async def heartbeat(request):
    body = await request.json()
    CONNECTED[str(body.get('client_id', 'unknown'))[:64]] = dict(pid=body.get('pid'), seen=time.time())
    return JSONResponse({'ok': True})


async def call_tool(request):
    body = await request.json()
    return JSONResponse(await execute(str(body['name']), body.get('arguments', {}), str(body.get('client_id', 'unknown'))))


class LocalAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('ascii', errors='ignore')
        origin = headers.get(b'origin', b'').decode('ascii', errors='ignore')
        expected = f'http://127.0.0.1:{PORT}'
        allowed_host = host == f'127.0.0.1:{PORT}'
        if not allowed_host or (origin and origin != expected):
            return await JSONResponse({'error': 'Local origin required'}, status_code=403)(scope, receive, send)
        if scope['path'].startswith('/api/'):
            token = headers.get(b'x-control-token', b'').decode('ascii', errors='ignore')
            if not secrets.compare_digest(token, TOKEN):
                return await JSONResponse({'error': 'Local control token required'}, status_code=401)(scope, receive, send)
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([
                    (b'x-content-type-options', b'nosniff'), (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'same-origin'), (b'cache-control', b'no-store'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")])
            await send(message)
        await self.app(scope, receive, secure_send)


APP = LocalAuth(Starlette(lifespan=lifespan, routes=[
    Route('/api/health', health), Route('/api/status', status),
    Route('/api/preview', screenshot), Route('/api/control', control, methods=['POST']),
    Route('/api/tools', list_tools), Route('/api/heartbeat', heartbeat, methods=['POST']), Route('/api/tool', call_tool, methods=['POST'])]))


def main():
    # Named mutex prevents multiple gateway processes from spawning multiple owners.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    owner = kernel.CreateMutexW(None, False, 'Local\\WorkBuddyControlPanelV1')
    if not owner or ctypes.get_last_error() == 183:
        return
    import uvicorn
    try:
        uvicorn.run(APP, host='127.0.0.1', port=PORT, log_level='warning', access_log=False)
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(owner)


if __name__ == '__main__':
    main()
