"""ocsr_board.py — OCSR 只读网页面板（本机 127.0.0.1，零第三方依赖）。

只读观察 OCSR 已有证据文件，不参与派发、看门狗或遥测写入：
- 活跃/近期 worker：扫描 <temp>/ocsr_dispatch_*/<label>/ 下的
  worker-state.json / session-binding.json / start.marker / launcher.ps1
- 历史：~/.ocsr/dispatch-log.jsonl 尾部
面板自身唯一可写文件是 ~/.ocsr/board-groups.json（手动分组配置）。

端口策略：默认 8787；被旧 ocsr_board 占用时 KILL 旧实例后复用同端口
（等同重启）；被其他程序占用时依次向后探测 8788..8797。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
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
PORT_PROBE_LIMIT = 20

OCSR_HOME = Path(os.environ.get("OCSR_HOME") or (Path.home() / ".ocsr"))
DISPATCH_LOG = OCSR_HOME / "dispatch-log.jsonl"
GROUPS_PATH = OCSR_HOME / "board-groups.json"
INDEX_HTML = Path(__file__).resolve().parent / "board" / "index.html"

DIR_NAME_RE = re.compile(r"^ocsr_dispatch_(\d{8})_(\d{6})_[0-9a-f]+$")
MODEL_RE = re.compile(r"\s-m\s+([\w./-]+)")
EXIT_RE = re.compile(r"^exit=(\d+)$")

# worker-state.json 的 state 值域（见 ocsr_dispatch.py 写入点）
ACTIVE_STATES = {"launched", "inspection_due", "renewed", "reported"}
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


def scan_worker_dir(wd: Path, dispatch_name: str, started: datetime | None) -> dict[str, Any]:
    state = _load_json(wd / "worker-state.json")
    binding = _load_json(wd / "session-binding.json")
    raw_state = state.get("state") or "launched"
    badge, badge_text = BADGE_BY_STATE.get(raw_state, ("active", "● 进行中"))
    exit_code = _read_exit_code(wd / "start.marker")
    if exit_code not in (None, 0) and raw_state != "landed":
        badge, badge_text = "failed", "✕ 失败"
    elif badge == "active":
        # watcher 已不在推进的证据：进程正常退出但未标 landed，或已越过不可变总期限。
        # 面板只投影证据，不发明新事实——这类标为滞留而非进行中。
        deadline = state.get("total_deadline")
        overdue = isinstance(deadline, (int, float)) and time.time() > deadline
        if exit_code == 0:
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

    return {
        "key": f"{dispatch_name}/{wd.name}",
        "label": state.get("label") or wd.name,
        "dir": str(wd),
        "state": raw_state,
        "badge": badge,
        "badge_text": badge_text,
        "session_id": binding.get("session_id") or "",
        "cwd": binding.get("cwd") or "",
        "model": _extract_model(wd / "launcher.ps1"),
        "started_at": started_at.isoformat() if started_at else "",
        "duration_min": duration_min,
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
        if not entry.is_dir():
            continue
        started = parse_dir_start(entry.name)
        if started is None:
            continue
        if cutoff is not None and started < cutoff:
            continue
        try:
            labels = sorted(entry.iterdir())
        except OSError:
            continue
        for wd in labels:
            if wd.is_dir():
                workers.append(scan_worker_dir(wd, entry.name, started))
    return workers


def read_history(log_path: Path, limit: int = 200, tail_bytes: int = 2_000_000) -> list[dict[str, Any]]:
    """从 dispatch-log.jsonl 尾部读最近 limit 条；坏行跳过。"""
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            blob = fh.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").split("\n")
    if size > tail_bytes and lines:
        lines = lines[1:]  # 首行可能不完整
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("label"):
            records.append(entry)
    return records[-limit:]


def attach_telemetry(workers: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    """用同 label 且时间不早于启动的最近一条遥测补全 wall_min/产物等信息。"""
    by_label: dict[str, list[dict[str, Any]]] = {}
    for rec in history:
        by_label.setdefault(rec.get("label", ""), []).append(rec)
    for w in workers:
        cands = by_label.get(w["label"], [])
        if not cands:
            continue
        started_iso = w.get("started_at") or ""
        best = None
        for rec in cands:
            ts = rec.get("ts", "")
            if started_iso and ts and ts < started_iso[:10]:
                continue
            if best is None or rec.get("ts", "") > best.get("ts", ""):
                best = rec
        if best is None:
            continue
        w["wall_min"] = best.get("wall_min")
        w["artifact_bytes"] = best.get("artifact_bytes")
        w["outcome_detail"] = best.get("outcome_detail", "")
        w["read_audit"] = best.get("read_audit", "")
        if w["badge"] == "active" and w["duration_min"] is None:
            w["duration_min"] = w["wall_min"]


# ---------------------------------------------------------------------------
# 手动分组（面板私有文件，不触碰 OCSR 证据）
# ---------------------------------------------------------------------------

def load_assignments(path: Path = GROUPS_PATH) -> dict[str, str]:
    data = _load_json(path)
    raw = data.get("assignments")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def save_assignment(key: str, group: str, path: Path = GROUPS_PATH) -> dict[str, str]:
    assignments = load_assignments(path)
    if group:
        assignments[key] = group
    else:
        assignments.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"assignments": assignments}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return assignments


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
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
    else:
        os.kill(pid, 15)


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
                bind=_bind, sleep=time.sleep) -> tuple[int, str]:
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
    if identity:
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

class BoardServer(ThreadingHTTPServer):
    # Windows 上 allow_reuse_address 会让 bind 在端口被占用时也成功，必须禁用。
    allow_reuse_address = False
    daemon_threads = True


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "OCSRBoard/1"
    started_at = time.time()
    days = 7
    temp_root = Path(tempfile.gettempdir())
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

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/identity":
            self._json({"app": APP_NAME, "version": APP_VERSION, "pid": os.getpid(),
                        "port": self.server.server_address[1],
                        "started_at": self.started_at})
        elif path == "/api/state":
            workers = scan_workers(self.temp_root, self.days)
            history = read_history(self.log_path)
            attach_telemetry(workers, history)
            dir_labels = {w["key"] for w in workers}
            self._json({
                "workers": workers,
                "history": history,
                "assignments": load_assignments(self.groups_path),
                "generated_at": datetime.now().astimezone().isoformat(),
                "dir_count": len(dir_labels),
            })
        elif path == "/":
            try:
                body = INDEX_HTML.read_bytes()
            except OSError:
                self._send(500, b"index.html missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path != "/api/groups":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(length, 65536)).decode("utf-8"))
            key = str(data["key"])
            group = str(data.get("group", ""))
        except (ValueError, KeyError, json.JSONDecodeError):
            self._json({"error": "bad request"}, 400)
            return
        self._json({"assignments": save_assignment(key, group, self.groups_path)})


def main() -> int:
    ap = argparse.ArgumentParser(description="OCSR 只读网页面板")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--days", type=int, default=7, help="扫描最近 N 天的派发目录；0=全部")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    port, action = ensure_port(port=args.port)
    BoardHandler.days = args.days
    server = BoardServer(("127.0.0.1", port), BoardHandler)
    url = f"http://127.0.0.1:{port}"
    note = {"fresh": "", "restarted": "（旧实例已终止，同端口重启）",
            "moved": f"（{args.port} 被非面板程序占用，已换端口）"}[action]
    print(f"[board] {url} {note}  Ctrl+C 停止", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
