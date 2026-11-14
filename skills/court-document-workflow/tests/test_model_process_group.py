"""Exercise timeout cleanup with owned, harmless real child processes only."""
import json
import os
from contextlib import nullcontext
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workflow_worker as worker


PROCESS_TREE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
folder, role, stubborn = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'yes'
if role != 'bridge' and stubborn:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
(folder / (role + '.pid')).write_text(str(os.getpid()))
if role in ('bridge', 'child'):
    following = 'child' if role == 'bridge' else 'grandchild'
    subprocess.Popen([sys.executable, __file__, str(folder), following, sys.argv[3]],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
if role == 'bridge':
    while not (folder / 'grandchild.pid').exists():
        time.sleep(0.01)
    (folder / 'ready.json').write_text(json.dumps({'group': os.getpgrp()}))
time.sleep(30)
(folder / (role + '.late-write')).write_text('unexpected surviving process')
'''


def running(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ModelProcessGroup(unittest.TestCase):
    def exercise_timeout(self, stubborn, interrupt=False):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            program = folder / 'owned_process_tree.py'
            program.write_text(PROCESS_TREE)
            group = None
            actual_popen = subprocess.Popen

            def interrupting_popen(*args, **kwargs):
                process = actual_popen(*args, **kwargs)
                actual_communicate = process.communicate
                first_call = True

                def communicate(*communicate_args, **communicate_kwargs):
                    nonlocal first_call
                    if first_call:
                        first_call = False
                        deadline = time.monotonic() + 2
                        while not (folder / 'ready.json').exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        raise KeyboardInterrupt()
                    return actual_communicate(*communicate_args, **communicate_kwargs)

                process.communicate = communicate
                return process

            try:
                intervention = (patch.object(worker.subprocess, 'Popen', side_effect=interrupting_popen)
                                if interrupt else nullcontext())
                with intervention, self.assertRaises(KeyboardInterrupt if interrupt else subprocess.TimeoutExpired):
                    worker.run_model_bridge(
                        [sys.executable, str(program), str(folder),
                         'bridge', 'yes' if stubborn else 'no'],
                        input='', timeout=2, env=os.environ.copy())
                self.assertTrue((folder / 'ready.json').exists(),
                                'The real child and grandchild must start before timeout')
                group = json.loads((folder / 'ready.json').read_text())['group']
                pids = {role: int((folder / (role + '.pid')).read_text())
                        for role in ('bridge', 'child', 'grandchild')}
                self.assertEqual(group, pids['bridge'])
                self.assertNotEqual(group, os.getpgrp())
                deadline = time.monotonic() + 3
                survivors = []
                while time.monotonic() < deadline:
                    survivors = [role for role, pid in pids.items() if running(pid)]
                    if not survivors:
                        break
                    time.sleep(0.05)
                self.assertEqual(survivors, [],
                                 'Timeout must terminate owned descendants even after their pipes close')
                self.assertFalse(list(folder.glob('*.late-write')))
            finally:
                # The independently-created group belongs exclusively to this test.
                if group is None and (folder / 'bridge.pid').exists():
                    group = int((folder / 'bridge.pid').read_text())
                if group and group != os.getpgrp():
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_timeout_terminates_real_child_and_grandchild(self):
        self.exercise_timeout(stubborn=False)

    def test_timeout_kills_descendants_ignoring_term_after_closing_pipes(self):
        self.exercise_timeout(stubborn=True)

    def test_keyboard_interrupt_cleans_owned_process_group(self):
        self.exercise_timeout(stubborn=True, interrupt=True)


if __name__ == '__main__':
    unittest.main()
