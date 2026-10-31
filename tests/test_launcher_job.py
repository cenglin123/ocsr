"""Offline Windows process-tree regression; never invokes the real opencode."""
from __future__ import annotations

import ctypes
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ocsr_job_test", ROOT / "scripts/ocsr_dispatch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("pwsh"),
                                reason="requires Windows and pwsh")

PAYLOAD = '''import ctypes, json, os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
level = int(sys.argv[2])
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.GetCurrentProcess.restype = ctypes.c_void_p
k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
if not k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
    raise ctypes.WinError(ctypes.get_last_error())
if level < 2:
    subprocess.Popen([sys.executable, __file__, str(root), str(level + 1)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
identity_file = root / (str(level) + '.tmp')
identity_file.write_text(json.dumps({'pid': os.getpid(), 'created': created.value}))
identity_file.replace(root / (str(level) + '.pid'))
if level == 0 and os.environ['OCSR_TEST_MODE'] == 'normal':
    while not (root / '2.pid').exists(): time.sleep(.05)
else:
    time.sleep(120)
'''

STUB = '''function opencode {
    & $env:OCSR_TEST_PYTHON $env:OCSR_TEST_PAYLOAD $env:OCSR_TEST_ROOT 0
    $global:LASTEXITCODE = $LASTEXITCODE
}
'''


def wait_until(predicate, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError("Timed out waiting for process state")


class ProcessHandle:
    """Retain an exact kernel process handle; cleanup cannot target a reused PID."""
    def __init__(self, pid, created, precision=1):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel.WaitForSingleObject.restype = ctypes.c_ulong
        self.kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        self.handle = self.kernel.OpenProcess(0x101001, False, pid)
        if not self.handle:
            # Normal cleanup can have removed this process before inspection.
            error = ctypes.get_last_error()
            if error != 87:
                raise ctypes.WinError(error)
        else:
            values = [ctypes.c_ulonglong() for _ in range(4)]
            if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(v) for v in values)):
                error = ctypes.get_last_error()
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                raise ctypes.WinError(error)
            if values[0].value // precision != created // precision:
                self.kernel.CloseHandle(self.handle)
                self.handle = None  # Original process exited; PID was reused.

    def stopped(self):
        return not self.handle or self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        if self.handle:
            if not self.stopped():
                self.kernel.TerminateProcess(self.handle, 1)
                self.kernel.WaitForSingleObject(self.handle, 5000)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def payload_handle(path):
    identity = json.loads(path.read_text())
    return ProcessHandle(identity['pid'], identity['created'])


def prepare(tmp_path, mode):
    (tmp_path / "payload.py").write_text(PAYLOAD, encoding="utf-8")
    (tmp_path / "prompt.txt").write_text("offline process test", encoding="utf-8")
    launcher = STUB + mod._pwsh_code("-m test/model", cwd=str(tmp_path))
    (tmp_path / "launcher.ps1").write_text(launcher, encoding="utf-8")
    env = dict(os.environ, OCSR_DISABLE_MODEL_CALLS="0", OCSR_TEST_MODE=mode,
               OCSR_TEST_PYTHON=sys.executable, OCSR_TEST_PAYLOAD=str(tmp_path / "payload.py"),
               OCSR_TEST_ROOT=str(tmp_path))
    return env


@pytest.mark.parametrize("mode", ["normal", "abrupt"])
def test_launcher_exit_cleans_three_level_tree(tmp_path, mode):
    env = prepare(tmp_path, mode)
    sibling = launcher = None
    handles = []
    try:
        sibling = mod._spawn_windowless([sys.executable, "-c", "import time; time.sleep(120)"])
        launcher = subprocess.Popen(["pwsh", "-NoProfile", "-File", str(tmp_path / "launcher.ps1")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        wait_until(lambda: (tmp_path / "2.pid").is_file())
        for i in range(3):
            handles.append(payload_handle(tmp_path / f"{i}.pid"))
        if mode == "abrupt":
            assert launcher.poll() is None
            assert not any(h.stopped() for h in handles)
            launcher.kill()  # Only launcher, deliberately not taskkill /T.
        launcher.wait(timeout=10)
        wait_until(lambda: all(h.stopped() for h in handles), timeout=8)
        assert sibling.poll() is None, "Unrelated sibling must survive"
        if mode == "normal":
            assert mod._read_start_marker_exit(tmp_path / "start.marker") == 0
    finally:
        if launcher is not None:
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=10)
        # Also recover payloads on a pre-fix failing regression run.
        for i in range(3):
            path = tmp_path / f"{i}.pid"
            if path.exists() and len(handles) <= i:
                handles.append(payload_handle(path))
        for h in handles:
            h.close()
        if sibling is not None:
            sibling.kill()
            sibling.wait(timeout=5)


def test_dispatcher_exit_does_not_kill_worker(tmp_path):
    env = prepare(tmp_path, "hold")
    dispatcher = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('driver', sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod._launch_worker(pathlib.Path(sys.argv[2]))
'''
    handles = []
    root = None
    try:
        subprocess.run([sys.executable, "-c", dispatcher, str(ROOT / "scripts/ocsr_dispatch.py"),
                        str(tmp_path)], env=env, check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_until(lambda: mod._read_json_object(tmp_path / 'process-identity.json') is not None)
        identity = json.loads((tmp_path / 'process-identity.json').read_text(encoding='utf-8-sig'))
        # .NET ticks (year 1) -> Win32 FILETIME ticks (year 1601).
        # Win32_Process exposes microseconds, whereas GetProcessTimes has 100ns precision.
        root = ProcessHandle(identity['pid'], int(identity['creation_time']) - 504911232000000000,
                             precision=10)
        wait_until(lambda: (tmp_path / "2.pid").exists())
        for i in range(3):
            handles.append(payload_handle(tmp_path / f"{i}.pid"))
        assert not root.stopped()
        assert not any(h.stopped() for h in handles)
        root.close()  # Abrupt root-only termination, after dispatcher already exited.
        wait_until(lambda: all(h.stopped() for h in handles), timeout=8)
    finally:
        if root:
            root.close()
        for i in range(3):
            path = tmp_path / f"{i}.pid"
            if path.exists() and len(handles) <= i:
                handles.append(payload_handle(path))
        for h in handles:
            h.close()


@pytest.mark.parametrize("failure", ["powershell", "limits", "assignment"])
def test_job_setup_failure_does_not_start_opencode(tmp_path, failure):
    env = prepare(tmp_path, "hold")
    source = (tmp_path / "launcher.ps1").read_text(encoding="utf-8")
    assert "[OcsrLauncherJob]::AttachCurrentProcess()" in source
    if failure == "powershell":
        source = source.replace("[OcsrLauncherJob]::AttachCurrentProcess()",
                                "throw 'injected job assignment failure'")
        message = 'injected job assignment failure'
    else:
        helper = (ROOT / 'scripts/ocsr_launcher_job.cs').read_bytes()
        if failure == "limits":
            broken = helper.replace(b'SetInformationJobObject(job, 9,',
                                    b'SetInformationJobObject(job, -1,')
            message = 'Cannot configure OCSR launcher job'
        else:
            broken = helper.replace(b'AssignProcessToJobObject(job, GetCurrentProcess())',
                                    b'AssignProcessToJobObject(job, IntPtr.Zero)')
            message = 'Cannot bind OCSR launcher to job'
        assert broken != helper
        source = source.replace(base64.b64encode(helper).decode('ascii'),
                                base64.b64encode(broken).decode('ascii'))
    (tmp_path / "launcher.ps1").write_text(source, encoding="utf-8")
    subprocess.run(["pwsh", "-NoProfile", "-File", str(tmp_path / "launcher.ps1")],
                   env=env, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert not (tmp_path / "0.pid").exists()
    assert mod._read_start_marker_exit(tmp_path / "start.marker") == 1
    assert message in (tmp_path / "error.log").read_text(encoding="utf-8-sig")
