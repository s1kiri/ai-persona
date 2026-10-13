"""Trusted subprocess supervisor: a killed runtime cannot leave Codex running.

No model-generated code. Inherit pipes/env and the supervisor process group;
terminate that entire group if the original parent disappears.
"""
import os
import signal
import subprocess
import sys
import time

def main():
    parent=int(sys.argv[1])
    child=subprocess.Popen(sys.argv[2:])
    stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop)
    while child.poll() is None:
        if os.getppid()!=parent or stopping:
            # Ignore TERM locally so descendants get a short cleanup window.
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            os.killpg(os.getpgrp(),signal.SIGTERM)
            time.sleep(.5)
            os.killpg(os.getpgrp(),signal.SIGKILL)
        time.sleep(.1)
    return child.returncode if child.returncode>=0 else 128-child.returncode

if __name__=='__main__':sys.exit(main())
