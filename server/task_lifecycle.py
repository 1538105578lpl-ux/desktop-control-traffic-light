"""Desktop task ownership and durable human pause intent, independent of the UI."""
import json
import re
import time
from pathlib import Path
from control_state import ControlState, ControlBlocked


def explicit_resume(prompt):
    # An ordinary message, negation or pasted instructions cannot release a pause.
    text = re.sub(r'[，,。.!！?？\s]+$', '', str(prompt).strip())
    return bool(re.fullmatch(r'(?:请|可以|好[，,]?\s*)?(?:继续|恢复电脑操作|继续电脑操作|继续刚才的任务|继续刚才的电脑操作)(?:[，,]\s*(?:先)?重新观察(?:一下)?(?:界面|画面))?', text))


class DesktopState(ControlState):
    def __init__(self, journal=None):
        super().__init__()
        self.mode = 'idle'
        self.journal = Path(journal) if journal else None
        self.pause_latched = False
        self.owner_client = None
        self.owner_session = None
        self.human = None
        self.resume_ticket = None
        self.paused_at = 0
        self.seen_hooks = set()
        self.current_action = ''
        self.task_started = False
        self.ended_at = 0
        self.started_human_id = None
        self.ended_human_id = None
        self.stops = {}
        self.target_title = ''
        if self.journal:
            try:
                old = json.loads(self.journal.read_text(encoding='utf-8'))
                if old.get('pause_latched') or old.get('mode') == 'running':
                    self.mode = 'stopped' if old.get('mode') == 'stopped' else 'paused'
                    self.pause_latched = True
                    self.owner_session = old.get('owner_session')
                    self.task = old.get('task', self.task)
                    self.task_started = True
                    self.paused_at = time.time()
                    self.event('control', '服务重新连接，电脑操作保持暂停；请回原任务明确说继续')
            except (OSError, ValueError):
                pass

    def persist(self):
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            temp = self.journal.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(mode=self.mode, pause_latched=self.pause_latched,
                owner_session=self.owner_session, task=self.task), ensure_ascii=False), encoding='utf-8')
            temp.replace(self.journal)

    def transition(self, mode):
        with self.lock:
            if self.mode == 'idle' and mode in {'paused', 'takeover', 'stopped'}:
                return
            if mode in {'paused', 'takeover', 'stopped'}:
                self.pause_latched = True
                self.paused_at = time.time()
                self.resume_ticket = None
            super().transition(mode)
            self.persist()

    def ingest(self, records):
        with self.lock:
            for item in records:
                ident = item.get('id')
                if not ident or ident in self.seen_hooks:
                    continue
                self.seen_hooks.add(ident)
                if len(self.seen_hooks) > 300:
                    self.seen_hooks = {r['id'] for r in records if 'id' in r}
                event, session = item.get('event'), item.get('session_id') or None
                if event == 'UserPromptSubmit':
                    if not self.human or item['ts'] > self.human['ts']:
                        self.human = item
                    if (self.pause_latched and self.mode != 'stopped' and session and
                            session == self.owner_session and item['ts'] > self.paused_at and
                            explicit_resume(item.get('prompt', ''))):
                        self.resume_ticket = dict(session=session, ts=item['ts'], generation=self.generation)
                elif event in {'Stop', 'StopFailure', 'SessionEnd'}:
                    self.stops[session] = item['ts']
                    if session == self.owner_session and self.mode == 'running':
                        self.end(self.owner_client, 'failed' if event == 'StopFailure' else 'ended')

    def begin(self, client, title, submitted_generation):
        with self.lock:
            if submitted_generation != self.generation:
                raise ControlBlocked('STALE_ACTION: control changed while BeginDesktopTask was queued.')
            if self.active:
                raise ControlBlocked('BUSY: the previous tool is still finishing. Observe before retrying.')
            human = self.human if self.human and time.time() - self.human['ts'] < 3600 else None
            session = human.get('session_id') if human else None
            if self.mode == 'running':
                if self.owner_client != client or session != self.owner_session:
                    raise ControlBlocked('DESKTOP_BUSY: another WorkBuddy task owns desktop input.')
                return self.snapshot()
            if self.pause_latched:
                if self.mode == 'stopped':
                    allowed = human and human['ts'] > self.paused_at and not explicit_resume(human.get('prompt', ''))
                else:
                    ticket = self.resume_ticket
                    allowed = (ticket and ticket['generation'] == self.generation and
                        ticket['session'] == session == self.owner_session and
                        time.time() - ticket['ts'] < 180)
                if not allowed:
                    raise ControlBlocked('CONTROL_PAUSED: input stays blocked. Ask the user to say “继续” in the original WorkBuddy task; do not poll or bypass the gate.')
            if not session:
                raise ControlBlocked('USER_TASK_REQUIRED: no recent WorkBuddy UserPromptSubmit hook. Check the traffic-light hook connection; do not use a website to start control.')
            if human['id'] == self.ended_human_id or self.stops.get(session, 0) >= human['ts']:
                raise ControlBlocked('TASK_ENDED: this user turn already ended. Wait for a new user task.')
            if self.mode == 'idle':
                self.target = None
                self.target_title = ''
            self.pause_latched = False
            self.resume_ticket = None
            self.owner_client, self.owner_session = client, session
            self.task_started = True
            self.started_human_id = human['id']
            self.task = dict(title=str(title or '电脑操作')[:200], steps=[], current=-1, status='running')
            self.current_action = '准备观察界面'
            super().transition('running')
            self.persist()
            return self.snapshot()

    def end(self, client, status='ended'):
        with self.lock:
            if client != self.owner_client:
                raise ControlBlocked('DESKTOP_BUSY: only the owning task can end this desktop operation.')
            if self.pause_latched:
                return self.snapshot()
            self.mode = 'idle'
            self.ended_at = time.time()
            self.ended_human_id = self.started_human_id
            self.generation += 1
            self.observations.clear()
            self.cleanup()
            self.task['status'] = status
            self.current_action = '本轮电脑操作结束' if status != 'failed' else '电脑操作遇到问题'
            self.owner_client = None
            self.event('task_end', self.current_action)
            self.persist()
            return self.snapshot()

    def admit(self, client_id, name, read_only, submitted_generation):
        with self.lock:
            if not read_only and self.owner_client and self.owner_client != client_id:
                raise ControlBlocked('DESKTOP_BUSY: another task owns desktop input.')
            return super().admit(client_id, name, read_only, submitted_generation)

    def snapshot(self):
        with self.lock:
            value = super().snapshot()
            value.update(owner_session=self.owner_session, pause_latched=self.pause_latched, target_title=self.target_title,
                         current_action=self.current_action, desktop_active=self.task_started and self.mode != 'idle')
            return value
