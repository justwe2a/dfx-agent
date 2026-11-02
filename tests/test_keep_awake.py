import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class AwakeTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('keep_awake',Path(__file__).resolve().parents[1]/'scripts/keep-awake.py')
        self.script=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.script)

    def test_release_on_child_failure_and_exception(self):
        for error in (False,True):
            with self.subTest(exception=error), patch.object(self.script.ctypes,'WinDLL',create=True) as dll, patch.object(self.script.subprocess,'run') as run, patch.object(self.script.os,'name','nt'):
                state=dll.return_value.SetThreadExecutionState;state.return_value=0x80000000
                if error:
                    run.side_effect=OSError('cannot launch')
                    with self.assertRaises(OSError):self.script.run_awake(['python','job.py'])
                else:
                    run.return_value.returncode=3
                    self.assertEqual(self.script.run_awake(['python','job.py']),3)
                self.assertEqual([c.args[0] for c in state.call_args_list],[0x80000003,0x80000000])

    def test_rejected_awake_request_does_not_launch_command(self):
        with patch.object(self.script.ctypes,'WinDLL',create=True) as dll, patch.object(self.script.subprocess,'run') as run, patch.object(self.script.os,'name','nt'):
            dll.return_value.SetThreadExecutionState.return_value=0
            with self.assertRaises(OSError):self.script.run_awake(['python','job.py'])
            run.assert_not_called()
