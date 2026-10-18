"""Interprocess-safe state updates and aggregation for concurrent WorkBuddy sessions."""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
import msvcrt


@contextmanager
def locked(path):
    file = open(str(path) + '.lock', 'a+b')
    try:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        deadline = time.monotonic() + 1.5
        while True:
            try:
                file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.015)
        try:
            yield
        finally:
            file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        file.close()


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=Path(path).name + '.', dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def aggregate(sessions, now):
    recent = [s for s in sessions.values() if now - s.get('ts', 0) < 3600]
    working = [s for s in recent if s.get('state') == 'working']
    waiting = [s for s in recent if s.get('state') == 'waiting']
    active = waiting or working
    chosen = max(active or recent, key=lambda s: s['ts']) if recent else {'state': 'idle', 'ts': now}
    value = dict(chosen)
    value.update(sessions=sessions, working_count=len(working), waiting_count=len(waiting))
    if waiting:
        value['state'] = 'waiting'
    elif working:
        value['state'] = 'working'
    return value


def update_state(path, payload):
    with locked(path):
        previous = load(path)
        sessions = previous.get('sessions', {})
        if not sessions and previous.get('session_id'):
            sessions[previous['session_id']] = {k: v for k, v in previous.items() if k != 'sessions'}
        key = payload.get('session_id') or payload.get('cwd') or '_default'
        old = sessions.get(key, {})
        event = payload.get('event')
        if event in {'Setup', 'SessionStart'} and old.get('state') in {'working', 'waiting'}:
            payload = {**payload, 'state': old['state']}
        merged = {**old, **payload}
        sessions[key] = merged
        sessions = {k: v for k, v in sessions.items() if payload['ts'] - v.get('ts', 0) < 86400}
        result = aggregate(sessions, payload['ts'])
        atomic_write(path, result)
        return result


def record_hook(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    event = data.get('hook_event_name')
    if event not in {'UserPromptSubmit', 'Stop', 'StopFailure', 'SessionEnd'}:
        return
    item = dict(id=f'{time.time_ns()}-{os.getpid()}', event=event, ts=time.time(),
                session_id=data.get('session_id') or '', cwd=data.get('cwd') or '')
    if event == 'UserPromptSubmit':
        item['prompt'] = str(data.get('prompt') or '')[:4000]
    with locked(path):
        records = load(path).get('records', [])
        records.append(item)
        atomic_write(path, {'records': records[-100:]})
