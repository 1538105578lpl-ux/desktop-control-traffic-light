import unittest
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / 'server', ROOT / 'vendor', ROOT / 'pet'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import reliability as r

def row(handle=1,pid=5,title='联系人',process='Weixin.exe',visible=True):
    return dict(handle=handle,pid=pid,title=title,process=process,visible=visible,
                minimized=False,rect=[0,0,500,500],foreground=True)

class SelectionTests(unittest.TestCase):
    def test_wechat_by_process_not_title(self):
        self.assertEqual(r.choose([row()],name='微信')['handle'],1)
    def test_ambiguous_refused(self):
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            r.choose([row(),row(2)],name='微信')
    def test_handle_selects_exact_window(self):
        self.assertEqual(r.choose([row(),row(2)],window_handle=2)['handle'],2)
    def test_pid_mismatch_refused(self):
        with self.assertRaises(ValueError):r.choose([row()],window_handle=1,process_id=100)
    def test_visible_preferred_to_hidden(self):
        self.assertEqual(r.choose([row(2,visible=False),row()],name='微信')['handle'],1)
    def test_foreground_loss_prevents_input(self):
        with patch.object(r.win32gui,'WindowFromPoint',return_value=1), patch.object(r.win32gui,'GetAncestor',return_value=1), patch.object(r.win32gui,'GetForegroundWindow',return_value=2), patch.object(r.uia,'Click') as click:
            with self.assertRaisesRegex(RuntimeError,'TARGET_NOT_FOREGROUND'):
                r.guarded_type(MagicMock(),[20,20],'测试',clear=True)
            click.assert_not_called()
    def test_focus_on_non_editor_prevents_input(self):
        control=MagicMock(ControlTypeName='ButtonControl')
        with patch.object(r.win32gui,'WindowFromPoint',return_value=1), patch.object(r.win32gui,'GetAncestor',return_value=1), patch.object(r.win32gui,'GetForegroundWindow',return_value=1), patch.object(r.uia,'Click'), patch.object(r.uia,'GetFocusedControl',return_value=control), patch.object(r.uia,'SetClipboardText') as clipboard:
            with self.assertRaisesRegex(RuntimeError,'EDIT_FOCUS_REQUIRED'):
                r.guarded_type(MagicMock(),[20,20],'测试',clear=True)
            clipboard.assert_not_called()
    def test_literal_multiline_and_no_submit(self):
        text='中文测试\n第二行 {Enter}'
        control=MagicMock(ControlTypeName='EditControl',ProcessId=5)
        control.BoundingRectangle=MagicMock(left=0,top=0,right=200,bottom=200)
        with patch.object(r.win32gui,'WindowFromPoint',return_value=1), patch.object(r.win32gui,'GetAncestor',return_value=1), patch.object(r.win32gui,'GetForegroundWindow',return_value=1), patch.object(r.win32process,'GetWindowThreadProcessId',return_value=(3,5)), patch.object(r.uia,'Click'), patch.object(r.uia,'GetFocusedControl',return_value=control), patch.object(r,'read_value',side_effect=['',text]), patch.object(r.uia,'SetClipboardText') as clipboard, patch.object(r.uia,'SendKeys') as keys, patch('win32clipboard.IsClipboardFormatAvailable',return_value=False), patch('win32clipboard.GetClipboardSequenceNumber',return_value=1):
            result=r.guarded_type(MagicMock(),[20,20],text,clear=True)
            clipboard.assert_called_once_with(text)
            self.assertTrue(result['verified'])
            self.assertFalse(result['submitted'])
            self.assertNotIn('{Enter}',[c.args[0] for c in keys.call_args_list])

if __name__=='__main__':unittest.main(verbosity=2)
