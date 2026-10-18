"""Meaningful gate and protocol checks. All OS inputs are mocked."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / 'server', ROOT / 'vendor', ROOT / 'pet'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from control_state import ControlState, ControlBlocked, is_read_only, BOUND_LEASE


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.state = ControlState(clock=lambda: self.now)

    def ready(self, client='a'):
        self.state.transition('running')
        self.state.select_target(1)
        self.state.observe(client, self.state.generation)

    def admit(self, name='Click', client='a', generation=None):
        return self.state.admit(client, name, is_read_only(name, {}), self.state.generation if generation is None else generation)

    def test_default_paused_blocks_mutation(self):
        with self.assertRaisesRegex(ControlBlocked, 'CONTROL_PAUSED'):
            self.admit()

    def test_read_allowed_while_paused(self):
        lease = self.admit('WindowList')
        self.state.finish(lease)

    def test_missing_observation_blocks_input(self):
        self.state.transition('running')
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION'):
            self.admit()

    def test_other_session_cannot_reuse_observation(self):
        self.ready()
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION'):
            self.admit(client='b')

    def test_observation_consumed_after_input(self):
        self.ready()
        self.state.finish(self.admit())
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION'):
            self.admit()

    def test_pausing_invalidates_inflight_lease_even_after_resume(self):
        self.ready()
        lease = self.admit()
        self.state.transition('paused')
        self.state.transition('running')
        with self.assertRaisesRegex(ControlBlocked, 'CONTROL_INTERRUPTED'):
            self.state.check(lease)

    def test_queued_action_discarded_after_resume(self):
        self.ready()
        generation = self.state.generation
        self.state.transition('paused')
        self.state.transition('running')
        self.state.observe('a', self.state.generation)
        with self.assertRaisesRegex(ControlBlocked, 'STALE_ACTION'):
            self.admit(generation=generation)

    def test_resume_requires_new_observation(self):
        self.ready()
        self.state.transition('takeover')
        self.state.transition('running')
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION'):
            self.admit()

    def test_window_change_cancels_lease(self):
        self.ready()
        lease = self.admit()
        self.state.select_target(2)
        with self.assertRaises(ControlBlocked):
            self.state.check(lease)

    def test_finished_worker_cannot_borrow_the_next_same_tool_lease(self):
        self.ready()
        old = self.admit()
        self.state.finish(old)
        self.state.observe('a', self.state.generation)
        new = self.admit()
        with self.assertRaises(ControlBlocked):
            self.state.check(old)
        self.state.finish(old)
        self.assertIs(self.state.active, new)

    def test_expired_observation_blocked(self):
        self.ready()
        self.now += 91
        with self.assertRaisesRegex(ControlBlocked, 'FRESH_OBSERVATION'):
            self.admit()

    def test_observation_started_before_pause_cannot_authorize_resume(self):
        self.ready()
        generation = self.state.generation
        self.state.transition('paused')
        self.state.transition('running')
        self.state.observe('a', generation)
        with self.assertRaises(ControlBlocked):
            self.admit()

    def test_only_read_modes_bypass_pause(self):
        for name, read, write in [('Clipboard','get','set'),('FileSystem','read','write'),('Registry','get','set'),('Process','list','kill')]:
            self.assertTrue(is_read_only(name, {'mode':read}))
            self.assertFalse(is_read_only(name, {'mode':write}))
            self.assertFalse(is_read_only(name, {}))


class InputBoundaryTests(unittest.TestCase):
    def setUp(self):
        import windows_mcp.uia as uia
        import windows_mcp.uia.core as core
        from windows_mcp.desktop.service import Desktop
        self.uia, self.core, self.desktop = uia, core, Desktop
        names = ('keybd_event', 'mouse_event', 'SetCursorPos', 'SendInput')
        methods = ('bring_window_to_top', 'launch_app', 'app', 'type', 'multi_edit', 'multi_select')
        self.saved = [(obj, name, getattr(obj, name)) for obj, keys in [(uia,names),(core,names),(Desktop,methods)] for name in keys]
        self.key = MagicMock()
        self.mouse = MagicMock()
        self.cursor = MagicMock()
        self.sendinput = MagicMock(return_value=1)
        core.keybd_event, core.mouse_event, core.SetCursorPos, core.SendInput = self.key, self.mouse, self.cursor, self.sendinput
        self.state = ControlState()
        self.state.transition('running')
        self.state.select_target(1)
        self.state.observe('a', self.state.generation)
        self.lease = self.state.admit('a', 'Shortcut', False, self.state.generation)
        self.binding = BOUND_LEASE.set(self.lease)
        from input_guard import install_input_guard
        install_input_guard(self.state)
        self.focus = patch('input_guard.win32gui.GetForegroundWindow', return_value=1)
        self.focus.start()

    def tearDown(self):
        BOUND_LEASE.reset(self.binding)
        self.focus.stop()
        for obj, name, value in self.saved:
            setattr(obj, name, value)

    def test_pause_releases_service_keys_and_blocks_later_keys(self):
        self.core.keybd_event(17, 0, 0, 0)
        self.state.transition('paused')
        self.assertEqual(self.key.call_args.args[:3], (17,0,2))
        count = self.key.call_count
        with self.assertRaises(ControlBlocked):
            self.core.keybd_event(86,0,0,0)
        self.assertEqual(self.key.call_count,count)

    def test_foreground_loss_blocks_key(self):
        with patch('input_guard.win32gui.GetForegroundWindow', return_value=2):
            with self.assertRaisesRegex(ControlBlocked,'TARGET_NOT_FOREGROUND'):
                self.core.keybd_event(86,0,0,0)
        self.key.assert_not_called()

    def test_foreign_window_cover_blocks_cursor(self):
        with patch('input_guard.win32gui.WindowFromPoint', return_value=2), patch('input_guard.win32gui.GetAncestor', return_value=2):
            with self.assertRaisesRegex(ControlBlocked,'POINT_OUTSIDE_TARGET'):
                self.core.SetCursorPos(25,25)
        self.cursor.assert_not_called()

    def test_mouse_release_with_absolute_flag_is_tracked(self):
        with patch.object(self.core,'GetCursorPos',return_value=(25,25)), patch('input_guard.win32gui.WindowFromPoint',return_value=1), patch('input_guard.win32gui.GetAncestor',return_value=1):
            self.core.mouse_event(2|32768,0,0,0,0)
            self.core.mouse_event(4|32768,0,0,0,0)
            self.state.transition('paused')
        self.assertEqual(self.mouse.call_count,2)

    def test_finish_releases_abandoned_modifier(self):
        self.core.keybd_event(17,0,0,0)
        self.state.finish(self.lease)
        self.assertEqual(self.key.call_count,2)

    def test_unicode_sendinput_pause_releases_only_owned_input(self):
        item=self.core.KeyboardInput(0,0x4e2d,4)
        self.core.SendInput(item)
        self.state.transition('paused')
        self.assertEqual(self.sendinput.call_count,2)
        self.assertTrue(self.sendinput.call_args.args[0].union.ki.dwFlags & 2)
        with self.assertRaises(ControlBlocked):
            self.core.SendInput(item)
        self.assertEqual(self.sendinput.call_count,2)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import panel_service as service
        self.service=service
        self.saved=(service.STATE,service.CLIENT,service.TOOLS,service.DISPATCH)
        service.STATE=ControlState()
        service.CLIENT=MagicMock()
        service.CLIENT.call_tool=AsyncMock()
        service.TOOLS=[{'name':name} for name in ['Click','Snapshot','WindowList','PowerShell','App']]
        service.DISPATCH=asyncio.Lock()

    async def asyncTearDown(self):
        s=self.service
        s.STATE,s.CLIENT,s.TOOLS,s.DISPATCH=self.saved

    async def test_paused_tool_never_reaches_backend(self):
        result=await self.service.execute('Click',{'loc':[0,0]},'a')
        self.assertTrue(result['isError'])
        self.service.CLIENT.call_tool.assert_not_called()

    async def test_arbitrary_script_paths_disabled(self):
        for name,args in [('PowerShell',{'command':'noop'}),('App',{'mode':'launch_executable','executable':'anything.exe'}),('App',{'name':'PowerShell'})]:
            result=await self.service.execute(name,args,'a')
            self.assertTrue(result['isError'])
        self.service.CLIENT.call_tool.assert_not_called()

    async def test_queued_input_not_replayed_on_resume(self):
        s=self.service
        s.STATE.transition('running');s.STATE.select_target(1);s.STATE.observe('a',s.STATE.generation)
        await s.DISPATCH.acquire()
        queued=asyncio.create_task(s.execute('Click',{'loc':[0,0]},'a'))
        await asyncio.sleep(.01)
        s.STATE.transition('paused');s.STATE.transition('running');s.STATE.observe('a',s.STATE.generation)
        s.DISPATCH.release()
        result=await queued
        self.assertIn('STALE_ACTION',result['content'][0]['text'])
        s.CLIENT.call_tool.assert_not_called()

    async def test_http_token_and_origin_checks(self):
        import httpx2
        s=self.service
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=s.APP),base_url='http://127.0.0.1:18765') as client:
            self.assertEqual((await client.get('/api/status')).status_code,401)
            self.assertEqual((await client.get('/api/session')).status_code,401)
            self.assertEqual((await client.get('/api/session',headers={'Sec-Fetch-Site':'cross-site','Referer':'https://foreign.example/'})).status_code,401)
            self.assertEqual((await client.get('/api/session',headers={'Sec-Fetch-Site':'same-origin','Referer':'http://127.0.0.1:18765/'})).status_code,401)
            self.assertEqual((await client.get('/')).status_code,404)
            for removed in ['/api/session','/api/windows']:
                self.assertEqual((await client.get(removed,headers={'X-Control-Token':s.TOKEN})).status_code,404)
            self.assertEqual((await client.post('/api/control',json={'action':'select','handle':1},headers={'X-Control-Token':s.TOKEN})).status_code,400)
            self.assertEqual((await client.get('/api/status',headers={'X-Control-Token':s.TOKEN})).status_code,200)
            self.assertEqual((await client.post('/api/control',json={'action':'resume'},headers={'X-Control-Token':s.TOKEN,'Origin':'https://foreign.example'})).status_code,403)
            self.assertEqual((await client.get('/api/status',headers={'X-Control-Token':s.TOKEN,'Host':'foreign.example'})).status_code,403)
            self.assertEqual(s.STATE.mode,'paused')
            response=await client.post('/api/control',json={'action':'takeover'},headers={'X-Control-Token':s.TOKEN})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['mode'],'takeover')
            self.assertIn('preview',response.json())
            self.assertIn('connections',response.json())
            self.assertIn('global_pause_hotkey',response.json())


if __name__=='__main__':
    unittest.main(verbosity=2)
