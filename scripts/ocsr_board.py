"""ocsr_board.py — OCSR 只读网页面板（本机 127.0.0.1，零第三方依赖）。

只读观察 OCSR 已有证据文件，不参与派发、看门狗或遥测写入：
- 活跃/近期 worker：扫描 <temp>/ocsr_dispatch_*/<label>/ 下的
  worker-state.json / session-binding.json / start.marker / launcher.ps1
- 历史：~/.ocsr/dispatch-log.jsonl 尾部
- 扫描根 = 默认 TEMP + CLI --work-dir + 面板「扫描目录」里在线增删的自定义根
面板自身唯一可写文件是 ~/.ocsr/board-groups.json（手动分组 assignments 与
自定义扫描根 roots）。

端口策略：默认 8787；被旧 ocsr_board 占用时 KILL 旧实例后复用同端口
（等同重启）；被其他程序占用时依次向后探测 8788..8797。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_NAME = "ocsr_board"
APP_VERSION = 1
DEFAULT_PORT = 8787
PORT_PROBE_LIMIT = 10  # 占用时探测 8788..8797

OCSR_HOME = Path.home() / ".ocsr"  # 与 dispatcher 的 DISPATCH_LOG 一致，不随 OCSR_HOME 环境变量分叉
DISPATCH_LOG = OCSR_HOME / "dispatch-log.jsonl"
GROUPS_PATH = OCSR_HOME / "board-groups.json"
INDEX_HTML = Path(__file__).resolve().parent / "board" / "index.html"
LOCK_PATH = OCSR_HOME / "board.lock"
_LAST_ACTIVITY = {"at": time.monotonic()}  # 有页面在看的最近请求时间（驱动空闲退出）

DIR_NAME_RE = re.compile(r"^ocsr_dispatch_(\d{8})_(\d{6})_[0-9a-f]+$")
MODEL_RE = re.compile(r"\s-m\s+(\S+)")
EXIT_RE = re.compile(r"^exit=(\d+)$")

# worker-state.json 的显式终态（见 ocsr_dispatch.py 写入点）；
# 其余值（launched/running/inspection_due/renewed/reported/terminating/unknown 等）
# 一律按活动态处理，再由退出码与 total_deadline 推导是否为滞留。
BADGE_BY_STATE = {
    "landed": ("landed", "✓ 已落盘"),
    "timed_out": ("timeout", "⚠ 超时"),
    "stopped": ("stopped", "■ 已停止"),
}


# ---------------------------------------------------------------------------
# 只读解析（所有读取容错：文件可能正被 watcher 写入，失败即跳过该字段）
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_dir_start(name: str) -> datetime | None:
    m = DIR_NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _extract_model(launcher: Path) -> str:
    try:
        text = launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = MODEL_RE.search(text)
    return m.group(1) if m else ""


def _read_exit_code(marker: Path) -> int | None:
    try:
        lines = marker.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = EXIT_RE.match(line.strip())
        if m:
            return int(m.group(1))
    return None


def _worker_fingerprint(wd: Path) -> tuple:
    """关键文件的 (mtime_ns, size) 指纹；未变则无需重读内容。"""
    fp = []
    for name in ("worker-state.json", "session-binding.json", "start.marker",
                 "launcher.ps1", "run.log"):
        try:
            st = (wd / name).stat()
            fp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            fp.append(None)
    return tuple(fp)


# 增量扫描缓存：worker 目录内容指纹 → 已解析的 worker 字典
_WORKER_CACHE: dict[str, dict[str, Any]] = {}


def _refresh_dynamic(w: dict[str, Any]) -> None:
    """缓存命中时重算时间相关字段：进行中时长、以及刚跨过 total_deadline 的滞留判定。"""
    started = _parse_iso(w.get("started_at"))
    if w["badge"] == "active":
        if started is not None:
            w["duration_min"] = round((datetime.now().astimezone() - started).total_seconds() / 60, 1)
        deadline = w.get("total_deadline")
        if isinstance(deadline, (int, float)) and time.time() > deadline:
            w["badge"], w["badge_text"] = "stale", "◌ 失联·超期"
            last = _parse_iso(w.get("last_activity"))
            if started is not None and last is not None:
                w["duration_min"] = round((last - started).total_seconds() / 60, 1)


def scan_worker_dir(wd: Path, dispatch_name: str, started: datetime | None,
                    root: Path | None = None) -> dict[str, Any]:
    """按目录指纹增量扫描：内容未变时复用缓存，仅重算动态字段。"""
    fp = _worker_fingerprint(wd)
    cached = _WORKER_CACHE.get(str(wd))
    if cached is not None and cached["fingerprint"] == fp:
        w = dict(cached["worker"])
        _refresh_dynamic(w)
        return w
    w = _scan_worker_dir_fresh(wd, dispatch_name, started, root)
    _WORKER_CACHE[str(wd)] = {"fingerprint": fp, "worker": dict(w)}
    return w


def _scan_worker_dir_fresh(wd: Path, dispatch_name: str, started: datetime | None,
                           root: Path | None = None) -> dict[str, Any]:
    state = _load_json(wd / "worker-state.json")
    binding = _load_json(wd / "session-binding.json")
    raw_state = state.get("state") or "launched"
    exit_code = _read_exit_code(wd / "start.marker")
    if raw_state in BADGE_BY_STATE:
        # watcher 写下的显式终态优先，不被 start.marker 退出码覆盖
        badge, badge_text = BADGE_BY_STATE[raw_state]
    else:
        # watcher 未推进的证据：进程退出但未标 landed，或已越过不可变总期限。
        # 面板只投影证据，不发明新事实——这类标为中性灰，不冒充进行中。
        badge, badge_text = "active", "● 进行中"
        deadline = state.get("total_deadline")
        overdue = isinstance(deadline, (int, float)) and time.time() > deadline
        if exit_code not in (None, 0):
            badge, badge_text = "failed", "✕ 失败"
        elif exit_code == 0:
            badge, badge_text = "stale", "◌ 已退出·未验收"
        elif overdue:
            badge, badge_text = "stale", "◌ 失联·超期"

    baseline = state.get("launch_baseline") or {}
    started_epoch = baseline.get("captured_at")
    started_at = None
    if isinstance(started_epoch, (int, float)) and started_epoch > 0:
        started_at = datetime.fromtimestamp(started_epoch).astimezone()
    elif started is not None:
        started_at = started.astimezone()

    last_activity = None
    for name in ("run.log", "worker-state.json"):
        try:
            mtime = (wd / name).stat().st_mtime
        except OSError:
            continue
        dt = datetime.fromtimestamp(mtime).astimezone()
        if last_activity is None or dt > last_activity:
            last_activity = dt

    duration_min = None
    if started_at is not None:
        if badge == "active":
            duration_min = round((datetime.now().astimezone() - started_at).total_seconds() / 60, 1)
        elif badge == "stale" and last_activity is not None:
            duration_min = round((last_activity - started_at).total_seconds() / 60, 1)

    # key 含扫描根短哈希：不同根下同名 dispatch/label 不共享隐藏/分组状态
    root_tag = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:8] if root else ""
    label = state.get("label")
    if not isinstance(label, str) or not label:
        label = wd.name  # 非字符串或空 label 用目录名兜底，避免绑定阶段按字典键抛错
    return {
        "key": f"{root_tag}/{dispatch_name}/{wd.name}" if root_tag else f"{dispatch_name}/{wd.name}",
        "label": label,
        "dir": str(wd),
        "state": raw_state,
        "badge": badge,
        "badge_text": badge_text,
        "session_id": binding.get("session_id") or "",
        "cwd": binding.get("cwd") or "",
        "model": _extract_model(wd / "launcher.ps1"),
        "started_at": started_at.isoformat() if started_at else "",
        "duration_min": duration_min,
        "total_deadline": state.get("total_deadline"),
        "wall_min": None,
        "exit_code": exit_code,
        "attempt_index": state.get("attempt_index"),
        "rework_index": state.get("rework_index"),
        "renewal_count": state.get("renewal_count"),
        "artifact_bytes": None,
        "outcome_detail": "",
        "read_audit": "",
        "last_activity": last_activity.isoformat() if last_activity else "",
    }


def scan_workers(temp_root: Path, days: int = 7) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    cutoff = datetime.now() - timedelta(days=days) if days > 0 else None
    try:
        entries = sorted(temp_root.iterdir())
    except OSError:
        return workers
    for entry in entries:
        # 先按目录名纯字符串过滤（零 IO），仅对名字匹配的候选做 is_dir/stat。
        # 否则大 TEMP 目录下会对全部条目逐个 stat（实测 5 万+ 次、约 2s）。
        if not DIR_NAME_RE.match(entry.name):
            continue
        started = parse_dir_start(entry.name)
        if started is None:
            continue
        if cutoff is not None and started < cutoff:
            continue
        if not entry.is_dir():
            continue
        try:
            labels = sorted(entry.iterdir())
        except OSError:
            continue
        for wd in labels:
            if wd.is_dir():
                workers.append(scan_worker_dir(wd, entry.name, started, root=entry.parent))
    # 淘汰已不在扫描集内的缓存项（目录被清理后不留存量）
    alive = {w["dir"] for w in workers}
    for key in list(_WORKER_CACHE):
        if key not in alive:
            del _WORKER_CACHE[key]
    return workers


# dispatch-log 增量读取缓存：记住上次读到的偏移，只解析新增完整行
_HISTORY_CACHE: dict[str, Any] = {"path": None, "offset": 0, "records": []}


def _parse_history_lines(lines: list[str], records: list[dict[str, Any]]) -> None:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not isinstance(label, str) or not label:
            continue  # 非字符串或空 label 会让 attach_telemetry 的字典键抛 TypeError
        records.append(entry)


def read_history(log_path: Path, limit: int = 200, tail_bytes: int = 2_000_000) -> list[dict[str, Any]]:
    """从 dispatch-log.jsonl 尾部读最近 limit 条；坏行跳过。

    增量：文件大小未变直接复用缓存；增长时只读并解析新增的完整行
    （不完整尾行留到下次），文件截断则全量重读。
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return []
    cache = _HISTORY_CACHE
    if cache["path"] == str(log_path) and cache["offset"] == size:
        return cache["records"][-limit:]
    if cache["path"] != str(log_path) or size < cache["offset"]:
        cache.update(path=str(log_path), offset=0, records=[])

    if cache["offset"] == 0:
        start = max(0, size - tail_bytes)
    else:
        start = cache["offset"]
    try:
        with log_path.open("rb") as fh:
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return cache["records"][-limit:]
    # 只消费到最后一个换行，不完整尾行留待下次（不推进偏移）
    last_nl = blob.rfind(b"\n")
    if last_nl < 0:
        return cache["records"][-limit:]
    complete = blob[: last_nl + 1]
    lines = complete.decode("utf-8", errors="replace").split("\n")
    if start > 0 and cache["offset"] == 0 and size > tail_bytes and lines:
        lines = lines[1:]  # 尾部窗口首行可能不完整
    _parse_history_lines(lines, cache["records"])
    cache["records"] = cache["records"][-limit:]
    cache["offset"] = start + last_nl + 1
    return cache["records"][-limit:]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ts_key(rec: dict[str, Any]) -> float:
    dt = _parse_iso(rec.get("ts"))
    return dt.timestamp() if dt else 0.0


def attach_telemetry(workers: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    """补全 wall_min/产物等。同 label 可能串行多次运行、且一次运行可能写多条
    遥测（hierarchical_report 先写 reported 中间行、后写终态行）。规则：
    同 label 的 workers 按 started_ts 排序，每个 worker 的候选区间为
    [started_ts, 同 label 下一个 worker 的 started_ts)，归属区间互不重叠——
    旧 worker 不会被同 label 下一次运行的终态行越界绑定；区间内取最早一条
    **终态行**，既不串到上一次运行，也不把中间行的检查时刻时长/log 大小当成
    最终值。终态行 = 非 reported: 前缀，或 reported:handoff_required——后者
    是 dispatcher 走到不可变总期限、hierarchical_report 交回上层时写的终态
    遥测，参与绑定；其余 reported:<status> 才是中间行，跳过。无 started_at
    或区间内无终态行的 worker 不绑定（保留目录证据原样）。遥测 error/timeout
    校正徽章。"""
    by_label: dict[str, list[dict[str, Any]]] = {}
    for rec in history:
        by_label.setdefault(rec.get("label", ""), []).append(rec)
    for recs in by_label.values():
        recs.sort(key=_ts_key)

    # 同 label 的 worker 依启动时间排序，构成互不重叠的归属区间
    ws_by_label: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for w in workers:
        started = _parse_iso(w.get("started_at"))
        if started is None:
            continue  # 无 started_at 无法划归属区间，不绑定遥测
        ws_by_label.setdefault(w["label"], []).append((started.timestamp(), w))
    for pairs in ws_by_label.values():
        pairs.sort(key=lambda p: p[0])

    for label, pairs in ws_by_label.items():
        cands = by_label.get(label, [])
        for i, (started_ts, w) in enumerate(pairs):
            upper = pairs[i + 1][0] if i + 1 < len(pairs) else None
            best = None
            for rec in cands:
                ts = _ts_key(rec)
                if ts < started_ts:
                    continue  # 属于更早的运行
                if upper is not None and ts >= upper:
                    break  # 越过区间上界：属于同 label 下一次运行
                od = rec.get("outcome_detail")
                # 中间行（reported:<status>）不是本次运行的最终状态；
                # reported:handoff_required 是总期限交回的终态行，参与绑定
                if (isinstance(od, str) and od.startswith("reported:")
                        and od != "reported:handoff_required"):
                    continue
                best = rec
                break
            if best is None:
                continue
            od = best.get("outcome_detail")
            w["wall_min"] = best.get("wall_min")
            w["artifact_bytes"] = best.get("artifact_bytes")
            w["outcome_detail"] = od if isinstance(od, str) else ""
            read_audit = best.get("read_audit", "")
            w["read_audit"] = read_audit if isinstance(read_audit, str) else ""
            # exit_0_no_artifact 等失败只存在于遥测（worker-state 无 failed 值域），
            # 据此校正；watcher 显式终态（landed/stopped/timed_out）不动。
            if w["badge"] in ("active", "stale"):
                if w["outcome_detail"].startswith("error"):
                    w["badge"], w["badge_text"] = "failed", "✕ 失败"
                elif (w["outcome_detail"].startswith("stall:")
                        or w["outcome_detail"].startswith("killed:")):
                    w["badge"], w["badge_text"] = "timeout", "⚠ 超时"
            if w["badge"] == "active" and w["duration_min"] is None:
                w["duration_min"] = w["wall_min"]


# ---------------------------------------------------------------------------
# 手动分组（面板私有文件，不触碰 OCSR 证据）
# ---------------------------------------------------------------------------

def load_config(path: Path = GROUPS_PATH) -> dict[str, Any]:
    data = _load_json(path)
    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    assignments = {str(k): str(v) for k, v in assignments.items() if isinstance(v, str)}
    roots = data.get("roots")
    if not isinstance(roots, list):
        roots = []
    roots = [r for r in roots if isinstance(r, str) and r]
    return {"assignments": assignments, "roots": roots}


def load_assignments(path: Path = GROUPS_PATH) -> dict[str, str]:
    return load_config(path)["assignments"]


def _write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    # 配置变更立即使缓存失效并递增代次；build_state 回填前核对代次，防止
    # 扫描期间发生的配置写被旧扫描结果覆盖回缓存。失效与回填互斥于同一
    # _STATE_LOCK（锁顺序保持 _CONFIG_LOCK → _STATE_LOCK，不反向嵌套）
    with _STATE_LOCK:
        _STATE_CACHE["at"] = 0.0
        _STATE_CACHE["gen"] += 1


# ThreadingHTTPServer 下两个写端点共用此锁，保护配置的读-改-写与固定 tmp 文件名
_CONFIG_LOCK = threading.Lock()

# /api/state 扫描缓存：Windows + Defender 下单次全量扫描需秒级，
# 多客户端轮询共享 TTL 内的缓存，避免每个请求都全量重扫拖满 CPU。
_STATE_CACHE: dict[str, Any] = {"at": 0.0, "payload": None, "gen": 0}
_STATE_CACHE_TTL = 10.0  # 秒
# 扫描互斥（与配置写锁分离）；用可重入 RLock：同线程内配置写触发的失效
# 需嵌套获取本锁（见 _write_config），跨线程互斥语义不变
_STATE_LOCK = threading.RLock()


def save_assignment(key: str, group: str, path: Path = GROUPS_PATH) -> dict[str, str]:
    with _CONFIG_LOCK:
        config = load_config(path)
        assignments = config["assignments"]
        if group:
            assignments[key] = group
        else:
            assignments.pop(key, None)
        _write_config(config, path)
        return assignments


def effective_roots(base: Path, path: Path = GROUPS_PATH,
                    extra: list[Path] | None = None) -> list[Path]:
    """默认 TEMP + CLI --work-dir + 配置里额外添加且仍存在的扫描根（去重，保持顺序）。"""
    base_resolved = str(base.resolve())
    roots = [base]
    seen = {base_resolved}
    for p in list(extra or []) + [Path(r) for r in load_config(path)["roots"]]:
        try:
            resolved = str(p.resolve())
            if p.is_dir() and resolved not in seen:
                roots.append(p)
                seen.add(resolved)
        except OSError:
            continue
    return roots


def mutate_roots(action: str, raw_path: Any, base: Path,
                 path: Path = GROUPS_PATH) -> tuple[list[str], str]:
    """增删额外扫描根。返回 (roots, 错误信息)；错误非空即失败。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return [], "路径为空或类型错误"
    if any(ord(c) < 32 for c in raw_path) or len(raw_path) > 512:
        return [], "路径含控制字符或超长"
    p = Path(raw_path.strip())
    with _CONFIG_LOCK:
        config = load_config(path)
        roots = config["roots"]
        if action == "add":
            if not p.is_absolute() or not p.is_dir():
                return [], "必须是存在的绝对路径目录"
            resolved = str(p.resolve())
            if resolved == str(base.resolve()):
                return [], "该目录已是默认扫描根"
            if resolved in roots:
                return [], "该目录已在列表中"
            roots.append(resolved)
        elif action == "remove":
            if raw_path.strip() not in roots:
                return [], "只能移除额外添加的扫描根"
            roots.remove(raw_path.strip())
        else:
            return [], "未知 action"
        _write_config(config, path)
        return roots, ""


def default_group(worker: dict[str, Any]) -> str:
    return worker.get("cwd") or "（未知工作目录）"


# ---------------------------------------------------------------------------
# 端口与旧实例重启
# ---------------------------------------------------------------------------

def probe_board_identity(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> dict[str, Any]:
    """占用者是本面板旧实例时返回其 identity（含 pid），否则 {}。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/identity", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and data.get("app") == APP_NAME and isinstance(data.get("pid"), int):
        return data
    return {}


def kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass  # 目标可能已退出；由后续 bind 结果判定端口是否释放


def verify_board_process(pid: int) -> bool:
    """KILL 前二次核对目标进程命令行确为 ocsr_board.py（防伪 identity 误杀）。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                capture_output=True, text=True, timeout=10)
            return "ocsr_board.py" in (out.stdout or "")
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        return "ocsr_board.py" in cmdline
    except (OSError, subprocess.SubprocessError):
        return False


def _restart_existing_instance() -> None:
    """单实例：锁文件指向活面板进程时 KILL 后重启（与 ensure_port 的重启语义一致）。"""
    data = _load_json(LOCK_PATH)
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return
    if not verify_board_process(pid):
        return
    port = data.get("port")
    if isinstance(port, int) and probe_board_identity(port).get("pid") != pid:
        return  # 端口身份与锁文件不符，不动
    kill_pid(pid)
    time.sleep(1)


def _write_lock(port: int) -> None:
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "port": port}) + "\n",
                             encoding="utf-8", newline="\n")
    except OSError:
        pass


def _clear_lock() -> None:
    try:
        data = _load_json(LOCK_PATH)
        if data.get("pid") == os.getpid():
            LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _idle_watchdog(server: ThreadingHTTPServer, timeout_sec: float) -> None:
    """长时间无页面请求（无人在看）时自动退出，避免孤儿面板常驻。"""
    while True:
        time.sleep(30)
        if time.monotonic() - _LAST_ACTIVITY["at"] > timeout_sec:
            server.shutdown()
            return


def _bind(host: str, port: int) -> socket.socket:
    # 不得设 SO_REUSEADDR：Windows 上它允许强行绑定已占用端口，
    # 会让占用探测失效（两个面板共享同一端口）。
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    finally:
        s.close()
    return s


def ensure_port(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                prober=probe_board_identity, killer=kill_pid,
                bind=_bind, sleep=time.sleep,
                verifier=verify_board_process) -> tuple[int, str]:
    """返回 (实际端口, 动作)。动作 ∈ fresh / restarted / moved。

    端口被旧 ocsr_board 占用 → KILL 旧实例后复用同端口（restarted）；
    被其他程序占用 → 顺序探测后续端口（moved）。
    """
    try:
        bind(host, port)
        return port, "fresh"
    except OSError:
        pass

    identity = prober(port, host)
    if identity and verifier(identity["pid"]):
        killer(identity["pid"])
        for _ in range(24):  # 最多等 ~6s 让端口释放
            sleep(0.25)
            try:
                bind(host, port)
                return port, "restarted"
            except OSError:
                continue
        raise SystemExit(f"[board] 旧实例(pid={identity['pid']})已终止但端口 {port} 未释放")

    for candidate in range(port + 1, port + 1 + PORT_PROBE_LIMIT):
        try:
            bind(host, candidate)
            return candidate, "moved"
        except OSError:
            continue
    raise SystemExit(f"[board] 端口 {port}..{port + PORT_PROBE_LIMIT} 均被占用")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_state(handler: "BoardHandler", force: bool = False) -> dict[str, Any]:
    """/api/state 的数据构建，带 TTL 缓存（fresh=1 强制重扫并刷新缓存）。

    代次防护：扫描期间若发生配置写（gen 递增），放弃本次回填并重扫，
    避免旧配置的扫描结果覆盖写后缓存。
    """
    with _STATE_LOCK:
        if not force and _STATE_CACHE["payload"] is not None \
                and time.monotonic() - _STATE_CACHE["at"] < _STATE_CACHE_TTL:
            return _STATE_CACHE["payload"]
        for _ in range(2):
            gen_before = _STATE_CACHE["gen"]
            cfg = load_config(handler.groups_path)
            custom = cfg["roots"]
            roots = effective_roots(handler.base_root, handler.groups_path, handler.cli_roots)
            workers: list[dict[str, Any]] = []
            for root in roots:
                workers.extend(scan_workers(root, handler.days))
            history = read_history(handler.log_path)
            attach_telemetry(workers, history)
            payload = {
                "workers": workers,
                "history": history,
                "assignments": cfg["assignments"],
                "roots": [{"path": str(r), "kind": "default" if r == handler.base_root
                           else ("cli" if r in handler.cli_roots else "custom")}
                          for r in roots],
                # 自定义根按配置原样列出（含已失效项），失效也可移除，不留幽灵配置
                "custom_roots": [{"path": raw, "exists": Path(raw).is_dir()} for raw in custom],
                "generated_at": datetime.now().astimezone().isoformat(),
                "cache_ttl": _STATE_CACHE_TTL,
            }
            if _STATE_CACHE["gen"] == gen_before:
                _STATE_CACHE.update(at=time.monotonic(), payload=payload)
                return payload
        return payload  # 极端连续写入：直接返回最新扫描，不写缓存


class BoardServer(ThreadingHTTPServer):
    # Windows 禁用 reuse（它会允许强行绑定已占用端口，使占用探测失效）；
    # POSIX 保留 reuse，避免旧实例 TIME_WAIT 连接导致同端口重启失败。
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def host_allowed(host_header: str, port: int) -> bool:
    """仅接受本机回环 Host（防 DNS rebinding 读取本机路径/session）。"""
    host = (host_header or "").strip().lower()
    if host.startswith("[") and "]" in host:  # IPv6 形式 [::1]:8787
        name, _, rest = host[1:].partition("]")
        return name == "::1" and rest in ("", f":{port}")
    name, sep, p = host.rpartition(":")
    if sep:
        try:
            if int(p) != port:
                return False
        except ValueError:
            return False
    else:
        name = host
    return name in ("127.0.0.1", "localhost")


def validate_group_payload(data: Any) -> tuple[str, str] | None:
    """校验 /api/groups 输入；合法返回 (key, group)，否则 None。group 空=清除分配。"""
    if not isinstance(data, dict):
        return None
    key, group = data.get("key"), data.get("group", "")
    if not isinstance(key, str) or not isinstance(group, str):
        return None
    if not (1 <= len(key) <= 512) or len(group) > 120:
        return None
    if any(ord(c) < 32 for c in key + group):
        return None
    return key, group


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "OCSRBoard/1"
    started_at = time.time()
    days = 7
    base_root = Path(tempfile.gettempdir())
    cli_roots: list[Path] = []
    log_path = DISPATCH_LOG
    groups_path = GROUPS_PATH

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _origin_ok(self) -> bool:
        if not host_allowed(self.headers.get("Host", ""), self.server.server_address[1]):
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self) -> None:
        if not self._origin_ok():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/identity":
            self._json({"app": APP_NAME, "version": APP_VERSION, "pid": os.getpid(),
                        "port": self.server.server_address[1],
                        "started_at": self.started_at})
        elif path == "/api/state":
            _LAST_ACTIVITY["at"] = time.monotonic()
            self._json(build_state(self, force="fresh=1" in self.path))
        elif path == "/":
            _LAST_ACTIVITY["at"] = time.monotonic()
            try:
                body = INDEX_HTML.read_bytes()
            except OSError:
                self._send(500, b"index.html missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 65536:
            raise ValueError("bad content length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self) -> None:
        if not self._origin_ok():
            return
        # 只接受 application/json（非 simple request，跨源浏览器表单无法伪造）
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "unsupported content type"}, 415)
            return
        try:
            self._do_post()
        except OSError as exc:
            self._json({"error": f"config io error: {exc}"}, 500)

    def _do_post(self) -> None:
        if self.path == "/api/shutdown":
            # 优雅退出：先回响应，再异步关闭 server（仅本机回环 + application/json）
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/api/roots":
            try:
                data = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "bad request"}, 400)
                return
            roots, err = mutate_roots(str(data.get("action", "")) if isinstance(data, dict) else "",
                                      data.get("path") if isinstance(data, dict) else None,
                                      self.base_root, self.groups_path)
            if err:
                self._json({"error": err}, 400)
                return
            self._json({"roots": roots})
            return
        if self.path != "/api/groups":
            self._json({"error": "not found"}, 404)
            return
        try:
            data = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad request"}, 400)
            return
        payload = validate_group_payload(data)
        if payload is None:
            self._json({"error": "bad request"}, 400)
            return
        self._json({"assignments": save_assignment(payload[0], payload[1], self.groups_path)})


def main() -> int:
    ap = argparse.ArgumentParser(description="OCSR 只读网页面板")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--days", type=int, default=7, help="扫描最近 N 天的派发目录；0=全部")
    ap.add_argument("--work-dir", action="append", default=None,
                    help="在默认 $TEMP 之外追加派发扫描根（可重复）；也可在面板「扫描目录」中在线增删并持久化")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--idle-timeout", type=int, default=30,
                    help="无页面请求的空闲退出分钟数（默认 30；0=禁用）")
    args = ap.parse_args()

    _restart_existing_instance()  # 单实例：已有活面板则 KILL 后重启
    BoardHandler.days = args.days
    if args.work_dir:
        BoardHandler.cli_roots = [Path(d) for d in args.work_dir]

    # 探测与正式 bind 之间端口可能被抢占：bind 失败时重走 ensure_port（自动换端口/重启判断）
    server = None
    for _ in range(2):
        port, action = ensure_port(port=args.port)
        try:
            server = BoardServer(("127.0.0.1", port), BoardHandler)
            break
        except OSError:
            continue
    if server is None:
        print(f"[board] 端口 {args.port} 附近均被占用或绑定失败", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}"
    note = {"fresh": "", "restarted": "（旧实例已终止，同端口重启）",
            "moved": f"（{args.port} 被非面板程序占用，已换端口）"}[action]
    print(f"[board] {url} {note}  Ctrl+C 停止", flush=True)
    _write_lock(port)
    if args.idle_timeout > 0:
        threading.Thread(target=_idle_watchdog, args=(server, args.idle_timeout * 60),
                         daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _clear_lock()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
