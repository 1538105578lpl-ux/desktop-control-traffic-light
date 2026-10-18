"""Thread-safe admission, cancellation and observation leases for desktop tools."""
from contextlib import contextmanager
from dataclasses import dataclass
from contextvars import ContextVar
import threading
import time


class ControlBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    generation: int
    client_id: str
    tool: str
    target: int | None


INPUT_TOOLS = {'Click', 'Type', 'Move', 'Scroll', 'Shortcut', 'MultiSelect', 'MultiEdit'}
READ_TOOLS = {'Snapshot', 'Screenshot', 'WindowList', 'ReadFocusedInput', 'DisplayInventory', 'Wait', 'WaitFor', 'ControlStatus'}
BOUND_LEASE = ContextVar('desktop_control_lease', default=None)


class ControlState:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.mode = 'paused'
        self.generation = 0
        self.target = None
        self.observations = {}
        self.active = None
        self.events = []
        self.sequence = 0
        self.task = {'title': '等待 WorkBuddy 任务', 'steps': [], 'current': -1, 'status': 'idle'}
        self.cleanup = lambda: None

    def event(self, kind, message, **fields):
        with self.lock:
            self.sequence += 1
            self.events.append(dict(id=self.sequence, time=time.time(), kind=kind, message=message, **fields))
            self.events = self.events[-150:]

    def transition(self, mode):
        if mode not in {'running', 'paused', 'takeover', 'stopped'}:
            raise ValueError('Unknown control mode')
        with self.lock:
            # A second continue must not invalidate a tool already running.
            if self.mode == mode:
                return
            self.mode = mode
            self.generation += 1
            self.observations.clear()
            if mode != 'running':
                self.cleanup()
            labels = {'running': '已允许执行，请重新观察界面', 'paused': '已暂停后续输入', 'takeover': '你正在接管，AI 输入已阻止', 'stopped': '已结束，后续操作已阻止'}
            self.event('control', labels[mode])

    def select_target(self, handle):
        with self.lock:
            if self.target != handle:
                self.generation += 1
                self.observations.clear()
                self.cleanup()
                self.target = handle
                self.event('target', '已选择操作窗口')

    def observe(self, client_id, generation):
        with self.lock:
            if generation == self.generation:
                self.observations[client_id] = (generation, self.clock())

    def admission_generation(self):
        with self.lock:
            return self.generation

    def admit(self, client_id, name, read_only, submitted_generation):
        with self.lock:
            if self.active is not None:
                raise ControlBlocked('BUSY: another operation is still finishing. Do not retry input blindly.')
            if not read_only:
                if self.mode != 'running':
                    raise ControlBlocked('CONTROL_PAUSED: user paused/took over/ended control. Stop input. Wait for a new user instruction; do not poll or resume automatically.')
                if submitted_generation != self.generation:
                    raise ControlBlocked('STALE_ACTION: control changed while this call was queued. Call was discarded; reobserve before deciding a new action.')
            if name in INPUT_TOOLS:
                observed = self.observations.get(client_id)
                if not observed or observed[0] != self.generation or self.clock() - observed[1] > 90:
                    raise ControlBlocked('FRESH_OBSERVATION_REQUIRED: get a successful Snapshot or Screenshot in this MCP session after resuming/selecting a window.')
                if not self.target:
                    raise ControlBlocked('TARGET_REQUIRED: use WindowList and FocusWindow, or select a window in the panel.')
                self.observations.pop(client_id, None)
            lease = Lease(self.generation, client_id, name, self.target)
            self.active = lease
            return lease

    def check(self, lease=None):
        with self.lock:
            lease = lease if lease is not None else BOUND_LEASE.get()
            if lease is None or self.active is not lease or self.mode != 'running' or lease.generation != self.generation:
                raise ControlBlocked('CONTROL_INTERRUPTED: no more input. Observe current state before any retry; previous action may be partially completed.')
            return lease

    @contextmanager
    def atomic_input(self):
        # Pause and OS injection have a definite order: pause takes this same lock.
        with self.lock:
            lease = self.check()
            yield lease

    def finish(self, lease):
        with self.lock:
            if self.active is lease:
                self.cleanup()
                self.active = None

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, generation=self.generation, target=self.target,
                        active=self.active.tool if self.active else None, task=dict(self.task),
                        events=list(self.events), sequence=self.sequence)


def is_read_only(name, args):
    if name in READ_TOOLS:
        return True
    if name == 'Clipboard':
        return args.get('mode') == 'get'
    if name == 'FileSystem':
        return args.get('mode') in {'read', 'list', 'search', 'info'}
    if name == 'Registry':
        return args.get('mode') in {'get', 'list'}
    if name == 'Process':
        return args.get('mode') == 'list'
    return False
