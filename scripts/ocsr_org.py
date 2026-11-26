#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocsr_org.py — OCSR 单元组合式 CLI 调度框架（后端严密、表面简单）。

面向 Agent 的 5 个直观命令：
  init   <goal> --budget <N>              初始化任务树
  spawn  <role> -m <model> -p <prompt>    派发 orchestrator / worker / reviewer（支持 --wait）
  wait   [unit_ids...] [--any]            等待指定单元或当前所有活跃子单元
  status [unit_id]                        无参看整棵组织树；带 ID 看单元详情与产物
  stop   [unit_id]                        停止单元子树或整个 run

后端自动处理：OS 文件锁、两级幂等、三段式预算守恒与自动回收、跨 run 工作树写租约、
调用者身份绑定、Codex 机械只读 reviewer、读时惰性对账与 summary.json 合并。

退出码：
  0 = 成功（含 wait 有界到期返回 state="running"）
  1 = 基础设施异常（锁超时、I/O 错误）
  2 = 参数或规则拒绝（超预算、超深度、叶子派发、租约冲突、converge 禁入等）
"""

from __future__ import annotations

import argparse
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
from pathlib import Path
from typing import Any

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import ocsr_backends as _backends
import ocsr_runtime as _runtime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

EXIT_OK = 0
EXIT_INFRA_ERROR = 1
EXIT_CONTRACT_ERROR = 2

DEFAULT_RUNS_ROOT = Path.home() / ".ocsr" / "runs"
ORG_ROOTS_REGISTRY = Path.home() / ".ocsr" / "org-roots.json"
OPENCODE_ALLOWLIST_PATH = Path.home() / ".ocsr" / "allowed-models.json"
CODEX_ALLOWLIST_PATH = Path.home() / ".ocsr" / "allowed-codex-models.json"

VIRTUAL_ROOT_ID = "u0000-root"
DEFAULT_MAX_DEPTH = 3
DEFAULT_UNIT_TIMEOUT_SEC = 1800
DEFAULT_WAIT_TIMEOUT_SEC = 90
MAX_WAIT_TIMEOUT_SEC = 100
DEFAULT_POLL_INTERVAL_SEC = 1.0
STARTUP_GRACE_SEC = 5.0
RESERVED_GRACE_SEC = 30.0
INSPECT_TAIL_MAX_LINES = 60
INSPECT_TAIL_MAX_BYTES = 4096

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "aborted", "uncertain"})
VALID_ROLES = frozenset({"orchestrator", "worker", "executor", "reviewer"})
CONVERGE_ENV_VARS = (
    "CONVERGE_RUN_ID",
    "CONVERGE_LEDGER",
    "CONVERGE_LEDGER_DIR",
    "OCSR_CONVERGE_LEDGER",
)

_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("kimi", "kimi"),
    ("k2", "kimi"),
    ("k3", "kimi"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("claude", "anthropic"),
    ("gemini", "google"),
    ("glm", "zhipu"),
    ("minimax", "minimax"),
)


class OrgError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_CONTRACT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _now_epoch() -> float:
    return time.time()


def _parse_iso_epoch(ts: str) -> float:
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _register_org_root(runs_root: Path) -> None:
    """将本次 runs_root 登记到 ~/.ocsr/org-roots.json（近 30 天），供 ocsr_board 只读自动发现。"""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("OCSR_DISABLE_MODEL_CALLS") == "1":
        return
    try:
        path = ORG_ROOTS_REGISTRY
        data = _runtime._read_json_object(path) or {}
        roots = data.get("roots") if isinstance(data.get("roots"), list) else []
        now = datetime.datetime.now().astimezone()
        resolved = str(runs_root.resolve())
        roots = [r for r in roots if isinstance(r, dict) and r.get("path") != resolved]
        roots.append({"path": resolved, "last_used": now.isoformat()})
        cutoff = now - datetime.timedelta(days=30)
        kept = []
        for r in roots:
            try:
                if datetime.datetime.fromisoformat(str(r.get("last_used", ""))) >= cutoff:
                    kept.append(r)
            except (ValueError, TypeError):
                continue
        _runtime.atomic_write_json(path, {"roots": kept})
    except OSError:
        pass


def _runs_root_from_args(run_root: str | None) -> Path:
    if run_root:
        return Path(run_root).expanduser().resolve()
    env_root = os.environ.get("OCSR_ORG_RUNS_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return DEFAULT_RUNS_ROOT


def _resolve_run_id(runs_root: Path, explicit_run: str | None) -> str:
    env_run = os.environ.get("OCSR_RUN_ID", "").strip()
    if explicit_run:
        if env_run and explicit_run != env_run:
            raise OrgError(
                f"Caller identity mismatch: OCSR_RUN_ID='{env_run}' != --run '{explicit_run}'.",
                EXIT_CONTRACT_ERROR,
            )
        return explicit_run
    if env_run:
        return env_run
    latest_file = runs_root / "latest-run.txt"
    if latest_file.is_file():
        rid = latest_file.read_text(encoding="utf-8", errors="replace").strip()
        if rid and (runs_root / rid).is_dir():
            return rid
    raise OrgError("No active run found. Pass --run <run_id> or run `init` first.", EXIT_CONTRACT_ERROR)


def _check_converge_prohibited(extra_converge_ledger: str | None = None) -> None:
    if extra_converge_ledger:
        raise OrgError(
            "Phase 1 ocsr_org is prohibited inside converge contexts (--converge-ledger rejected).",
            EXIT_CONTRACT_ERROR,
        )
    for var in CONVERGE_ENV_VARS:
        if os.environ.get(var, "").strip():
            raise OrgError(
                f"Phase 1 ocsr_org is prohibited inside converge contexts (env {var} is set).",
                EXIT_CONTRACT_ERROR,
            )


def _normalize_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r == "executor":
        return "worker"
    if r not in {"orchestrator", "worker", "reviewer"}:
        raise OrgError(f"Unsupported role '{role}'. Expected orchestrator, worker/executor, or reviewer.")
    return r


def _detect_backend(model: str, explicit_backend: str | None) -> str:
    if explicit_backend:
        return explicit_backend.strip().lower()
    return _backends.BACKEND_OPENCODE if "/" in model else _backends.BACKEND_CODEX


def _infer_family(model: str, explicit_family: str = "") -> str:
    if explicit_family and explicit_family.strip():
        return explicit_family.strip().lower()
    slug = model.split("/", 1)[-1].lower()
    for token, fam in _FAMILY_PATTERNS:
        if token in slug:
            return fam
    return "unknown"


def _load_opencode_allowlist() -> frozenset[str]:
    try:
        raw = json.loads(OPENCODE_ALLOWLIST_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    items = raw.get("models", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    models = set()
    for item in items:
        if isinstance(item, str) and item.strip():
            models.add(item.strip())
        elif isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"].strip():
            models.add(item["model"].strip())
    return frozenset(models)


def _load_codex_allowlist() -> dict[str, dict[str, Any]]:
    return _backends.load_codex_allowed_models(CODEX_ALLOWLIST_PATH)


def _norm_path_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _git_run(cwd: Path, args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=15)
        return proc.returncode, _runtime._decode_native(proc.stdout).strip(), _runtime._decode_native(proc.stderr).strip()
    except Exception as exc:
        return 1, "", str(exc)


def _resolve_git_worktree_info(target_dir: Path) -> dict[str, str] | None:
    rc, top, _ = _git_run(target_dir, ["rev-parse", "--show-toplevel"])
    if rc != 0 or not top:
        return None
    _, branch, _ = _git_run(target_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    _, head_sha, _ = _git_run(target_dir, ["rev-parse", "HEAD"])
    return {
        "toplevel": _norm_path_key(Path(top)),
        "toplevel_display": str(Path(top).resolve()),
        "branch": branch or "HEAD",
        "head_sha": head_sha,
    }


def _lease_file_path(runs_root: Path, norm_worktree_key: str) -> Path:
    digest = hashlib.sha256(norm_worktree_key.encode("utf-8")).hexdigest()
    return runs_root / "leases" / f"{digest}.json"


def _append_ledger_event(run_dir: Path, event: dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with (run_dir / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()


def _read_ledger_events(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    ledger_path = run_dir / "ledger.jsonl"
    if not ledger_path.is_file():
        return [], 0
    events: list[dict[str, Any]] = []
    bad_lines = 0
    try:
        raw = ledger_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                events.append(obj)
            else:
                bad_lines += 1
        except json.JSONDecodeError:
            bad_lines += 1
    return events, bad_lines


def _load_run_config(run_dir: Path) -> dict[str, Any]:
    cfg = _runtime._read_json_object(run_dir / "run.json")
    if cfg is None:
        raise OrgError(f"Run config not found: {run_dir / 'run.json'}", EXIT_CONTRACT_ERROR)
    return cfg


def _load_run_state(run_dir: Path) -> dict[str, Any]:
    return _runtime._read_json_object(run_dir / "run-state.json") or {"status": "active", "updated_at": _now_iso()}


def _load_unit_spec(run_dir: Path, unit_id: str) -> dict[str, Any]:
    spec = _runtime._read_json_object(run_dir / "units" / unit_id / "spec.json")
    if spec is None:
        raise OrgError(f"Unit '{unit_id}' not found in run '{run_dir.name}'.", EXIT_CONTRACT_ERROR)
    return spec


def _compute_run_projection(run_dir: Path) -> dict[str, Any]:
    run_cfg = _load_run_config(run_dir)
    run_state = _load_run_state(run_dir)
    events, bad_lines = _read_ledger_events(run_dir)

    max_calls = int(run_cfg["max_calls"])
    units: dict[str, dict[str, Any]] = {}
    children_map: dict[str, list[str]] = {}

    units_dir = run_dir / "units"
    if units_dir.is_dir():
        for entry in sorted(units_dir.iterdir()):
            if not entry.is_dir():
                continue
            spec = _runtime._read_json_object(entry / "spec.json")
            if spec is None:
                continue
            uid = str(spec["unit_id"])
            is_virtual = bool(spec.get("virtual", False))
            units[uid] = {
                "unit_id": uid,
                "parent_id": spec.get("parent_id"),
                "depth": int(spec.get("depth", 0)),
                "role": spec.get("role", "worker"),
                "virtual": is_virtual,
                "backend": spec.get("backend", ""),
                "model": spec.get("model", ""),
                "family": spec.get("family", ""),
                "budget_alloc": int(spec.get("budget_alloc", 0)),
                "available": max_calls if is_virtual else 0,
                "held": 0,
                "status": "virtual" if is_virtual else "reserved",
                "spawned": False,
                "settled_in_ledger": False,
                "budget_settled": False,
                "exit_code": None,
                "warnings": [],
                "error_reason": None,
                "created_at": spec.get("created_at", ""),
                "terminal_at": None,
                "spec": spec,
            }
            parent_id = spec.get("parent_id")
            if parent_id:
                children_map.setdefault(str(parent_id), []).append(uid)

    consumed = 0
    for ev in events:
        etype = ev.get("type")
        uid = ev.get("unit_id")
        if etype == "unit_reserved" and uid in units:
            u = units[uid]
            parent_id = ev.get("parent_id") or u.get("parent_id")
            child_budget = int(ev.get("budget_alloc", u["budget_alloc"]))
            cost = 1 + (child_budget if u["role"] == "orchestrator" else 0)
            if parent_id in units:
                units[parent_id]["available"] -= cost
            consumed += 1
            if u["role"] == "orchestrator":
                u["available"] += child_budget
            u["status"] = "reserved"
        elif etype == "unit_spawned" and uid in units:
            units[uid]["spawned"] = True
            if units[uid]["status"] == "reserved":
                units[uid]["status"] = "running"
        elif etype == "unit_terminal" and uid in units:
            u = units[uid]
            if not u["settled_in_ledger"]:
                u["settled_in_ledger"] = True
                u["status"] = str(ev.get("status", "uncertain"))
                u["exit_code"] = ev.get("exit_code")
                u["warnings"] = list(ev.get("warnings") or [])
                u["error_reason"] = ev.get("error_reason")
                u["terminal_at"] = ev.get("ts")
        elif etype == "budget_returned" and uid in units:
            u = units[uid]
            if not u["budget_settled"]:
                amt = int(ev.get("amount", 0))
                u["available"] -= amt
                parent_id = ev.get("parent_id") or u.get("parent_id")
                if parent_id in units:
                    units[parent_id]["available"] += amt
                u["budget_settled"] = True
        elif etype == "budget_held" and uid in units:
            u = units[uid]
            if not u["budget_settled"]:
                amt = int(ev.get("amount", 0))
                u["available"] -= amt
                u["held"] += amt
                u["budget_settled"] = True
        elif etype == "budget_released" and uid in units:
            u = units[uid]
            amt = int(ev.get("amount", 0))
            if amt > 0 and u["held"] >= amt:
                u["held"] -= amt
                parent_id = ev.get("parent_id") or u.get("parent_id")
                if parent_id in units:
                    units[parent_id]["available"] += amt

    total_held = sum(u["held"] for u in units.values())
    total_reserved = sum(u["available"] for u in units.values() if not u["virtual"] and u["available"] > 0)
    root_available = units[VIRTUAL_ROOT_ID]["available"] if VIRTUAL_ROOT_ID in units else 0

    return {
        "run": run_cfg,
        "run_state": run_state,
        "units": units,
        "children_map": children_map,
        "bad_ledger_lines": bad_lines,
        "budget": {
            "max_calls": max_calls,
            "consumed": consumed,
            "reserved": total_reserved,
            "held": total_held,
            "root_available": root_available,
            "invariant_ok": (consumed + total_reserved + total_held <= max_calls)
            and (consumed + total_reserved + total_held + root_available == max_calls),
        },
    }


def _collect_unit_artifacts(unit_dir: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    artifacts: list[dict[str, Any]] = []

    def _add(p: Path) -> None:
        if not p.is_file():
            return
        norm = _norm_path_key(p)
        if norm in seen:
            return
        seen.add(norm)
        try:
            size, sha = _runtime.file_sha256(p)
        except OSError:
            return
        artifacts.append({"path": str(p.resolve()), "size": size, "sha256": sha})

    art_dir = unit_dir / "artifacts"
    if art_dir.is_dir():
        for p in sorted(art_dir.rglob("*")):
            if p.is_file():
                _add(p)
    for exp in spec.get("expected_outputs") or []:
        _add(Path(exp))
    return artifacts


def _unit_process_liveness(unit_dir: Path, table: dict[int, dict]) -> tuple[str, int | None, set[int]]:
    pid = _runtime._read_pid(unit_dir)
    if pid is None:
        return "no_pid", None, set()
    if not table:
        return "unknown", pid, set()
    identity = _runtime._read_json_object(unit_dir / _runtime.PROCESS_IDENTITY_FILE)
    creation_time = str(identity.get("creation_time") or "").strip() if identity else ""
    status = _runtime.check_process_status(pid, creation_time, table=table)
    desc = _runtime._descendant_pids(pid, table, root_creation_time=creation_time)
    if status == "running" or desc:
        return "running", pid, desc
    return status, pid, set()


def _release_worktree_lease_if_owned(runs_root: Path, run_id: str, unit_id: str, spec: dict[str, Any]) -> None:
    wt = spec.get("worktree_ref")
    if not isinstance(wt, dict) or not wt.get("toplevel"):
        return
    lease_path = _lease_file_path(runs_root, str(wt["toplevel"]))
    with _runtime.os_file_lock(runs_root / "leases.lock"):
        cur = _runtime._read_json_object(lease_path)
        if cur and cur.get("run_id") == run_id and cur.get("unit_id") == unit_id:
            try:
                lease_path.unlink(missing_ok=True)
            except OSError:
                pass


def _build_or_merge_orchestrator_summary(
    run_dir: Path,
    unit_id: str,
    proj: dict[str, Any],
    unit_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    unit_dir = run_dir / "units" / unit_id
    summary_path = unit_dir / "summary.json"
    existing = _runtime._read_json_object(summary_path) or {}
    children_summary = []
    for cid in proj["children_map"].get(unit_id, []):
        cu = proj["units"][cid]
        cres = _runtime._read_json_object(run_dir / "units" / cid / "result.json") or {}
        children_summary.append({
            "unit_id": cid,
            "role": cu["role"],
            "model": cu["model"],
            "family": cu["family"],
            "status": cu["status"],
            "artifacts": cres.get("artifacts", []),
            "warnings": cu.get("warnings", []),
        })
    u = proj["units"][unit_id]
    merged = {
        "unit_id": unit_id,
        "role": "orchestrator",
        "status": u["status"],
        "children": children_summary,
        "artifacts": unit_artifacts,
        "budget": {
            "alloc": u["budget_alloc"],
            "remaining_available": u["available"],
            "held": u["held"],
        },
        "decision": existing.get("decision"),
        "unresolved": existing.get("unresolved"),
        "self_executed": bool(existing.get("self_executed", False)),
        "updated_at": _now_iso(),
    }
    _runtime.atomic_write_json(summary_path, merged)
    return merged


def _subtree_is_confirmed_dead(run_dir: Path, proj: dict[str, Any], root_uid: str, table: dict[int, dict]) -> bool:
    if not table:
        return False
    stack = [root_uid]
    while stack:
        uid = stack.pop()
        stack.extend(proj["children_map"].get(uid, []))
        unit_dir = run_dir / "units" / uid
        u = proj["units"][uid]
        if u["virtual"]:
            continue
        if not u["spawned"] and not (unit_dir / _runtime.PID_FILE_NAME).is_file():
            continue
        liveness, _, desc = _unit_process_liveness(unit_dir, table)
        if liveness not in {"stopped", "pid_reused", "no_pid"} or desc:
            return False
    return True


def _reconcile_run(
    runs_root: Path,
    run_dir: Path,
    *,
    deadline_monotonic: float | None = None,
    auto_release_dead_held: bool = True,
) -> dict[str, Any]:
    """读时惰性对账 + 可重放结算 + 自动回收已证实死亡子树的冻结额度。"""
    proj = _compute_run_projection(run_dir)
    needs_work = any(
        (not u["virtual"])
        and (
            u["status"] not in TERMINAL_STATUSES
            or not (run_dir / "units" / uid / "result.json").is_file()
            or (auto_release_dead_held and u["held"] > 0)
        )
        for uid, u in proj["units"].items()
    )
    if not needs_work:
        return proj

    table = _runtime._query_process_table()
    now_ep = _now_epoch()

    lock_timeout = 15.0
    if deadline_monotonic is not None:
        lock_timeout = max(0.5, min(15.0, deadline_monotonic - time.monotonic()))

    with _runtime.os_file_lock(run_dir / "run.lock", timeout_sec=lock_timeout):
        proj = _compute_run_projection(run_dir)
        ordered = sorted(
            [uid for uid in proj["units"] if not proj["units"][uid]["virtual"]],
            key=lambda uid: -proj["units"][uid]["depth"],
        )
        for uid in ordered:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
            u = proj["units"][uid]
            unit_dir = run_dir / "units" / uid
            spec = u["spec"]
            result_path = unit_dir / "result.json"

            if u["settled_in_ledger"]:
                if not result_path.is_file():
                    arts = _collect_unit_artifacts(unit_dir, spec)
                    elapsed = max(0.0, _parse_iso_epoch(u["terminal_at"] or _now_iso()) - _parse_iso_epoch(u["created_at"]))
                    _runtime.atomic_write_json(result_path, {
                        "unit_id": uid,
                        "status": u["status"],
                        "exit_code": u["exit_code"],
                        "elapsed_sec": round(elapsed, 2),
                        "artifacts": arts,
                        "warnings": u["warnings"],
                        "error_reason": u["error_reason"],
                        "reconciled_at": _now_iso(),
                    })
                continue

            marker_path = unit_dir / _runtime.START_MARKER_FILE
            marker_exit = _runtime.parse_start_marker_exit(marker_path)
            liveness, _, _ = _unit_process_liveness(unit_dir, table)
            created_ep = _parse_iso_epoch(spec.get("created_at", ""))
            age_sec = max(0.0, now_ep - created_ep) if created_ep > 0 else 0.0
            timeout_sec = int(spec.get("timeout_sec", DEFAULT_UNIT_TIMEOUT_SEC))
            expired = created_ep > 0 and age_sec > timeout_sec

            new_status: str | None = None
            exit_code: int | None = marker_exit
            error_reason: str | None = None
            warnings: list[str] = list(spec.get("spawn_warnings") or [])
            confirmed_dead = False

            if marker_exit is not None:
                if liveness == "running" and not expired:
                    continue
                confirmed_dead = liveness in {"stopped", "pid_reused", "no_pid"}
                new_status = "succeeded" if marker_exit == 0 else "failed"
                if marker_exit != 0:
                    error_reason = f"process_exited_nonzero:{marker_exit}"
            elif expired:
                if liveness == "running":
                    killed = _runtime.kill_process_tree_verified(uid, unit_dir)
                    new_status = "timed_out" if killed else "uncertain"
                    error_reason = f"timeout_exceeded:{timeout_sec}s" if killed else "timeout_kill_unverified"
                    confirmed_dead = killed
                elif liveness in {"stopped", "pid_reused"}:
                    new_status = "timed_out"
                    error_reason = f"timeout_exceeded:{timeout_sec}s"
                    confirmed_dead = True
                else:
                    new_status = "uncertain"
                    error_reason = "timeout_with_unknown_process_state"
                    confirmed_dead = False
            elif liveness in {"stopped", "pid_reused"}:
                # Startup grace window: never misclassify a just-spawned unit before start.marker appears
                if not marker_path.is_file() and age_sec < STARTUP_GRACE_SEC:
                    continue
                new_status = "uncertain"
                error_reason = "process_died_without_exit_marker"
                confirmed_dead = True
            elif not u["spawned"] and age_sec > RESERVED_GRACE_SEC:
                new_status = "uncertain"
                error_reason = "reserved_never_spawned"
                confirmed_dead = (liveness == "no_pid")
            else:
                continue

            child_ids = proj["children_map"].get(uid, [])
            non_terminal_children = [
                cid for cid in child_ids if proj["units"][cid]["status"] not in TERMINAL_STATUSES
            ]
            held_children = [
                cid for cid in child_ids if proj["units"][cid]["held"] > 0 or proj["units"][cid]["status"] == "uncertain"
            ]

            if u["role"] == "orchestrator":
                if non_terminal_children:
                    if new_status == "succeeded":
                        new_status = "failed"
                        error_reason = f"parent_exited_with_active_children:{','.join(non_terminal_children)}"
                    warnings.append("WARN:parent_exited_with_active_children")
                if u["budget_alloc"] > 1 and len(child_ids) == 0 and not spec.get("allow_self_execute"):
                    warnings.append("WARN:zero_children_orchestrator")
                wd_path = Path(spec.get("work_dir", ""))
                if wd_path.is_dir():
                    rc_git, porcelain, _ = _git_run(wd_path, ["status", "--porcelain"])
                    if rc_git == 0 and porcelain:
                        warnings.append("WARN:orchestrator_mutated_repo")

            if new_status == "succeeded":
                for exp in spec.get("expected_outputs") or []:
                    ep = Path(exp)
                    if not ep.is_file() or ep.stat().st_size == 0:
                        new_status = "failed"
                        error_reason = f"expected_output_missing_or_empty:{exp}"
                        break

            arts = _collect_unit_artifacts(unit_dir, spec)
            ts = _now_iso()

            _append_ledger_event(run_dir, {
                "ts": ts,
                "type": "unit_terminal",
                "unit_id": uid,
                "status": new_status,
                "exit_code": exit_code,
                "warnings": warnings,
                "error_reason": error_reason,
            })

            if u["role"] == "orchestrator" and not u["budget_settled"] and u["available"] > 0:
                rem = u["available"]
                can_return = (
                    new_status != "uncertain"
                    and confirmed_dead
                    and not non_terminal_children
                    and not held_children
                )
                if can_return:
                    _append_ledger_event(run_dir, {
                        "ts": ts,
                        "type": "budget_returned",
                        "unit_id": uid,
                        "parent_id": u["parent_id"],
                        "amount": rem,
                    })
                else:
                    _append_ledger_event(run_dir, {
                        "ts": ts,
                        "type": "budget_held",
                        "unit_id": uid,
                        "parent_id": u["parent_id"],
                        "amount": rem,
                        "reason": error_reason or new_status,
                    })

            proj = _compute_run_projection(run_dir)
            if u["role"] == "orchestrator":
                _build_or_merge_orchestrator_summary(run_dir, uid, proj, arts)

            _runtime.atomic_write_json(result_path, {
                "unit_id": uid,
                "status": new_status,
                "exit_code": exit_code,
                "elapsed_sec": round(age_sec, 2),
                "artifacts": arts,
                "warnings": warnings,
                "error_reason": error_reason,
                "reconciled_at": ts,
            })

            if confirmed_dead and new_status != "uncertain":
                _release_worktree_lease_if_owned(runs_root, run_dir.name, uid, spec)

        # Auto-release held budgets when the entire subtree is confirmed dead in process table
        if auto_release_dead_held and table:
            proj = _compute_run_projection(run_dir)
            for uid, u in list(proj["units"].items()):
                if u["virtual"] or u["held"] <= 0:
                    continue
                if _subtree_is_confirmed_dead(run_dir, proj, uid, table):
                    amt = u["held"]
                    _append_ledger_event(run_dir, {
                        "ts": _now_iso(),
                        "type": "budget_released",
                        "unit_id": uid,
                        "parent_id": u["parent_id"],
                        "amount": amt,
                    })
                    _release_worktree_lease_if_owned(runs_root, run_dir.name, uid, u["spec"])

        return _compute_run_projection(run_dir)


def _build_orchestrator_quickref_header(
    *,
    run_id: str,
    unit_id: str,
    run_dir: Path,
    budget_alloc: int,
    remaining_depth: int,
    script_path: Path,
) -> str:
    """精简直观的子编排器速查头（env 已自动注入，无需重复传 --run/--parent/--run-root/--backend）。"""
    sp = _runtime._native_path_str(script_path)
    summary_p = _runtime._native_path_str(run_dir / "units" / unit_id / "summary.json")
    return textwrap.dedent(f"""\
        [OCSR ORCHESTRATOR QUICKREF]
        Unit `{unit_id}` in run `{run_id}` | sub-budget={budget_alloc} | remaining_depth={remaining_depth}
        Your env already sets run/parent/root; backend is auto-detected from model name.
        Commands:
        - Spawn worker/reviewer/orchestrator:
            python -X utf8 "{sp}" spawn <worker|reviewer|orchestrator> -m <model> -p <prompt-or-file> [--budget <k>] [--target <uid>] [--output <path>] [--wait] --json
        - Wait for all active children (re-run if state=="running", max 6x):
            python -X utf8 "{sp}" wait --json
        - View tree or inspect one unit:
            python -X utf8 "{sp}" status [unit_id] --json
        Rules:
        1. Delegate deliverables via `spawn` (do NOT write heavy code/reports yourself).
        2. Wait until all spawned children finish before exiting; on failure inspect evidence before re-spawning.
        3. Before finishing, write `{summary_p}` with `{{"decision": "...", "unresolved": [], "self_executed": false}}`.
        [/OCSR ORCHESTRATOR QUICKREF]

    """)


def _build_backend_command_line(exec_cfg: _backends.ExecutionConfig, unit_dir: Path) -> tuple[str, str | None]:
    adapter = _backends.get_backend_adapter(exec_cfg.backend)
    cwd_literal = _runtime._native_path_str(exec_cfg.work_dir).replace("'", "''")
    capture = exec_cfg.artifact_mode == _backends.ARTIFACT_MODE_CAPTURE_REPLY
    capture_tmp_expr = '"$PSScriptRoot/last-message.tmp"' if capture else ""
    cmd = adapter.build_command_fragment(
        exec_cfg,
        label=unit_dir.name,
        cwd_literal=cwd_literal,
        capture_tmp_expr=capture_tmp_expr,
    )
    return cmd, (str(unit_dir / "last-message.tmp") if capture else None)


def _emit(payload: dict[str, Any], *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text if text is not None else json.dumps(payload, ensure_ascii=False, indent=2))


def _validate_and_acquire_worktree_lease(
    runs_root: Path,
    run_id: str,
    unit_id: str,
    worktree_dir: Path,
    expected_branch: str | None,
    expected_base: str | None,
    allow_dirty: bool,
) -> dict[str, Any]:
    if not worktree_dir.is_dir():
        raise OrgError(f"--worktree does not exist: {worktree_dir}")
    wt_info = _resolve_git_worktree_info(worktree_dir)
    if wt_info is None:
        raise OrgError(f"--worktree is not inside a git worktree: {worktree_dir}")
    if expected_branch and wt_info["branch"] != expected_branch:
        raise OrgError(f"Worktree branch mismatch: expected '{expected_branch}', actual '{wt_info['branch']}'.")
    if expected_base:
        rc, _, err = _git_run(worktree_dir, ["merge-base", "--is-ancestor", expected_base, "HEAD"])
        if rc != 0:
            raise OrgError(f"--base '{expected_base}' is not an ancestor of HEAD ({wt_info['head_sha']}): {err}")
    if not allow_dirty:
        rc_s, porcelain, _ = _git_run(worktree_dir, ["status", "--porcelain"])
        if rc_s == 0 and porcelain:
            raise OrgError(f"Worktree '{worktree_dir}' has uncommitted changes (pass --allow-dirty to override).")

    lease_key = wt_info["toplevel"]
    lease_path = _lease_file_path(runs_root, lease_key)
    table = _runtime._query_process_table()

    with _runtime.os_file_lock(runs_root / "leases.lock"):
        existing = _runtime._read_json_object(lease_path)
        if existing is not None:
            holder_run = str(existing.get("run_id", ""))
            holder_unit = str(existing.get("unit_id", ""))
            holder_dir = runs_root / holder_run / "units" / holder_unit
            can_reclaim = False
            if not holder_dir.is_dir():
                can_reclaim = True
            else:
                holder_res = _runtime._read_json_object(holder_dir / "result.json")
                liveness, _, _ = _unit_process_liveness(holder_dir, table)
                if (
                    holder_res is not None
                    and holder_res.get("status") in TERMINAL_STATUSES
                    and liveness in {"stopped", "pid_reused", "no_pid"}
                ):
                    can_reclaim = True
            if not can_reclaim:
                raise OrgError(
                    f"Worktree write lease conflict on '{wt_info['toplevel_display']}': "
                    f"held by run='{holder_run}' unit='{holder_unit}'.",
                    EXIT_CONTRACT_ERROR,
                )
        _runtime.atomic_write_json(lease_path, {
            "worktree_key": lease_key,
            "worktree_path": wt_info["toplevel_display"],
            "run_id": run_id,
            "unit_id": unit_id,
            "branch": wt_info["branch"],
            "base_sha": expected_base or wt_info["head_sha"],
            "head_sha": wt_info["head_sha"],
            "acquired_at": _now_iso(),
        })
    return {
        "path": str(worktree_dir.resolve()),
        "toplevel": lease_key,
        "toplevel_display": wt_info["toplevel_display"],
        "branch": wt_info["branch"],
        "base_sha": expected_base or wt_info["head_sha"],
        "head_sha": wt_info["head_sha"],
    }


def _wait_loop(
    runs_root: Path,
    run_dir: Path,
    unit_ids: list[str],
    *,
    mode: str,
    timeout_sec: float,
    once: bool,
    poll_interval: float,
) -> dict[str, Any]:
    if timeout_sec <= 0 or timeout_sec > MAX_WAIT_TIMEOUT_SEC:
        raise OrgError(f"--timeout must be in (0, {MAX_WAIT_TIMEOUT_SEC}] seconds.", EXIT_CONTRACT_ERROR)
    poll = max(0.1, poll_interval)
    deadline = time.monotonic() + timeout_sec

    while True:
        proj = _reconcile_run(runs_root, run_dir, deadline_monotonic=deadline)
        missing = [uid for uid in unit_ids if uid not in proj["units"]]
        if missing:
            raise OrgError(f"Unknown unit_id(s): {', '.join(missing)}", EXIT_CONTRACT_ERROR)

        done_units = [uid for uid in unit_ids if proj["units"][uid]["status"] in TERMINAL_STATUSES]
        running_units = [uid for uid in unit_ids if uid not in done_units]
        satisfied = (mode == "any" and len(done_units) >= 1) or (mode == "all" and len(running_units) == 0)

        if satisfied or once or time.monotonic() >= deadline:
            return {
                "run_id": run_dir.name,
                "mode": mode,
                "state": "done" if satisfied else "running",
                "done_units": done_units,
                "running_units": running_units,
                "statuses": {uid: proj["units"][uid]["status"] for uid in unit_ids},
                "budget": proj["budget"],
            }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll, remaining))


# ─── 5 Intuitive Agent Commands ──────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    _check_converge_prohibited(getattr(args, "converge_ledger", None))
    if os.environ.get("OCSR_UNIT_ROLE", "").strip() or os.environ.get("OCSR_UNIT_ID", "").strip():
        raise OrgError("init is prohibited inside a spawned unit.", EXIT_CONTRACT_ERROR)

    goal = args.goal or args.goal_opt
    if not goal:
        raise OrgError("A goal description is required: `init \"your goal\" --budget 6`.")
    max_calls = args.budget if args.budget is not None else args.max_calls
    if max_calls is None or max_calls < 1:
        raise OrgError("--budget (or --max-calls) must be >= 1.")
    if args.depth < 1:
        raise OrgError("--depth must be >= 1.")

    runs_root = _runs_root_from_args(args.run_root)
    repo_root = str(Path(args.repo).resolve()) if args.repo else str(Path.cwd().resolve())

    if args.dry_run:
        _emit({
            "dry_run": True,
            "runs_root": str(runs_root),
            "goal": goal,
            "repo_root": repo_root,
            "max_calls": max_calls,
            "max_depth": args.depth,
            "root_unit_id": VIRTUAL_ROOT_ID,
            "root_turns_counted": False,
        }, as_json=args.json)
        return EXIT_OK

    runs_root.mkdir(parents=True, exist_ok=True)
    with _runtime.os_file_lock(runs_root / "init.lock"):
        init_req_path = runs_root / "init-requests.json"
        init_reqs = _runtime._read_json_object(init_req_path) or {}
        if args.request_id and args.request_id in init_reqs:
            existing_run_id = str(init_reqs[args.request_id]["run_id"])
            cfg = _load_run_config(runs_root / existing_run_id)
            payload = {
                "idempotent_replay": True,
                "run_id": existing_run_id,
                "run_dir": str(runs_root / existing_run_id),
                "root_unit_id": VIRTUAL_ROOT_ID,
                "max_calls": cfg["max_calls"],
                "max_depth": cfg["max_depth"],
            }
            _emit(payload, as_json=args.json, text=f"run_id={existing_run_id} (replayed)")
            return EXIT_OK

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"org-{stamp}-{uuid.uuid4().hex[:6]}"
        run_dir = runs_root / run_id
        (run_dir / "units" / VIRTUAL_ROOT_ID).mkdir(parents=True, exist_ok=True)
        ts = _now_iso()

        _runtime.atomic_write_json(run_dir / "run.json", {
            "schema_version": 1,
            "run_id": run_id,
            "goal": goal,
            "repo_root": repo_root,
            "max_calls": max_calls,
            "max_depth": args.depth,
            "virtual_root_id": VIRTUAL_ROOT_ID,
            "root_turns_counted": False,
            "created_at": ts,
        })
        _runtime.atomic_write_json(run_dir / "run-state.json", {"status": "active", "updated_at": ts})
        _runtime.atomic_write_json(run_dir / "requests.json", {})
        _runtime.atomic_write_json(run_dir / "units" / VIRTUAL_ROOT_ID / "spec.json", {
            "unit_id": VIRTUAL_ROOT_ID,
            "parent_id": None,
            "depth": 0,
            "role": "orchestrator",
            "virtual": True,
            "spawn_allowed": True,
            "write_policy": "artifacts-only",
            "fresh_context": True,
            "budget_alloc": max_calls,
            "created_at": ts,
        })
        _append_ledger_event(run_dir, {
            "ts": ts,
            "type": "run_init",
            "run_id": run_id,
            "max_calls": max_calls,
            "max_depth": args.depth,
        })
        if args.request_id:
            init_reqs[args.request_id] = {"run_id": run_id, "created_at": ts}
            _runtime.atomic_write_json(init_req_path, init_reqs)
        _runtime._write_utf8(runs_root / "latest-run.txt", run_id + "\n")
    _register_org_root(runs_root)

    payload = {
        "idempotent_replay": False,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "root_unit_id": VIRTUAL_ROOT_ID,
        "max_calls": max_calls,
        "max_depth": args.depth,
        "root_turns_counted": False,
    }
    _emit(payload, as_json=args.json, text=f"run_id={run_id} budget={max_calls} depth={args.depth}")
    return EXIT_OK


def cmd_spawn(args: argparse.Namespace) -> int:
    _check_converge_prohibited(getattr(args, "converge_ledger", None))
    if getattr(args, "resume_from", None):
        raise OrgError("Phase 1 ocsr_org does not support --resume-from (fresh-context only).", EXIT_CONTRACT_ERROR)

    runs_root = _runs_root_from_args(args.run_root)
    run_id = _resolve_run_id(runs_root, args.run)
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        raise OrgError(f"Run directory not found: {run_dir}", EXIT_CONTRACT_ERROR)

    env_uid = os.environ.get("OCSR_UNIT_ID", "").strip()
    if env_uid:
        if args.parent and args.parent != env_uid:
            raise OrgError(
                f"Caller identity mismatch: OCSR_UNIT_ID='{env_uid}' != --parent '{args.parent}'.",
                EXIT_CONTRACT_ERROR,
            )
        parent_id = env_uid
    else:
        parent_id = args.parent or VIRTUAL_ROOT_ID

    role = _normalize_role(args.role)
    if role == "orchestrator":
        if args.budget is None or args.budget < 1:
            raise OrgError("role=orchestrator requires --budget K (K >= 1).")
        child_budget = int(args.budget)
    else:
        if args.budget is not None and args.budget != 0:
            raise OrgError(f"role={role} cannot hold sub-budget (--budget must be omitted or 0).")
        child_budget = 0

    # Resolve prompt: -f/--prompt-file (explicit file) OR -p/--prompt (file path if exists, else inline text)
    if args.prompt_file:
        pf = Path(args.prompt_file)
        if not pf.is_file():
            raise OrgError(f"--prompt-file does not exist: {pf}")
        prompt_body = pf.read_text(encoding="utf-8-sig")
    elif args.prompt:
        maybe_path = Path(args.prompt)
        if len(args.prompt) < 260 and "\n" not in args.prompt and maybe_path.is_file():
            prompt_body = maybe_path.read_text(encoding="utf-8-sig")
        else:
            prompt_body = args.prompt
    else:
        raise OrgError("Provide a prompt via -p <text-or-file> or -f <prompt-file>.")

    backend = _detect_backend(args.model, args.backend)
    worktree_dir = Path(args.worktree).resolve() if args.worktree else None
    run_cfg = _load_run_config(run_dir)
    work_dir = worktree_dir or (Path(args.work_dir).resolve() if args.work_dir else Path(run_cfg["repo_root"]).resolve())

    sandbox = args.sandbox
    artifact_mode = args.artifact_mode
    spawn_warnings: list[str] = []
    sandbox_note = ""

    if role == "reviewer":
        if backend == _backends.BACKEND_CODEX:
            if sandbox is not None and sandbox != _backends.SANDBOX_READ_ONLY:
                raise OrgError(f"role=reviewer on codex requires --sandbox read-only (got '{sandbox}').")
            if artifact_mode is not None and artifact_mode != _backends.ARTIFACT_MODE_CAPTURE_REPLY:
                raise OrgError(f"role=reviewer on codex requires --artifact-mode capture-reply (got '{artifact_mode}').")
            sandbox = _backends.SANDBOX_READ_ONLY
            artifact_mode = _backends.ARTIFACT_MODE_CAPTURE_REPLY
            sandbox_note = "codex_mechanical_read_only_capture_reply"
            write_policy = "none"
        else:
            sandbox_note = "opencode_no_process_sandbox_cooperative_only"
            write_policy = "artifacts-only"
    elif role == "worker":
        write_policy = "repo" if worktree_dir is not None else "artifacts-only"
    else:
        write_policy = "artifacts-only"

    try:
        exec_cfg = _backends.validate_execution_config(
            backend=backend,
            model=args.model,
            work_dir=str(work_dir),
            sandbox=sandbox,
            reasoning_effort=args.reasoning_effort,
            artifact_mode=artifact_mode,
            opencode_allowed_models=_load_opencode_allowlist(),
            codex_allowed_models=_load_codex_allowlist(),
            codex_allowlist_path=CODEX_ALLOWLIST_PATH,
        )
    except (ValueError, _backends.BackendConfigError) as exc:
        raise OrgError(str(exc), EXIT_CONTRACT_ERROR) from exc

    family = _infer_family(args.model, exec_cfg.family)

    review_target_snapshot: dict[str, Any] | None = None
    if args.target:
        target_spec = _load_unit_spec(run_dir, args.target)
        target_res = _runtime._read_json_object(run_dir / "units" / args.target / "result.json")
        target_family = str(target_spec.get("family") or "unknown")
        if family == "unknown" or target_family == "unknown":
            spawn_warnings.append("WARN:family_unknown")
        elif family == target_family:
            spawn_warnings.append("WARN:same_family_review")
        review_target_snapshot = {
            "unit_id": args.target,
            "target_model": target_spec.get("model"),
            "target_family": target_family,
            "target_status": (target_res or {}).get("status", "unknown"),
            "target_worktree_ref": target_spec.get("worktree_ref"),
            "target_artifacts": (target_res or {}).get("artifacts", []),
        }
    elif role == "reviewer" and family == "unknown":
        spawn_warnings.append("WARN:family_unknown")

    timeout_sec = int(args.timeout or DEFAULT_UNIT_TIMEOUT_SEC)
    if timeout_sec < 10:
        raise OrgError("--timeout must be >= 10 seconds.")

    with _runtime.os_file_lock(run_dir / "run.lock"):
        run_state = _load_run_state(run_dir)
        if run_state.get("status") != "active":
            raise OrgError(f"Run '{run_id}' is not active (status={run_state.get('status')}).")

        req_path = run_dir / "requests.json"
        requests = _runtime._read_json_object(req_path) or {}
        if args.request_id and args.request_id in requests:
            existing_uid = str(requests[args.request_id]["unit_id"])
            proj = _compute_run_projection(run_dir)
            u = proj["units"].get(existing_uid, {})
            payload = {
                "idempotent_replay": True,
                "run_id": run_id,
                "unit_id": existing_uid,
                "status": u.get("status", "unknown"),
                "unit_dir": str(run_dir / "units" / existing_uid),
            }
            _emit(payload, as_json=args.json, text=f"unit_id={existing_uid} (replayed) status={payload['status']}")
            return EXIT_OK

        proj = _compute_run_projection(run_dir)
        if parent_id not in proj["units"]:
            raise OrgError(f"Parent unit '{parent_id}' does not exist in run '{run_id}'.")
        parent = proj["units"][parent_id]
        if parent["role"] != "orchestrator":
            raise OrgError(f"Leaf unit '{parent_id}' (role={parent['role']}) cannot spawn children.", EXIT_CONTRACT_ERROR)
        if not parent["virtual"] and parent["status"] in TERMINAL_STATUSES:
            raise OrgError(f"Parent unit '{parent_id}' is already terminal (status={parent['status']}).")

        new_depth = parent["depth"] + 1
        max_depth = int(run_cfg["max_depth"])
        if new_depth > max_depth:
            raise OrgError(f"Max depth exceeded: {new_depth} > {max_depth}.", EXIT_CONTRACT_ERROR)

        cost = 1 + child_budget
        if parent["available"] < cost:
            raise OrgError(
                f"Insufficient budget in '{parent_id}': need {cost}, available={parent['available']}.",
                EXIT_CONTRACT_ERROR,
            )

        existing_nums = [int(m.group(1)) for uid in proj["units"] if (m := re.match(r"^u(\d{4})-", uid))]
        next_num = (max(existing_nums) + 1) if existing_nums else 1
        role_tag = {"orchestrator": "orch", "worker": "worker", "reviewer": "rev"}[role]
        unit_id = f"u{next_num:04d}-{role_tag}-{uuid.uuid4().hex[:4]}"
        unit_dir = run_dir / "units" / unit_id

        worktree_ref: dict[str, Any] | None = None
        if worktree_dir is not None:
            worktree_ref = _validate_and_acquire_worktree_lease(
                runs_root, run_id, unit_id, worktree_dir, args.branch, args.base, bool(args.allow_dirty),
            )

        (unit_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        expected_outputs = [str(Path(o).resolve()) for o in (args.output or [])]
        capture_output_path: str | None = None
        if exec_cfg.artifact_mode == _backends.ARTIFACT_MODE_CAPTURE_REPLY:
            capture_output_path = expected_outputs[0] if expected_outputs else str((unit_dir / "artifacts" / "reply.md").resolve())
            if capture_output_path not in expected_outputs:
                expected_outputs.append(capture_output_path)

        ts = _now_iso()
        spec = {
            "unit_id": unit_id,
            "parent_id": parent_id,
            "depth": new_depth,
            "role": role,
            "virtual": False,
            "spawn_allowed": role == "orchestrator",
            "write_policy": write_policy,
            "fresh_context": True,
            "backend": exec_cfg.backend,
            "model": exec_cfg.cli_model,
            "provider": exec_cfg.provider,
            "family": family,
            "work_dir": str(work_dir),
            "worktree_ref": worktree_ref,
            "review_target": review_target_snapshot,
            "sandbox": exec_cfg.sandbox,
            "artifact_mode": exec_cfg.artifact_mode,
            "sandbox_note": sandbox_note,
            "budget_alloc": child_budget,
            "expected_outputs": expected_outputs,
            "timeout_sec": timeout_sec,
            "allow_self_execute": bool(args.allow_self_execute),
            "spawn_warnings": spawn_warnings,
            "created_at": ts,
        }
        _runtime.atomic_write_json(unit_dir / "spec.json", spec)

        final_prompt = prompt_body
        if role == "orchestrator":
            final_prompt = _build_orchestrator_quickref_header(
                run_id=run_id,
                unit_id=unit_id,
                run_dir=run_dir,
                budget_alloc=child_budget,
                remaining_depth=max(0, max_depth - new_depth),
                script_path=Path(__file__).resolve(),
            ) + prompt_body
        _runtime._write_utf8(unit_dir / "prompt.txt", final_prompt)

        cmd_line, _ = _build_backend_command_line(exec_cfg, unit_dir)
        launcher_code = _runtime._pwsh_code(
            "",
            cwd=str(work_dir),
            backend=exec_cfg.backend,
            command_line=cmd_line,
            capture_output_path=capture_output_path,
        )
        _runtime._write_utf8(unit_dir / "launcher.ps1", launcher_code)

        _append_ledger_event(run_dir, {
            "ts": ts,
            "type": "unit_reserved",
            "unit_id": unit_id,
            "parent_id": parent_id,
            "role": role,
            "budget_alloc": child_budget,
        })
        if args.request_id:
            requests[args.request_id] = {"action": "spawn", "unit_id": unit_id, "created_at": ts}
            _runtime.atomic_write_json(req_path, requests)

    child_env = {
        "OCSR_RUN_ID": run_id,
        "OCSR_UNIT_ID": unit_id,
        "OCSR_UNIT_ROLE": role,
        "OCSR_RUN_DIR": str(run_dir),
        "OCSR_ORG_RUNS_ROOT": str(runs_root),
    }
    pid = _runtime._launch_worker(unit_dir, extra_env=child_env)

    with _runtime.os_file_lock(run_dir / "run.lock"):
        _append_ledger_event(run_dir, {
            "ts": _now_iso(),
            "type": "unit_spawned",
            "unit_id": unit_id,
            "pid": pid,
        })

    payload: dict[str, Any] = {
        "idempotent_replay": False,
        "run_id": run_id,
        "unit_id": unit_id,
        "parent_id": parent_id,
        "depth": new_depth,
        "role": role,
        "backend": exec_cfg.backend,
        "model": exec_cfg.cli_model,
        "family": family,
        "pid": pid,
        "budget_alloc": child_budget,
        "unit_dir": str(unit_dir),
        "artifacts_dir": str(unit_dir / "artifacts"),
        "warnings": spawn_warnings,
    }

    if args.wait:
        wait_res = _wait_loop(
            runs_root,
            run_dir,
            [unit_id],
            mode="all",
            timeout_sec=float(args.wait_timeout),
            once=False,
            poll_interval=DEFAULT_POLL_INTERVAL_SEC,
        )
        payload["wait"] = wait_res
        payload["status"] = wait_res["statuses"].get(unit_id, "running")
        res_obj = _runtime._read_json_object(unit_dir / "result.json")
        if res_obj:
            payload["artifacts"] = res_obj.get("artifacts", [])

    warn_str = f" warnings={','.join(spawn_warnings)}" if spawn_warnings else ""
    extra = f" status={payload['status']}" if args.wait else ""
    _emit(payload, as_json=args.json, text=f"spawned {unit_id} [{role}] ({exec_cfg.backend}:{exec_cfg.cli_model}) pid={pid}{extra}{warn_str}")
    return EXIT_OK


def cmd_wait(args: argparse.Namespace) -> int:
    runs_root = _runs_root_from_args(args.run_root)
    run_id = _resolve_run_id(runs_root, args.run)
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        raise OrgError(f"Run directory not found: {run_dir}", EXIT_CONTRACT_ERROR)

    unit_ids: list[str] = []
    for u in args.units_pos or []:
        unit_ids.extend([x.strip() for x in u.split(",") if x.strip()])
    if args.units:
        unit_ids.extend([x.strip() for x in args.units.split(",") if x.strip()])

    if not unit_ids:
        # Default: all currently active direct children of caller
        proj = _reconcile_run(runs_root, run_dir)
        caller = os.environ.get("OCSR_UNIT_ID", "").strip() or VIRTUAL_ROOT_ID
        all_kids = proj["children_map"].get(caller, [])
        active_kids = [c for c in all_kids if proj["units"][c]["status"] not in TERMINAL_STATUSES]
        unit_ids = active_kids or all_kids
        if not unit_ids:
            payload = {
                "run_id": run_id,
                "mode": "any" if args.any else "all",
                "state": "done",
                "done_units": [],
                "running_units": [],
                "statuses": {},
                "budget": proj["budget"],
            }
            _emit(payload, as_json=args.json, text="state=done (no child units)")
            return EXIT_OK

    mode = "any" if args.any else args.mode
    payload = _wait_loop(
        runs_root,
        run_dir,
        unit_ids,
        mode=mode,
        timeout_sec=float(args.timeout),
        once=bool(args.once),
        poll_interval=float(args.poll_interval),
    )
    _emit(payload, as_json=args.json, text=f"state={payload['state']} done={payload['done_units']} running={payload['running_units']}")
    return EXIT_OK


def _read_bounded_tail(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > INSPECT_TAIL_MAX_BYTES:
        data = data[-INSPECT_TAIL_MAX_BYTES:]
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(lines) > INSPECT_TAIL_MAX_LINES:
        lines = lines[-INSPECT_TAIL_MAX_LINES:]
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    runs_root = _runs_root_from_args(args.run_root)
    run_id = _resolve_run_id(runs_root, args.run)
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        raise OrgError(f"Run directory not found: {run_dir}", EXIT_CONTRACT_ERROR)

    proj = _reconcile_run(runs_root, run_dir)

    # Detailed inspection when a specific unit_id is given
    if args.unit_id:
        uid = args.unit_id
        if uid not in proj["units"]:
            raise OrgError(f"Unknown unit_id: {uid}", EXIT_CONTRACT_ERROR)
        unit_dir = run_dir / "units" / uid
        payload = {
            "run_id": run_id,
            "unit_id": uid,
            "status": proj["units"][uid]["status"],
            "spec": _load_unit_spec(run_dir, uid),
            "result": _runtime._read_json_object(unit_dir / "result.json"),
            "summary": _runtime._read_json_object(unit_dir / "summary.json"),
            "stderr_tail": _read_bounded_tail(unit_dir / "stderr.log"),
            "error_tail": _read_bounded_tail(unit_dir / "error.log"),
        }
        _emit(payload, as_json=args.json)
        return EXIT_OK

    # Tree overview when no unit_id is given
    def _build_node(uid: str) -> dict[str, Any]:
        u = proj["units"][uid]
        res = _runtime._read_json_object(run_dir / "units" / uid / "result.json") or {}
        return {
            "unit_id": uid,
            "role": u["role"],
            "virtual": u["virtual"],
            "backend": u["backend"],
            "model": u["model"],
            "family": u["family"],
            "status": u["status"],
            "budget_alloc": u["budget_alloc"],
            "available": u["available"],
            "held": u["held"],
            "elapsed_sec": res.get("elapsed_sec"),
            "artifacts_count": len(res.get("artifacts", [])),
            "warnings": u["warnings"],
            "children": [_build_node(cid) for cid in proj["children_map"].get(uid, [])],
        }

    tree = _build_node(VIRTUAL_ROOT_ID)
    b = proj["budget"]
    payload = {
        "run_id": run_id,
        "goal": proj["run"]["goal"],
        "run_status": proj["run_state"]["status"],
        "budget": b,
        "tree": tree,
    }

    lines = [
        f"Run {run_id} ({proj['run_state']['status']}) | budget: used={b['consumed']}/{b['max_calls']} avail={b['root_available']} reserved={b['reserved']} held={b['held']}"
    ]

    def _render(node: dict[str, Any], prefix: str = "", is_last: bool = True) -> None:
        branch = "" if not prefix else ("└─ " if is_last else "├─ ")
        warn = f" {','.join(node['warnings'])}" if node["warnings"] else ""
        model_info = f"{node['backend']}:{node['model']}" if node["model"] else "root"
        lines.append(
            f"{prefix}{branch}{node['unit_id']} [{node['role']}] {node['status']} ({model_info}) "
            f"avail={node['available']}/{node['budget_alloc']} arts={node['artifacts_count']}{warn}"
        )
        child_prefix = prefix + ("   " if is_last else "│  ") if prefix else "  "
        kids = node["children"]
        for i, kid in enumerate(kids):
            _render(kid, child_prefix, i == len(kids) - 1)

    _render(tree)
    _emit(payload, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def _abort_subtree(runs_root: Path, run_dir: Path, root_uid: str) -> list[dict[str, Any]]:
    proj = _compute_run_projection(run_dir)
    if root_uid not in proj["units"]:
        raise OrgError(f"Unknown unit_id: {root_uid}", EXIT_CONTRACT_ERROR)

    order: list[str] = []

    def _dfs(uid: str) -> None:
        for cid in proj["children_map"].get(uid, []):
            _dfs(cid)
        if not proj["units"][uid]["virtual"]:
            order.append(uid)

    _dfs(root_uid)
    aborted_reports: list[dict[str, Any]] = []
    table = _runtime._query_process_table()

    for uid in order:
        unit_dir = run_dir / "units" / uid
        u = proj["units"][uid]
        if u["status"] in TERMINAL_STATUSES:
            continue
        liveness, _, _ = _unit_process_liveness(unit_dir, table)
        confirmed_dead = False
        if liveness == "running":
            confirmed_dead = _runtime.kill_process_tree_verified(uid, unit_dir)
        elif liveness in {"stopped", "pid_reused", "no_pid"}:
            confirmed_dead = True

        ts = _now_iso()
        new_status = "aborted" if confirmed_dead else "uncertain"
        reason = "stopped_by_parent" if confirmed_dead else "stop_kill_unverified"
        arts = _collect_unit_artifacts(unit_dir, u["spec"])

        with _runtime.os_file_lock(run_dir / "run.lock"):
            proj = _compute_run_projection(run_dir)
            cur = proj["units"][uid]
            if not cur["settled_in_ledger"]:
                _append_ledger_event(run_dir, {"ts": ts, "type": "unit_aborted", "unit_id": uid, "confirmed_dead": confirmed_dead})
                _append_ledger_event(run_dir, {
                    "ts": ts,
                    "type": "unit_terminal",
                    "unit_id": uid,
                    "status": new_status,
                    "exit_code": None,
                    "warnings": [],
                    "error_reason": reason,
                })
                if cur["role"] == "orchestrator" and not cur["budget_settled"] and cur["available"] > 0:
                    rem = cur["available"]
                    has_held_child = any(
                        proj["units"][c]["held"] > 0 or proj["units"][c]["status"] == "uncertain"
                        for c in proj["children_map"].get(uid, [])
                    )
                    if confirmed_dead and not has_held_child:
                        _append_ledger_event(run_dir, {"ts": ts, "type": "budget_returned", "unit_id": uid, "parent_id": cur["parent_id"], "amount": rem})
                    else:
                        _append_ledger_event(run_dir, {"ts": ts, "type": "budget_held", "unit_id": uid, "parent_id": cur["parent_id"], "amount": rem, "reason": reason})
                _runtime.atomic_write_json(unit_dir / "result.json", {
                    "unit_id": uid,
                    "status": new_status,
                    "exit_code": None,
                    "elapsed_sec": 0.0,
                    "artifacts": arts,
                    "warnings": [],
                    "error_reason": reason,
                    "reconciled_at": ts,
                })

        if confirmed_dead:
            _release_worktree_lease_if_owned(runs_root, run_dir.name, uid, u["spec"])
        aborted_reports.append({"unit_id": uid, "status": new_status, "confirmed_dead": confirmed_dead})
    return aborted_reports


def cmd_stop(args: argparse.Namespace) -> int:
    runs_root = _runs_root_from_args(args.run_root)
    run_id = _resolve_run_id(runs_root, args.run)
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        raise OrgError(f"Run directory not found: {run_dir}", EXIT_CONTRACT_ERROR)

    target = args.unit_id or VIRTUAL_ROOT_ID
    reports = _abort_subtree(runs_root, run_dir, target)
    if target == VIRTUAL_ROOT_ID:
        with _runtime.os_file_lock(run_dir / "run.lock"):
            ts = _now_iso()
            _runtime.atomic_write_json(run_dir / "run-state.json", {"status": "aborted", "updated_at": ts, "closed_at": ts})
    proj = _compute_run_projection(run_dir)
    payload = {"run_id": run_id, "stopped": reports, "budget": proj["budget"]}
    _emit(payload, as_json=args.json, text=f"stopped {len(reports)} unit(s)")
    return EXIT_OK


def cmd_close(args: argparse.Namespace) -> int:
    runs_root = _runs_root_from_args(args.run_root)
    run_id = _resolve_run_id(runs_root, args.run)
    run_dir = runs_root / run_id
    if not run_dir.is_dir():
        raise OrgError(f"Run directory not found: {run_dir}", EXIT_CONTRACT_ERROR)

    proj = _reconcile_run(runs_root, run_dir)
    active = [uid for uid, u in proj["units"].items() if not u["virtual"] and u["status"] not in TERMINAL_STATUSES]
    if active:
        raise OrgError(f"Cannot close run: active unit(s) remain: {', '.join(active)}", EXIT_CONTRACT_ERROR)
    if proj["budget"]["held"] > 0:
        raise OrgError(f"Cannot close run: held budget={proj['budget']['held']} remains.", EXIT_CONTRACT_ERROR)

    with _runtime.os_file_lock(run_dir / "run.lock"):
        ts = _now_iso()
        _runtime.atomic_write_json(run_dir / "run-state.json", {"status": "closed", "updated_at": ts, "closed_at": ts})
        _append_ledger_event(run_dir, {"ts": ts, "type": "run_closed", "run_id": run_id})

    payload = {"run_id": run_id, "run_status": "closed", "budget": proj["budget"]}
    _emit(payload, as_json=args.json, text=f"run {run_id} closed")
    return EXIT_OK


# ─── CLI Parser ──────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocsr_org.py",
        description="OCSR Composable CLI Orchestrator — 5 intuitive commands: init, spawn, wait, status, stop.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 1. init
    p_init = sub.add_parser("init", help="Initialize a new task tree")
    p_init.add_argument("goal", nargs="?", default=None, help="Goal description")
    p_init.add_argument("--goal", dest="goal_opt", default=None, help=argparse.SUPPRESS)
    p_init.add_argument("--budget", type=int, default=None, help="Total call budget for this run")
    p_init.add_argument("--max-calls", type=int, default=None, help=argparse.SUPPRESS)
    p_init.add_argument("--depth", "--max-depth", dest="depth", type=int, default=DEFAULT_MAX_DEPTH)
    p_init.add_argument("--repo", default=None)
    p_init.add_argument("--run-root", default=None)
    p_init.add_argument("--request-id", default=None)
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--converge-ledger", default=None, help=argparse.SUPPRESS)
    p_init.add_argument("--json", action="store_true")
    p_init.set_defaults(func=cmd_init)

    # 2. spawn
    p_spawn = sub.add_parser("spawn", help="Spawn an orchestrator, worker, or reviewer unit")
    p_spawn.add_argument("role", choices=sorted(VALID_ROLES), help="orchestrator | worker | executor | reviewer")
    p_spawn.add_argument("-m", "--model", required=True, help="Model slug (auto-detects opencode vs codex)")
    p_spawn.add_argument("-p", "--prompt", default=None, help="Prompt text or path to prompt file")
    p_spawn.add_argument("-f", "--prompt-file", default=None, help="Explicit path to prompt file")
    p_spawn.add_argument("--backend", default=None, choices=sorted(_backends.SUPPORTED_BACKENDS))
    p_spawn.add_argument("--budget", type=int, default=None, help="Sub-budget (required for orchestrator)")
    p_spawn.add_argument("--target", "--review-target", dest="target", default=None, help="Target unit_id for reviewer")
    p_spawn.add_argument("--output", action="append", default=[], help="Expected output path (repeatable)")
    p_spawn.add_argument("--work-dir", default=None)
    p_spawn.add_argument("--worktree", "--worktree-dir", dest="worktree", default=None)
    p_spawn.add_argument("--branch", "--worktree-branch", dest="branch", default=None)
    p_spawn.add_argument("--base", "--worktree-base", dest="base", default=None)
    p_spawn.add_argument("--sandbox", default=None)
    p_spawn.add_argument("--artifact-mode", default=None)
    p_spawn.add_argument("--resume-from", default=None)
    p_spawn.add_argument("--timeout", type=int, default=DEFAULT_UNIT_TIMEOUT_SEC)
    p_spawn.add_argument("--reasoning-effort", default=None)
    p_spawn.add_argument("--wait", action="store_true", help="Spawn and wait (bounded) in one command")
    p_spawn.add_argument("--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SEC)
    p_spawn.add_argument("--request-id", default=None)
    p_spawn.add_argument("--allow-self-execute", action="store_true")
    p_spawn.add_argument("--allow-dirty", action="store_true")
    p_spawn.add_argument("--run", default=None)
    p_spawn.add_argument("--parent", default=None)
    p_spawn.add_argument("--run-root", default=None)
    p_spawn.add_argument("--converge-ledger", default=None, help=argparse.SUPPRESS)
    p_spawn.add_argument("--json", action="store_true")
    p_spawn.set_defaults(func=cmd_spawn)

    # 3. wait
    p_wait = sub.add_parser("wait", help="Wait for specific units or all active children")
    p_wait.add_argument("units_pos", nargs="*", help="Unit IDs (defaults to all active children)")
    p_wait.add_argument("--units", default=None, help=argparse.SUPPRESS)
    p_wait.add_argument("--any", action="store_true", help="Return as soon as any unit finishes")
    p_wait.add_argument("--mode", choices=["any", "all"], default="all", help=argparse.SUPPRESS)
    p_wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SEC)
    p_wait.add_argument("--once", action="store_true")
    p_wait.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    p_wait.add_argument("--run", default=None)
    p_wait.add_argument("--run-root", default=None)
    p_wait.add_argument("--json", action="store_true")
    p_wait.set_defaults(func=cmd_wait)

    # 4. status
    p_status = sub.add_parser("status", help="Show org tree (default) or inspect one unit")
    p_status.add_argument("unit_id", nargs="?", default=None, help="Optional unit_id for detailed inspection")
    p_status.add_argument("--run", default=None)
    p_status.add_argument("--run-root", default=None)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # 5. stop
    p_stop = sub.add_parser("stop", help="Stop one unit subtree or the whole run")
    p_stop.add_argument("unit_id", nargs="?", default=None, help="Optional unit_id (omit to stop whole run)")
    p_stop.add_argument("--run", default=None)
    p_stop.add_argument("--run-root", default=None)
    p_stop.add_argument("--json", action="store_true")
    p_stop.set_defaults(func=cmd_stop)

    # Optional maintenance command: close
    p_close = sub.add_parser("close", help="Mark a completed run closed")
    p_close.add_argument("--run", default=None)
    p_close.add_argument("--run-root", default=None)
    p_close.add_argument("--json", action="store_true")
    p_close.set_defaults(func=cmd_close)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except OrgError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "exit_code": exc.exit_code}, ensure_ascii=False))
        else:
            print(f"[ocsr_org] ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    except _runtime.LockTimeoutError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "exit_code": EXIT_INFRA_ERROR}, ensure_ascii=False))
        else:
            print(f"[ocsr_org] INFRA ERROR: {exc}", file=sys.stderr)
        return EXIT_INFRA_ERROR
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "exit_code": EXIT_INFRA_ERROR}, ensure_ascii=False))
        else:
            print(f"[ocsr_org] INFRA ERROR: {exc}", file=sys.stderr)
        return EXIT_INFRA_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
