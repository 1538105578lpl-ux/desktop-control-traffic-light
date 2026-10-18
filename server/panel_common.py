"""Shared local connection paths. No external network or new model API needed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'panel-runtime'
CONNECTION = RUNTIME / 'connection.json'
PORT = 18765
BASE = f'http://127.0.0.1:{PORT}'


def request_json(path, connection, payload=None, timeout=10):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = Request(BASE + path, data=body, headers={'X-Control-Token': connection['token'], 'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def read_connection():
    return json.loads(CONNECTION.read_text(encoding='utf-8'))


def ensure_service():
    try:
        connection = read_connection()
        result = request_json('/api/health', connection, timeout=1)
        if result.get('service') == 'workbuddy-control-panel':
            return connection
    except (OSError, ValueError, KeyError):
        pass
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / 'service.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'panel_service.py')],
                         cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            connection = read_connection()
            if request_json('/api/health', connection, timeout=1).get('ready'):
                return connection
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.3)
    raise RuntimeError('Control panel did not start. See panel-runtime/service.log.')
