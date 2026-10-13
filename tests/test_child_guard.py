"""Abrupt host death must not leave a synthetic CLI executing."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

class ChildGuardTests(unittest.TestCase):
    def test_guard_terminates_descendant_after_parent_is_killed(self):
        guard=Path(__file__).resolve().parents[1]/'persona_runtime/child_guard.py'
        with tempfile.TemporaryDirectory() as directory:
            pidfile=Path(directory)/'child.pid'
            child="import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)"%str(pidfile)
            parent="import subprocess,sys,os,time; subprocess.Popen([sys.executable,%r,str(os.getpid()),sys.executable,'-c',%r],start_new_session=True); time.sleep(30)"%(str(guard),child)
            process=subprocess.Popen([sys.executable,'-c',parent],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            child_pid=None
            try:
                until=time.monotonic()+5
                while not pidfile.exists() and time.monotonic()<until:time.sleep(.05)
                self.assertTrue(pidfile.exists());child_pid=int(pidfile.read_text())
                process.kill();process.wait(timeout=3)
                until=time.monotonic()+5
                while time.monotonic()<until:
                    result=subprocess.run(['/bin/ps','-p',str(child_pid),'-o','stat='],capture_output=True,text=True)
                    if not result.stdout.strip() or result.stdout.strip().startswith('Z'):break
                    time.sleep(.05)
                else:self.fail('synthetic descendant still executing after parent death')
            finally:
                if process.poll() is None:process.kill();process.wait()
                if child_pid:
                    try:os.kill(child_pid,signal.SIGKILL)
                    except ProcessLookupError:pass

if __name__=='__main__':unittest.main()
