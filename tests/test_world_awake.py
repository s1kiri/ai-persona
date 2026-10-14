"""Sleep protection is temporary and follows one finite review only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from persona_runtime import world_awake


class AwakeTests(unittest.TestCase):
    def test_stopped_plan_releases_native_assertion_and_marks_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory/'lab.json').write_text(json.dumps({'pid': 12345}))
            plan = {'id': 'synthetic', 'status': 'running', 'deadline': 2000}
            child = Mock(pid=54321)
            child.poll.return_value = None
            with patch.object(world_awake.sys, 'platform', 'darwin'), \
                 patch.object(world_awake, 'night', side_effect=[plan, {**plan, 'status': 'stopped'}]), \
                 patch.object(world_awake.time, 'time', return_value=1000), \
                 patch.object(world_awake.os, 'kill'), \
                 patch.object(world_awake.subprocess, 'Popen', return_value=child) as launch:
                world_awake.run(directory)
            self.assertEqual(launch.call_args.args[0],
                             ['/usr/bin/caffeinate', '-i', '-t', '1000', '-w', '12345'])
            child.terminate.assert_called_once_with()
            child.wait.assert_called_once_with(timeout=5)
            receipt = json.loads((directory/'awake.json').read_text())
            self.assertFalse(receipt['active'])
            self.assertEqual(receipt['night_id'], 'synthetic')

    def test_no_assertion_without_a_running_finite_plan(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory/'lab.json').write_text(json.dumps({'pid': 12345}))
            for plan in [None, {'status': 'finished', 'deadline': 2000},
                         {'status': 'running', 'deadline': 50000}]:
                with self.subTest(plan=plan), \
                     patch.object(world_awake.sys, 'platform', 'darwin'), \
                     patch.object(world_awake, 'night', return_value=plan), \
                     patch.object(world_awake.time, 'time', return_value=1000), \
                     patch.object(world_awake.subprocess, 'Popen') as launch:
                    with self.assertRaises(RuntimeError):
                        world_awake.run(directory)
                    launch.assert_not_called()
