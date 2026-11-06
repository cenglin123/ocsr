"""Deterministic stand-in for scripts/ocsr_dispatch.py inside the adapter test sandbox.

The adapter contract tests never want to drive the real OCSR dispatcher (it would
call OpenCode and touch worker directories). This fixture keeps only the two
observable facts the adapter forwards: the child's stdout/stderr and its exit
code. ``FAKE_EXIT`` selects the exit code and ``FAKE_MULTILINE`` adds a second
stderr line, so success, failure, and single-line-detail paths stay hermetic.
"""

import os
import sys
import time

sys.stdout.write("FAKE-STDOUT\n")
sys.stdout.flush()
sys.stderr.write("FAKE-STDERR\n")
if os.environ.get("FAKE_MULTILINE") == "1":
    sys.stderr.write("FAKE-STDERR-SECOND-LINE\n")
sys.stderr.flush()
sleep_ms = int(os.environ.get("FAKE_SLEEP_MS", "0"))
if sleep_ms > 0:
    time.sleep(sleep_ms / 1000)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
