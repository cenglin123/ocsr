#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocsr_runtime.py — OCSR 共享底层运行时原语（进程身份、脱管 Launcher、文件锁与原子写）。

由 ocsr_dispatch.py 与 ocsr_org.py 共同复用，不反向 import ocsr_dispatch。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import ocsr_backends as _backends

PID_FILE_NAME = "pid.txt"
PROCESS_IDENTITY_FILE = "process-identity.json"
START_MARKER_FILE = "start.marker"

_MSYS_DRIVE_PATH = re.compile(r"^/([A-Za-z])(?:/(.*))?$")


def _native_path_str(path: "str | os.PathLike | None" = None) -> str:
    """把路径转成子进程可用的原生绝对路径字符串（Windows 下转成 C:/...）。"""
    raw = os.fspath(path) if path is not None else os.getcwd()
    text = str(raw)
    if sys.platform != "win32":
        return text
    match = _MSYS_DRIVE_PATH.match(text)
    if match:
        drive = match.group(1).upper()
        rest = (match.group(2) or "").replace("/", "\\")
        text = f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    try:
        absolute = os.path.abspath(text)
    except (OSError, ValueError):
        absolute = text
    return absolute.replace("\\", "/")


def _child_env_with_native_pwd(
    path: "str | os.PathLike | None" = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """返回把 PWD 规范成原生路径的子进程环境；附带 extra_env 时在 POSIX 也返回新环境字典。"""
    if sys.platform != "win32" and not extra_env:
        return None
    env = dict(os.environ)
    if sys.platform == "win32":
        env["PWD"] = _native_path_str(path)
    if extra_env:
        env.update(extra_env)
    return env


def _write_utf8(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def atomic_write_json(path: Path, data: Any) -> None:
    """以唯一临时文件 + os.replace 原子写入 UTF-8 LF JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_pid(wd: Path) -> int | None:
    """读取 <wd>/pid.txt。返回 None 表示 PID 不可用（未捕获/文件损坏）。"""
    try:
        raw = (wd / PID_FILE_NAME).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, AttributeError):
        return None
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else None


def _decode_native(data: bytes | str | None) -> str:
    """原生命令输出的容错解码：UTF-8 strict 优先，失败按本机代码页（Windows mbcs）。"""
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")
            except Exception:
                text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _ticks_to_iso(ticks: str) -> str:
    """.NET UTC Ticks 转可读 ISO 时间。"""
    try:
        t = int(ticks)
        return datetime.datetime.fromtimestamp(
            (t - 621355968000000000) / 1e7,
            tz=datetime.timezone.utc,
        ).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ticks or "?"


def parse_start_marker_exit(marker_path: Path) -> int | None:
    """从 start.marker 中提取最后一条 exit=<code>，未完成返回 None。"""
    try:
        text = marker_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"(?m)^exit=(-?\d+)\s*$", text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def file_sha256(path: Path) -> tuple[int, str]:
    """返回文件字节大小与 SHA-256 十六进制摘要。"""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _query_process_table(
    *,
    run_fn: Callable[..., Any] = subprocess.run,
) -> dict[int, dict]:
    """读取 Windows 进程 PID/父 PID/创建身份；失败返回空表并由调用方 fail-closed。"""
    if sys.platform != "win32":
        return _query_process_table_posix(run_fn=run_fn)
    command = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "Get-CimInstance Win32_Process | ForEach-Object { "
        "[ordered]@{ ProcessId = [int]$_.ProcessId; "
        "ParentProcessId = [int]$_.ParentProcessId; "
        "CreationTimeUtcTicks = ([datetime]$_.CreationDate).ToUniversalTime().Ticks."
        "ToString([System.Globalization.CultureInfo]::InvariantCulture); "
        "CommandLine = [string]$_.CommandLine } "
        "} | ConvertTo-Json -Compress"
    )
    try:
        proc = run_fn(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return {}
    if proc is None or getattr(proc, "returncode", 1) != 0 or not (getattr(proc, "stdout", b"") or b"").strip():
        return {}
    try:
        raw = json.loads(_decode_native(proc.stdout))
    except json.JSONDecodeError:
        return {}
    rows = raw if isinstance(raw, list) else [raw]
    table: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            process_id = int(row["ProcessId"])
            parent_id = int(row.get("ParentProcessId", 0))
        except (KeyError, TypeError, ValueError):
            continue
        table[process_id] = {
            "parent_pid": parent_id,
            "creation_time": str(row.get("CreationTimeUtcTicks") or "").strip(),
            "command": str(row.get("CommandLine") or "")[:200],
        }
    return table


def _query_process_table_posix(
    *,
    run_fn: Callable[..., Any] = subprocess.run,
) -> dict[int, dict]:
    try:
        proc = run_fn(
            ["ps", "-eo", "pid=,ppid=,lstart=,args="],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    table: dict[int, dict] = {}
    for line in _decode_native(proc.stdout).splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        lstart = " ".join(parts[2:7])
        cmd = parts[7][:200] if len(parts) > 7 else ""
        table[pid] = {
            "parent_pid": ppid,
            "creation_time": lstart,
            "command": cmd,
        }
    return table


def _descendant_pids(
    root_pid: int,
    table: dict[int, dict],
    root_creation_time: str = "",
) -> set[int]:
    """遍历 root 的后代，带 PID 复用身份防护。"""
    current = table.get(root_pid)
    pid_reused = (
        current is not None
        and root_creation_time
        and current.get("creation_time") != root_creation_time
    )
    current_creation = current.get("creation_time", "") if pid_reused else ""
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        first_level = frontier == {root_pid}
        children = set()
        for pid, info in table.items():
            if pid in descendants or int(info.get("parent_pid", 0)) not in frontier:
                continue
            if pid_reused and first_level:
                child_creation = info.get("creation_time") or ""
                if child_creation and child_creation >= current_creation:
                    continue
            children.add(pid)
        descendants.update(children)
        frontier = children
    descendants.discard(root_pid)
    return descendants


def check_process_status(
    pid: int,
    creation_time: str,
    table: dict[int, dict] | None = None,
    *,
    query_table_fn: Callable[[], dict[int, dict]] = _query_process_table,
) -> str:
    """校验进程是否仍为原始身份存活：running / stopped / pid_reused / unknown。"""
    proc_table = table if table is not None else query_table_fn()
    if not proc_table:
        return "unknown"
    row = proc_table.get(pid)
    if row is None:
        return "stopped"
    actual = str(row.get("creation_time") or "").strip()
    if not actual:
        return "unknown"
    if not creation_time:
        return "running"
    return "running" if actual == creation_time else "pid_reused"


def kill_process_tree_verified(
    label: str,
    wd: Path,
    *,
    query_table_fn: Callable[[], dict[int, dict]] = _query_process_table,
    run_fn: Callable[..., Any] = subprocess.run,
) -> bool:
    """按 PID 终止进程树；仅在 PID 与已知后代均确认消失时返回 True。"""
    pid = _read_pid(wd)
    if pid is None:
        print(
            f"[ocsr] ⚠️ {label} 无可用 PID（{PID_FILE_NAME} 缺失或损坏），无法按 PID 终止",
            file=sys.stderr,
        )
        return False
    before = query_table_fn()
    if not before:
        print(f"[ocsr] ⚠️ {label} 无法读取进程表，拒绝在未知身份下终止", file=sys.stderr)
        return False
    identity = _read_json_object(wd / PROCESS_IDENTITY_FILE)
    if (
        identity is None
        or identity.get("pid") != pid
        or not str(identity.get("creation_time") or "").strip()
        or not str(identity.get("command_fingerprint") or "").strip()
    ):
        print(
            f"[ocsr] ⚠️ {label} 缺少完整进程启动身份，拒绝终止未验证 PID={pid}",
            file=sys.stderr,
        )
        return False
    root = before.get(pid)
    if root is None or root.get("creation_time") != identity.get("creation_time"):
        print(
            f"[ocsr] ⚠️ {label} PID={pid} 启动身份不一致（可能 PID 复用），拒绝终止",
            file=sys.stderr,
        )
        return False
    tracked = {pid} | _descendant_pids(pid, before)
    try:
        proc = run_fn(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception as e:
        print(f"[ocsr] ⚠️ {label} taskkill PID={pid} 异常: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        detail = (_decode_native(proc.stdout) + _decode_native(proc.stderr)).strip()[:200]
        print(
            f"[ocsr] ⚠️ {label} taskkill PID={pid} 失败 (rc={proc.returncode}): {detail}",
            file=sys.stderr,
        )
        return False
    remaining = set(tracked)
    for _ in range(20):
        after = query_table_fn()
        if not after:
            time.sleep(0.25)
            continue
        remaining = tracked.intersection(after)
        if not remaining:
            return True
        time.sleep(0.25)
    print(f"[ocsr] ⚠️ {label} 终止后仍存在 PID/后代: {sorted(remaining)}", file=sys.stderr)
    return False


def _pwsh_code(
    text: str,
    cwd: str | None = None,
    *,
    backend: str = _backends.BACKEND_OPENCODE,
    command_line: str | None = None,
    capture_output_path: str | None = None,
) -> str:
    """生成通用 launcher，保留统一 Job/身份/日志/marker 协议并按后端注入命令与 session 提取。"""
    adapter = _backends.get_backend_adapter(backend)
    fingerprint_source = command_line if command_line is not None else text
    command_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    job_source = (Path(__file__).with_name("ocsr_launcher_job.cs")).read_bytes()
    job_source_b64 = base64.b64encode(job_source).decode("ascii")
    worker_cwd = _native_path_str(cwd if cwd is not None else Path.cwd())
    cwd_literal = worker_cwd.replace("'", "''")
    exec_line = (
        command_line
        if command_line is not None
        else f"& opencode run $prompt --format json --standalone {text} '{cwd_literal}'"
    )
    sid_field = adapter.session_id_field
    pre_capture_clean = ""
    capture_block = ""
    if capture_output_path:
        pre_capture_clean = '          Remove-Item -LiteralPath "$PSScriptRoot/last-message.tmp" -Force -ErrorAction SilentlyContinue\n'
        cap_literal = _native_path_str(capture_output_path).replace("'", "''")
        capture_block = textwrap.dedent(f"""\
          if ($opencodeExit -eq 0) {{
            $captureOk = $false
            if (Test-Path -LiteralPath "$PSScriptRoot/last-message.tmp" -PathType Leaf) {{
              $capturedText = Get-Content -LiteralPath "$PSScriptRoot/last-message.tmp" -Raw -Encoding UTF8
              if (-not [string]::IsNullOrWhiteSpace($capturedText)) {{
                $targetPath = '{cap_literal}'
                $targetDir = Split-Path -Parent $targetPath
                $atomicTmp = "$targetPath.ocsr.tmp.$PID"
                try {{
                  if ($targetDir -and -not (Test-Path -LiteralPath $targetDir)) {{
                    New-Item -ItemType Directory -Path $targetDir -Force -ErrorAction Stop | Out-Null
                  }}
                  [System.IO.File]::WriteAllText($atomicTmp, $capturedText, [Text.UTF8Encoding]::new($false))
                  Move-Item -LiteralPath $atomicTmp -Destination $targetPath -Force -ErrorAction Stop
                  $captureOk = $true
                }} catch {{
                  Remove-Item -LiteralPath $atomicTmp -Force -ErrorAction SilentlyContinue
                  "capture-reply commit failed: $_" | Add-Content "$PSScriptRoot/error.log" -Encoding UTF8
                }}
              }}
            }}
            if (-not $captureOk) {{
              "capture-reply failed: missing or empty last-message.tmp" | Add-Content "$PSScriptRoot/error.log" -Encoding UTF8
              $opencodeExit = 2
            }}
          }}
        """)
        capture_block = "\n".join("          " + ln if ln else "" for ln in capture_block.splitlines()) + "\n"
    return textwrap.dedent(f"""\
        if ($env:OCSR_DISABLE_MODEL_CALLS -eq '1') {{
            "launcher blocked: OCSR_DISABLE_MODEL_CALLS=1" | Set-Content "$PSScriptRoot/error.log"
            exit 1
        }}
        $selfProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $identity = [ordered]@{{
            pid = $PID
            creation_time = ([datetime]$selfProcess.CreationDate).ToUniversalTime().Ticks.ToString([System.Globalization.CultureInfo]::InvariantCulture)
            command_fingerprint = "{command_fingerprint}"
        }}
        $identity | ConvertTo-Json -Compress | Set-Content "$PSScriptRoot/process-identity.json" -Encoding UTF8
        "pwsh started $(Get-Date -Format o)" | Set-Content "$PSScriptRoot/start.marker"
        try {{
          [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
          $OutputEncoding = [Console]::OutputEncoding
          if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {{
            $jobSource = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{job_source_b64}'))
            Add-Type -TypeDefinition $jobSource -ErrorAction Stop
            [OcsrLauncherJob]::AttachCurrentProcess()
          }}
          Set-Location -LiteralPath '{cwd_literal}'
          $env:PWD = '{cwd_literal}'
          $prompt = Get-Content "$PSScriptRoot/prompt.txt" -Raw -Encoding UTF8
{pre_capture_clean}          $sessionIDs = [System.Collections.Generic.HashSet[string]]::new()
          {exec_line} 2> "$PSScriptRoot/stderr.log" | ForEach-Object {{
            $line = [string]$_
            $line | Add-Content "$PSScriptRoot/run.log" -Encoding UTF8
            try {{
              $event = $line | ConvertFrom-Json -ErrorAction Stop
              $sessionProperty = $event.PSObject.Properties['{sid_field}']
              if ($null -ne $sessionProperty -and $sessionProperty.Value -is [string] -and $sessionProperty.Value.Trim()) {{
                [void]$sessionIDs.Add($sessionProperty.Value.Trim())
                if ($sessionIDs.Count -eq 1) {{
                  $binding = [ordered]@{{
                    session_id = @($sessionIDs)[0]
                    backend = '{adapter.name}'
                    cwd = '{cwd_literal}'
                    pid = $identity.pid
                    creation_time = $identity.creation_time
                    command_fingerprint = $identity.command_fingerprint
                  }}
                  $binding | ConvertTo-Json -Compress | Set-Content "$PSScriptRoot/session-binding.json" -Encoding UTF8
                }} elseif ($sessionIDs.Count -gt 1) {{
                  Remove-Item "$PSScriptRoot/session-binding.json" -Force -ErrorAction SilentlyContinue
                }}
              }}
            }} catch {{ }}
          }}
          $opencodeExit = $LASTEXITCODE
          if ($sessionIDs.Count -ne 1) {{
            Remove-Item "$PSScriptRoot/session-binding.json" -Force -ErrorAction SilentlyContinue
          }}
{capture_block}          "exit=$opencodeExit" | Add-Content "$PSScriptRoot/start.marker"
        }} catch {{
          $_ | Out-File "$PSScriptRoot/error.log" -Encoding UTF8
          "exit=1" | Add-Content "$PSScriptRoot/start.marker"
        }}
    """)


def _spawn_windowless(args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    """以不分配 console 窗口 + 不继承父进程句柄的方式启动进程。

    Windows 下优先尝试 CREATE_BREAKAWAY_FROM_JOB，使脱管子进程在允许 breakaway 的
    宿主 shell 退出后继续存活；若外层 Job 禁止 breakaway（WinError 5），自动回退到
    CREATE_NO_WINDOW。
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) if sys.platform == "win32" else 0
    if sys.platform == "win32" and breakaway:
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=no_window | breakaway,
                env=env,
            )
        except PermissionError:
            pass
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=no_window,
        env=env,
    )


def _launch_worker(
    wd: Path,
    *,
    extra_env: dict[str, str] | None = None,
    spawn_fn: Callable[..., subprocess.Popen] = _spawn_windowless,
) -> int:
    """无窗口、脱管启动 pwsh -File <wd>/launcher.ps1，写 pid.txt 并返回 PID。"""
    proc = spawn_fn(
        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wd / "launcher.ps1")],
        env=_child_env_with_native_pwd(wd, extra_env=extra_env),
    )
    (wd / PID_FILE_NAME).write_text(f"{proc.pid}\n", encoding="ascii")
    return proc.pid


class LockTimeoutError(RuntimeError):
    """OS 文件锁在超时时间内未能获取。"""


@contextmanager
def os_file_lock(
    lock_path: Path,
    *,
    timeout_sec: float = 15.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """进程退出即由内核自动释放的跨平台互斥文件锁（无无主锁窗口）。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_sec)
    try:
        # Ensure at least 1 byte exists for msvcrt.locking on Windows
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"Timed out waiting for lock: {lock_path}")
                time.sleep(poll_interval)
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
