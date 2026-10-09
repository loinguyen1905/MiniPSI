#!/usr/bin/env python3
"""Stand-in for psi_session in tests: same arguments and output lines, no crypto or network.

FAKE_MODE (environment variable) lists what goes wrong, e.g. "hang_r:3,wrong_r:1":
  hang_s:<i> / hang_r:<i>   the sender / receiver session of pair i never exits
  wrong_r:<i>               the receiver of pair i returns a wrong intersection (exit code 2)
  crash_r:<i>               the receiver of pair i exits with code 1 and prints no RESULT
  noready_s:<i>             the sender of pair i exits without printing READY
  resource_s:<i> / resource_r:<i>  the sender (before READY) / receiver of pair i exits with
                            code 75, as psi_session does when it runs out of threads or memory
FAKE_SLEEP: seconds each session takes (default 0.2).
"""
import os
import sys
import time

proto, role, host, port, index, seed = sys.argv[1:7]
size = int(sys.argv[7]) if len(sys.argv) > 7 else 100
i = int(index)
modes = {}
for item in filter(None, os.environ.get("FAKE_MODE", "").split(",")):
    what, idx = item.split(":")
    modes.setdefault(what, set()).add(int(idx))
nap = float(os.environ.get("FAKE_SLEEP", "0.2"))
common = size // 10

if role == "s":
    if i in modes.get("noready_s", ()):
        sys.exit(1)
    if i in modes.get("resource_s", ()):
        sys.exit(75)
    print("READY", flush=True)
    time.sleep(nap)
    if i in modes.get("hang_s", ()):
        time.sleep(3600)
    print(f"RESULT {i} {size} 1000", flush=True)
else:
    time.sleep(nap)
    if i in modes.get("hang_r", ()):
        time.sleep(3600)
    if i in modes.get("crash_r", ()):
        sys.exit(1)
    if i in modes.get("resource_r", ()):
        sys.exit(75)
    wrong = i in modes.get("wrong_r", ())
    found = common - 1 if wrong else common
    print(f"RESULT {i} {size} 2000 {common} {found} {0 if wrong else 1} {found}", flush=True)
    sys.exit(2 if wrong else 0)
