import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
import uuid
import shutil
ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / 'server', ROOT / 'vendor', ROOT / 'pet'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from control_state import ControlBlocked
from task_lifecycle import DesktopState, explicit_resume
from state_store import update_state, record_hook, load

STAGE = ROOT / 'server'
TEMP_BASE = ROOT / 'test-temp'
TEMP_BASE.mkdir(exist_ok=True)


@contextmanager
def test_directory():
    # Normal inherited Windows ACLs; Python's 0700 tempfile ACL excludes the sandbox token.
    folder = TEMP_BASE / uuid.uuid4().hex
    folder.mkdir()
    try:
        yield folder
    finally:
        assert folder.resolve().parent == TEMP_BASE.resolve()
        shutil.rmtree(folder)


def prompt(session='original', text='帮我查看电脑里的文件', ts=None):
    return dict(id=str(time.time_ns()), event='UserPromptSubmit', session_id=session,
                prompt=text, ts=ts or time.time())


class TaskLifecycleTests(unittest.TestCase):
    def started(self):
        state = DesktopState()
        state.ingest([prompt()])
        state.begin('client', '查看文件', state.generation)
        return state

    def test_initial_task_starts_without_website(self):
        state = self.started()
        self.assertEqual(state.mode, 'running')
        self.assertEqual(state.owner_session, 'original')

    def test_model_cannot_start_without_human_hook(self):
        state = DesktopState()
        with self.assertRaisesRegex(ControlBlocked, 'USER_TASK_REQUIRED'):
            state.begin('client', '查看文件', state.generation)

    def test_pause_is_not_cleared_by_begin_progress_or_ordinary_prompt(self):
        state = self.started()
        state.transition('paused')
        for message in ['现在什么情况', '不要继续', '你可以解释一下继续按钮吗']:
            state.ingest([prompt(text=message)])
            with self.assertRaisesRegex(ControlBlocked, 'CONTROL_PAUSED'):
                state.begin('client', '查看文件', state.generation)
        self.assertEqual(state.mode, 'paused')

    def test_other_session_continue_cannot_resume(self):
        state = self.started(); state.transition('takeover')
        state.ingest([prompt('other', '继续')])
        with self.assertRaisesRegex(ControlBlocked, 'CONTROL_PAUSED'):
            state.begin('client', '查看文件', state.generation)

    def test_resume_invalidates_old_observation(self):
        state = self.started(); state.select_target(7)
        state.observe('client', state.generation)
        state.transition('paused')
        state.ingest([prompt(text='继续')])
        state.begin('client', '查看文件', state.generation)
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION_REQUIRED'):
            state.admit('client', 'Click', False, state.generation)

    def test_stop_hook_does_not_clear_pause(self):
        state = self.started(); state.transition('paused')
        state.ingest([dict(id='stop', event='Stop', session_id='original', ts=time.time())])
        self.assertEqual(state.mode, 'paused')
        self.assertTrue(state.snapshot()['desktop_active'])

    def test_another_task_begin_cannot_claim_same_shared_connector(self):
        state = self.started()
        state.ingest([prompt('other')])
        with self.assertRaisesRegex(ControlBlocked, 'DESKTOP_BUSY'):
            state.begin('client', '另一个任务', state.generation)

    def test_pause_survives_service_restart(self):
        with test_directory() as folder:
            path = Path(folder)/'intent.json'
            state = DesktopState(path); state.ingest([prompt()]); state.begin('client', '查看', state.generation)
            restarted = DesktopState(path)
            self.assertEqual(restarted.mode, 'paused')
            self.assertEqual(restarted.owner_session, 'original')
            with self.assertRaisesRegex(ControlBlocked, 'CONTROL_PAUSED'):
                restarted.begin('new-client', '查看', restarted.generation)
            restarted.ingest([prompt(text='继续')])
            restarted.begin('new-client', '查看', restarted.generation)
            self.assertEqual(restarted.mode, 'running')

    def test_end_cannot_reopen_same_user_turn(self):
        state = self.started(); state.end('client')
        with self.assertRaisesRegex(ControlBlocked, 'TASK_ENDED'):
            state.begin('client', '重复开始', state.generation)

    def test_idle_hotkey_does_not_latch_a_nonexistent_task(self):
        state = DesktopState(); state.transition('paused')
        self.assertEqual(state.mode, 'idle')
        state.ingest([prompt()]); state.begin('client', '查看', state.generation)

    def test_old_stop_and_new_prompt_in_same_batch_allow_new_turn(self):
        state = self.started()
        ended = dict(id='old-stop', event='Stop', session_id='original', ts=time.time())
        fresh = prompt(ts=ended['ts']+.0001)
        state.ingest([ended, fresh])
        state.begin('client', '新任务', state.generation)
        self.assertEqual(state.mode, 'running')

    def test_finished_human_hook_cannot_start_desktop_work(self):
        state=DesktopState(); human=prompt()
        state.ingest([human,dict(id='stop',event='Stop',session_id='original',ts=human['ts']+.1)])
        with self.assertRaisesRegex(ControlBlocked,'TASK_ENDED'):
            state.begin('client','旧任务',state.generation)

    def test_resume_phrases_are_narrow_and_do_not_accept_negations(self):
        for text in ['继续', '继续。', '请继续', '恢复电脑操作', '继续，先重新观察界面']:
            self.assertTrue(explicit_resume(text), text)
        for text in ['不要继续', '继续这个词是什么意思', '上次你让我说“继续”', '继续改代码', '自动继续']:
            self.assertFalse(explicit_resume(text), text)


class AggregateTests(unittest.TestCase):
    def test_permission_traffic_and_idle_notifications_keep_the_existing_semantics(self):
        from hook import build_payload
        self.assertEqual(build_payload({'hook_event_name':'PermissionRequest'})['state'],'working')
        self.assertIsNone(build_payload({'hook_event_name':'PermissionDenied'}))
        self.assertIsNone(build_payload({'hook_event_name':'Notification','notification_type':'idle_prompt'}))
        self.assertEqual(build_payload({'hook_event_name':'Notification','notification_type':'permission_prompt'})['state'],'waiting')
        self.assertEqual(build_payload({'hook_event_name':'Elicitation'})['state'],'waiting')

    def test_stop_one_task_keeps_other_working_and_retains_title(self):
        with test_directory() as folder:
            path=Path(folder)/'state.json'
            ts=time.time()
            update_state(path, dict(session_id='a', state='working', event='UserPromptSubmit', ts=ts, detail='任务 A'))
            update_state(path, dict(session_id='b', state='working', event='UserPromptSubmit', ts=ts+.1, detail='任务 B'))
            value=update_state(path, dict(session_id='b', state='done', event='Stop', ts=ts+.2))
            self.assertEqual(value['state'], 'working'); self.assertEqual(value['working_count'], 1)
            self.assertEqual(value['sessions']['b']['detail'], '任务 B')
            value=update_state(path, dict(session_id='a', state='done', event='Stop', ts=ts+.3))
            self.assertEqual(value['state'], 'done'); self.assertEqual(value['working_count'], 0)

    def test_session_start_cannot_erase_an_already_submitted_prompt(self):
        with test_directory() as folder:
            path=Path(folder)/'state.json'; ts=time.time()
            update_state(path, dict(session_id='a', state='working', event='UserPromptSubmit', ts=ts))
            value=update_state(path, dict(session_id='a', state='idle', event='SessionStart', ts=ts+.1))
            self.assertEqual(value['state'], 'working')

    def test_parallel_hooks_keep_all_sessions(self):
        import subprocess
        with test_directory() as folder:
            path=Path(folder)/'state.json'
            program="import sys,time; from state_store import update_state; update_state(sys.argv[1],dict(session_id=sys.argv[2],state='working',event='UserPromptSubmit',ts=time.time()))"
            processes=[subprocess.Popen([sys.executable,'-c',program,str(path),str(n)],cwd=str(ROOT / 'pet')) for n in range(8)]
            for process in processes:
                self.assertEqual(process.wait(timeout=15),0)
            self.assertEqual(load(path)['working_count'], 8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
