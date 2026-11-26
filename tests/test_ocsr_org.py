#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the converged 5-command ocsr_org.py (init/spawn/wait/status/stop) + real nested Windows Job."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ocsr_board
import ocsr_org
import ocsr_runtime


def _run_cli(argv: list[str], env_overrides: dict[str, str] | None = None) -> tuple[int, dict]:
    buf = io.StringIO()
    old_env = dict(os.environ)
    try:
        for k in ("OCSR_RUN_ID", "OCSR_UNIT_ID", "OCSR_UNIT_ROLE", "OCSR_RUN_DIR", "OCSR_ORG_RUNS_ROOT",
                  "CONVERGE_RUN_ID", "CONVERGE_LEDGER", "CONVERGE_LEDGER_DIR", "OCSR_CONVERGE_LEDGER"):
            os.environ.pop(k, None)
        if env_overrides:
            os.environ.update(env_overrides)
        with redirect_stdout(buf):
            rc = ocsr_org.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    out = buf.getvalue().strip()
    data = json.loads(out) if out.startswith("{") else {"raw": out}
    return rc, data


@pytest.fixture
def org_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    oc_allow = tmp_path / "allowed-models.json"
    oc_allow.write_text(json.dumps([
        "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "alibaba-token-plan-cn/qwen4-max",
        "custom-provider/mystery-model-x",
    ]), encoding="utf-8")
    cx_allow = tmp_path / "allowed-codex-models.json"
    cx_allow.write_text(json.dumps([
        {"model": "gpt-6.2-astra", "provider": "openai", "family": "openai", "reasoning_efforts": ["low", "medium"]},
        {"model": "gpt-6.2-sol", "provider": "openai", "family": "openai", "reasoning_efforts": ["low", "medium"]},
    ]), encoding="utf-8")
    monkeypatch.setattr(ocsr_org, "OPENCODE_ALLOWLIST_PATH", oc_allow)
    monkeypatch.setattr(ocsr_org, "CODEX_ALLOWLIST_PATH", cx_allow)
    monkeypatch.setattr(ocsr_org, "ORG_ROOTS_REGISTRY", tmp_path / "org-roots.json")

    fake_pids = {"next": 900000, "alive": set(), "launched": []}

    def fake_launch(wd: Path, *, extra_env=None, spawn_fn=None) -> int:
        fake_pids["next"] += 1
        pid = fake_pids["next"]
        fake_pids["alive"].add(pid)
        fake_pids["launched"].append({"wd": wd, "pid": pid, "env": dict(extra_env or {})})
        (wd / ocsr_runtime.PID_FILE_NAME).write_text(f"{pid}\n", encoding="ascii")
        ocsr_runtime.atomic_write_json(wd / ocsr_runtime.PROCESS_IDENTITY_FILE, {
            "pid": pid,
            "creation_time": f"ticks-{pid}",
            "command_fingerprint": "fake-fp",
        })
        (wd / ocsr_runtime.START_MARKER_FILE).write_text("pwsh started\n", encoding="utf-8")
        return pid

    def fake_table() -> dict[int, dict]:
        tbl = {os.getpid(): {"parent_pid": 0, "creation_time": "self", "command": "pytest"}}
        for pid in list(fake_pids["alive"]):
            tbl[pid] = {"parent_pid": 1, "creation_time": f"ticks-{pid}", "command": "fake-launcher"}
        return tbl

    def fake_kill(label: str, wd: Path, **kwargs) -> bool:
        pid = ocsr_runtime._read_pid(wd)
        if pid in fake_pids["alive"]:
            fake_pids["alive"].discard(pid)
            return True
        return False

    monkeypatch.setattr(ocsr_runtime, "_launch_worker", fake_launch)
    monkeypatch.setattr(ocsr_runtime, "_query_process_table", fake_table)
    monkeypatch.setattr(ocsr_runtime, "kill_process_tree_verified", fake_kill)

    return {"tmp": tmp_path, "runs_root": runs_root, "fake": fake_pids}


def _finish_fake_unit(org_env, unit_dir: Path, exit_code: int = 0, artifact_text: str | None = "ok\n"):
    pid = ocsr_runtime._read_pid(unit_dir)
    org_env["fake"]["alive"].discard(pid)
    if artifact_text is not None:
        (unit_dir / "artifacts" / "out.txt").write_text(artifact_text, encoding="utf-8")
    with (unit_dir / ocsr_runtime.START_MARKER_FILE).open("a", encoding="utf-8") as fh:
        fh.write(f"exit={exit_code}\n")


def test_simple_5_command_workflow_and_budget_refund(org_env):
    rr = str(org_env["runs_root"])
    # 1. init with minimal args
    rc, init = _run_cli(["init", "Build feature X", "--budget", "5", "--depth", "2", "--run-root", rr, "--json"])
    assert rc == 0
    run_id = init["run_id"]

    # 2. spawn sub-orchestrator: NO --run (auto latest), NO --backend (auto-detected codex!), inline -p
    rc, orch = _run_cli([
        "spawn", "orchestrator", "-m", "gpt-6.2-astra", "--budget", "3",
        "-p", "Coordinate feature X", "--run-root", rr, "--json",
    ])
    assert rc == 0
    assert orch["backend"] == "codex"
    orch_id = orch["unit_id"]

    # Over-budget spawn from root (cost=2 > avail=1) rejected exit 2
    rc, _ = _run_cli([
        "spawn", "orchestrator", "-m", "gpt-6.2-astra", "--budget", "1",
        "-p", "Too expensive", "--run-root", rr, "--json",
    ])
    assert rc == 2

    # 3. Sub-orchestrator spawns worker with ONLY env vars (NO --run, NO --parent, NO --run-root, NO --backend!)
    sub_env = {
        "OCSR_RUN_ID": run_id,
        "OCSR_UNIT_ID": orch_id,
        "OCSR_UNIT_ROLE": "orchestrator",
        "OCSR_ORG_RUNS_ROOT": rr,
    }
    rc, worker = _run_cli([
        "spawn", "worker", "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "-p", "Write module", "--json",
    ], env_overrides=sub_env)
    assert rc == 0
    assert worker["backend"] == "opencode"
    w_id = worker["unit_id"]

    # 4. wait with NO unit_ids defaults to waiting on all active children of caller!
    rc, w_running = _run_cli(["wait", "--once", "--json"], env_overrides=sub_env)
    assert rc == 0
    assert w_running["state"] == "running"
    assert w_id in w_running["running_units"]

    # Leaf worker cannot spawn
    rc, _ = _run_cli([
        "spawn", "worker", "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "-p", "illegal", "--json",
    ], env_overrides={"OCSR_RUN_ID": run_id, "OCSR_UNIT_ID": w_id, "OCSR_UNIT_ROLE": "worker", "OCSR_ORG_RUNS_ROOT": rr})
    assert rc == 2

    # Finish worker & sub-orchestrator -> unused 2 budget auto-refunds to root
    _finish_fake_unit(org_env, Path(worker["unit_dir"]), exit_code=0)
    _finish_fake_unit(org_env, Path(orch["unit_dir"]), exit_code=0)

    # 5. status (tree view) and status <uid> (detailed inspect)
    rc, tree = _run_cli(["status", "--run-root", rr, "--json"])
    assert rc == 0
    assert tree["budget"]["consumed"] == 2
    assert tree["budget"]["root_available"] == 3
    assert tree["budget"]["invariant_ok"] is True

    rc, detail = _run_cli(["status", orch_id, "--run-root", rr, "--json"])
    assert rc == 0
    assert detail["summary"]["children"][0]["unit_id"] == w_id

    rc, closed = _run_cli(["close", "--run-root", rr, "--json"])
    assert rc == 0
    assert closed["run_status"] == "closed"


def test_caller_identity_spoof_and_nested_init_blocked(org_env):
    rr = str(org_env["runs_root"])
    rc, init = _run_cli(["init", "id-test", "--budget", "4", "--run-root", rr, "--json"])
    run_id = init["run_id"]
    rc, w = _run_cli([
        "spawn", "worker", "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "-p", "task", "--run-root", rr, "--json",
    ])
    w_id = w["unit_id"]

    # Spoofing --parent u0000-root from inside worker must fail-closed exit 2
    rc, _ = _run_cli([
        "spawn", "worker", "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "--parent", "u0000-root", "-p", "spoof", "--json",
    ], env_overrides={"OCSR_RUN_ID": run_id, "OCSR_UNIT_ID": w_id, "OCSR_UNIT_ROLE": "worker", "OCSR_ORG_RUNS_ROOT": rr})
    assert rc == 2

    # Nested init inside worker must fail-closed exit 2
    rc, _ = _run_cli(
        ["init", "bypass", "--budget", "99", "--run-root", rr, "--json"],
        env_overrides={"OCSR_UNIT_ROLE": "worker", "OCSR_UNIT_ID": w_id},
    )
    assert rc == 2


def test_idempotency_and_startup_grace_and_auto_release(org_env):
    rr = str(org_env["runs_root"])
    rc1, i1 = _run_cli(["init", "idem", "--budget", "5", "--request-id", "req-i1", "--run-root", rr, "--json"])
    rc2, i2 = _run_cli(["init", "idem", "--budget", "5", "--request-id", "req-i1", "--run-root", rr, "--json"])
    assert rc1 == 0 and rc2 == 0 and i1["run_id"] == i2["run_id"] and i2["idempotent_replay"] is True

    rc3, s1 = _run_cli([
        "spawn", "orchestrator", "-m", "gpt-6.2-astra", "--budget", "3",
        "-p", "plan", "--request-id", "req-s1", "--run-root", rr, "--json",
    ])
    rc4, s2 = _run_cli([
        "spawn", "orchestrator", "-m", "gpt-6.2-astra", "--budget", "3",
        "-p", "plan", "--request-id", "req-s1", "--run-root", rr, "--json",
    ])
    assert rc3 == 0 and rc4 == 0 and s1["unit_id"] == s2["unit_id"] and len(org_env["fake"]["launched"]) == 1

    # Startup grace: if process is not yet seen AND start.marker doesn't exist within STARTUP_GRACE_SEC,
    # unit stays running (NOT prematurely marked uncertain!)
    orch_dir = Path(s1["unit_dir"])
    pid = ocsr_runtime._read_pid(orch_dir)
    org_env["fake"]["alive"].discard(pid)
    (orch_dir / ocsr_runtime.START_MARKER_FILE).unlink()
    rc, st_grace = _run_cli(["wait", s1["unit_id"], "--once", "--run-root", rr, "--json"])
    assert rc == 0
    assert st_grace["state"] == "running"

    # Once start.marker exists (process actually started and vanished without exit=),
    # reconcile marks uncertain AND auto-releases held budget since process is confirmed dead!
    (orch_dir / ocsr_runtime.START_MARKER_FILE).write_text("pwsh started\n", encoding="utf-8")
    rc, tree = _run_cli(["status", "--run-root", rr, "--json"])
    assert rc == 0
    assert tree["budget"]["held"] == 0
    assert tree["budget"]["root_available"] == 4
    assert tree["budget"]["invariant_ok"] is True


def test_cross_run_worktree_lease_conflict_and_stop(org_env):
    rr = str(org_env["runs_root"])
    repo = org_env["tmp"] / "sample-repo"
    sub_dir = repo / "nested" / "sub"
    sub_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    _, r1 = _run_cli(["init", "r1", "--budget", "2", "--run-root", rr, "--json"])
    _, r2 = _run_cli(["init", "r2", "--budget", "2", "--run-root", rr, "--json"])

    rc, w1 = _run_cli([
        "spawn", "worker", "--run", r1["run_id"], "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "--worktree", str(repo), "-p", "edit", "--run-root", rr, "--json",
    ])
    assert rc == 0

    # Different run targeting subdirectory of SAME git worktree -> lease conflict exit 2
    rc, err = _run_cli([
        "spawn", "worker", "--run", r2["run_id"], "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "--worktree", str(sub_dir), "-p", "edit2", "--run-root", rr, "--json",
    ])
    assert rc == 2 and "lease conflict" in err["error"].lower()

    # Stop w1 -> lease released -> r2 succeeds
    rc, _ = _run_cli(["stop", w1["unit_id"], "--run", r1["run_id"], "--run-root", rr, "--json"])
    assert rc == 0

    rc, w2 = _run_cli([
        "spawn", "worker", "--run", r2["run_id"], "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "--worktree", str(sub_dir), "-p", "edit2", "--run-root", rr, "--json",
    ])
    assert rc == 0


def test_reviewer_guards_and_converge_ban(org_env):
    rr = str(org_env["runs_root"])
    rc, _ = _run_cli(["init", "c", "--budget", "2", "--run-root", rr, "--json"], env_overrides={"CONVERGE_RUN_ID": "c1"})
    assert rc == 2

    _, init = _run_cli(["init", "rev-test", "--budget", "5", "--run-root", rr, "--json"])
    _, w = _run_cli(["spawn", "worker", "-m", "gpt-6.2-sol", "-p", "work", "--run-root", rr, "--json"])
    w_id = w["unit_id"]

    # Reviewer rejects --resume-from and writable codex sandbox
    rc, _ = _run_cli(["spawn", "reviewer", "-m", "gpt-6.2-astra", "--resume-from", w_id, "-p", "rev", "--run-root", rr, "--json"])
    assert rc == 2
    rc, _ = _run_cli(["spawn", "reviewer", "-m", "gpt-6.2-astra", "--sandbox", "workspace-write", "-p", "rev", "--run-root", rr, "--json"])
    assert rc == 2

    # Codex reviewer auto-enforces read-only + capture-reply and warns same-family
    rc, rev = _run_cli(["spawn", "reviewer", "-m", "gpt-6.2-astra", "--target", w_id, "-p", "rev", "--run-root", rr, "--json"])
    assert rc == 0
    assert "WARN:same_family_review" in rev["warnings"]
    spec = json.loads((Path(rev["unit_dir"]) / "spec.json").read_text(encoding="utf-8"))
    assert spec["sandbox"] == "read-only" and spec["artifact_mode"] == "capture-reply"

    # Unknown family warns WARN:family_unknown
    rc, rev2 = _run_cli(["spawn", "reviewer", "-m", "custom-provider/mystery-model-x", "--target", w_id, "-p", "rev", "--run-root", rr, "--json"])
    assert rc == 0 and "WARN:family_unknown" in rev2["warnings"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows KILL_ON_JOB_CLOSE nested launcher test")
def test_real_windows_nested_job_lifecycle_and_abort(tmp_path: Path):
    if os.getpid() not in ocsr_runtime._query_process_table():
        pytest.skip("Windows process table unavailable; cannot verify launcher identity")
    parent_dir = tmp_path / "parent-unit"
    parent_dir.mkdir()
    grandchild_pid_file = tmp_path / "grandchild.pid"
    (parent_dir / "prompt.txt").write_text("noop", encoding="utf-8")

    gc_path = str(grandchild_pid_file).replace("'", "''")
    child_script = parent_dir / "child1.ps1"
    child_script.write_text(
        f"$gc = Start-Process -FilePath pwsh -ArgumentList @('-NoProfile','-Command','Start-Sleep -Seconds 60') "
        f"-WindowStyle Hidden -PassThru\n"
        f"Set-Content -LiteralPath '{gc_path}' -Value $gc.Id -Encoding Ascii\n"
        f"Start-Sleep -Milliseconds 300\n"
        f"exit 0\n",
        encoding="utf-8",
    )
    cmd_exit_early = f"& pwsh -NoProfile -ExecutionPolicy Bypass -File '{str(child_script).replace(chr(39), chr(39)*2)}'"
    (parent_dir / "launcher.ps1").write_text(ocsr_runtime._pwsh_code("", cwd=str(tmp_path), command_line=cmd_exit_early), encoding="utf-8")
    parent_pid = ocsr_runtime._launch_worker(parent_dir, extra_env={"OCSR_DISABLE_MODEL_CALLS": "0"})

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ocsr_runtime.parse_start_marker_exit(parent_dir / "start.marker") is not None:
            break
        time.sleep(0.25)
    assert ocsr_runtime.parse_start_marker_exit(parent_dir / "start.marker") == 0
    gc_pid = int(grandchild_pid_file.read_text(encoding="ascii").strip())
    time.sleep(0.8)
    tbl = ocsr_runtime._query_process_table()
    assert os.getpid() in tbl
    assert parent_pid not in tbl and gc_pid not in tbl

    parent2_dir = tmp_path / "parent-abort"
    parent2_dir.mkdir()
    gc2_pid_file = tmp_path / "grandchild2.pid"
    (parent2_dir / "prompt.txt").write_text("noop", encoding="utf-8")
    gc2_path = str(gc2_pid_file).replace("'", "''")
    child2_script = parent2_dir / "child2.ps1"
    child2_script.write_text(
        f"$gc = Start-Process -FilePath pwsh -ArgumentList @('-NoProfile','-Command','Start-Sleep -Seconds 60') "
        f"-WindowStyle Hidden -PassThru\n"
        f"Set-Content -LiteralPath '{gc2_path}' -Value $gc.Id -Encoding Ascii\n"
        f"Start-Sleep -Seconds 60\n",
        encoding="utf-8",
    )
    cmd_stay_alive = f"& pwsh -NoProfile -ExecutionPolicy Bypass -File '{str(child2_script).replace(chr(39), chr(39)*2)}'"
    (parent2_dir / "launcher.ps1").write_text(ocsr_runtime._pwsh_code("", cwd=str(tmp_path), command_line=cmd_stay_alive), encoding="utf-8")
    p2_pid = ocsr_runtime._launch_worker(parent2_dir, extra_env={"OCSR_DISABLE_MODEL_CALLS": "0"})

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if gc2_pid_file.is_file() and (parent2_dir / ocsr_runtime.PROCESS_IDENTITY_FILE).is_file():
            break
        time.sleep(0.25)
    gc2_pid = int(gc2_pid_file.read_text(encoding="ascii").strip())
    assert ocsr_runtime.kill_process_tree_verified("parent-abort", parent2_dir) is True
    tbl2 = ocsr_runtime._query_process_table()
    assert os.getpid() in tbl2
    assert p2_pid not in tbl2 and gc2_pid not in tbl2


def test_board_readonly_org_tree_scan(org_env):
    """ocsr_board.scan_org_runs reads org trees accurately without modifying any run file."""
    rr = org_env["runs_root"]
    rc, init = _run_cli(["init", "Board visibility test", "--budget", "4", "--run-root", str(rr), "--json"])
    assert rc == 0
    run_id = init["run_id"]
    rc, orch = _run_cli([
        "spawn", "orchestrator", "-m", "gpt-6.2-astra", "--budget", "2",
        "-p", "plan", "--run-root", str(rr), "--json",
    ])
    assert rc == 0
    rc, worker = _run_cli([
        "spawn", "worker", "-m", "alibaba-token-plan-cn/deepseek-v4.1-flash-1017",
        "-p", "build", "--json",
    ], env_overrides={
        "OCSR_RUN_ID": run_id,
        "OCSR_UNIT_ID": orch["unit_id"],
        "OCSR_UNIT_ROLE": "orchestrator",
        "OCSR_ORG_RUNS_ROOT": str(rr),
    })
    assert rc == 0
    _finish_fake_unit(org_env, Path(worker["unit_dir"]), exit_code=0)
    _finish_fake_unit(org_env, Path(orch["unit_dir"]), exit_code=0)
    _run_cli(["status", "--run-root", str(rr), "--json"])

    # Snapshot every file mtime/size under run_dir before board scan
    run_dir = rr / run_id
    before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in run_dir.rglob("*") if p.is_file()}

    scanned = ocsr_board.scan_org_runs([rr], days=7)
    assert len(scanned) == 1
    item = scanned[0]
    assert item["run_id"] == run_id
    assert item["goal"] == "Board visibility test"
    assert item["budget"]["consumed"] == 2
    assert item["budget"]["root_available"] == 2
    assert item["tree"]["unit_id"] == "u0000-root"
    assert len(item["tree"]["children"]) == 1
    assert item["tree"]["children"][0]["children"][0]["unit_id"] == worker["unit_id"]

    # Strictly read-only: zero files added, removed, or modified
    after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in run_dir.rglob("*") if p.is_file()}
    assert before == after


@pytest.fixture
def board_unit(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    run_dir = root / "org-fixture"
    unit_dir = run_dir / "units" / "u0000-root"
    unit_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_dir.name, "goal": "fixture", "max_calls": 3,
        "created_at": datetime.now().isoformat(),  # Legacy naive timestamp.
    }), encoding="utf-8")
    (unit_dir / "spec.json").write_text(json.dumps({
        "unit_id": unit_dir.name, "virtual": True, "role": "orchestrator",
    }), encoding="utf-8")
    monkeypatch.setattr(ocsr_board, "DEFAULT_ORG_ROOT", root)
    monkeypatch.setattr(ocsr_board, "ORG_ROOTS_REGISTRY", tmp_path / "org-roots.json")
    return run_dir, unit_dir


def _board_get(path, roots):
    handler = object.__new__(ocsr_board.BoardHandler)
    handler.path = path
    handler.org_roots = roots
    handler._origin_ok = lambda: True
    response = []
    handler._send = lambda code, body, _: response.append((code, json.loads(body)))
    handler.do_GET()
    (result,) = response
    return result


def _org_unit_get(run_id="org-fixture", unit_id="u0000-root"):
    return _board_get("/api/org-unit?" + urlencode({"run_id": run_id, "unit_id": unit_id}), [])


def test_board_org_unit_reads_bounded_utf8(board_unit):
    run_dir, unit_dir = board_unit
    (unit_dir / "result.json").write_text('{"elapsed_sec": 10}', encoding="utf-8")
    (unit_dir / "summary.json").write_text('{"decision": "done"}', encoding="utf-8")
    (unit_dir / "prompt.txt").write_text("任" * 2000, encoding="utf-8")
    (unit_dir / "stderr.log").write_text("\n".join(map(str, range(80))) + "\n", encoding="utf-8")
    (unit_dir / "error.log").write_text("错" * 2000 + "末尾", encoding="utf-8")
    code, data = _org_unit_get()
    assert code == 200
    assert data == {
        "run_id": run_dir.name, "unit_id": unit_dir.name,
        "spec": {"unit_id": unit_dir.name, "virtual": True, "role": "orchestrator"},
        "result": {"elapsed_sec": 10}, "summary": {"decision": "done"},
        "prompt_preview": "任" * 1365,
        "stderr_tail": "\n".join(map(str, range(20, 80))),
        "error_tail": "错" * 1363 + "末尾",
    }
    assert all(len(data[key].encode("utf-8")) <= 4096 for key in (
        "prompt_preview", "stderr_tail", "error_tail",
    ))


@pytest.mark.parametrize("query", [
    "run_id=org-x%2F..%2Forg-fixture&unit_id=u0000-root",
    "run_id=org-fixture&unit_id=u0000-root%5c..%5cu0001-worker",
    "run_id=org-fixture%0A&unit_id=u0000-root",
    "run_id=org-fixture&run_id=org-other&unit_id=u0000-root",
    "run_id=org-fixture&unit_id=",
])
def test_board_org_unit_rejects_invalid_ids(board_unit, query):
    assert _board_get("/api/org-unit?" + query, [])[0] == 400


def test_board_org_unit_missing_or_undiscovered(board_unit):
    run_dir, unit_dir = board_unit
    assert _org_unit_get("org-missing")[0] == 404
    assert _org_unit_get(unit_id="u0999-missing")[0] == 404
    (run_dir / "run.json").unlink()
    assert _org_unit_get()[0] == 404


def test_board_org_unit_rejects_linked_evidence(board_unit, tmp_path):
    run_dir, unit_dir = board_unit
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    prompt = unit_dir / "prompt.txt"
    try:
        prompt.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert _org_unit_get()[1]["prompt_preview"] == ""
    prompt.unlink()
    detached = unit_dir.rename(tmp_path / "detached")
    unit_dir.symlink_to(detached, target_is_directory=True)
    assert _org_unit_get()[0] == 404
    moved_run = run_dir.rename(tmp_path / run_dir.name)
    run_dir.symlink_to(moved_run, target_is_directory=True)
    assert _org_unit_get()[0] == 404


def test_board_org_unit_is_strictly_readonly(board_unit, monkeypatch):
    run_dir, unit_dir = board_unit
    (unit_dir / "result.json").write_text("{", encoding="utf-8")
    before = {p: (p.stat().st_mtime_ns, p.read_bytes())
              for p in run_dir.rglob("*") if p.is_file()}
    original_open = io.open
    original_os_open = os.open

    def guarded_open(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+"), (file, mode)
        return original_open(file, mode, *args, **kwargs)

    def guarded_os_open(file, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_os_open(file, flags, *args, **kwargs)

    def forbidden_lock(*args, **kwargs):
        pytest.fail("org-unit attempted to take a file lock")

    with monkeypatch.context() as patch:
        patch.setattr(io, "open", guarded_open)
        patch.setattr("builtins.open", guarded_open)
        patch.setattr(os, "open", guarded_os_open)
        patch.setattr(ocsr_runtime, "os_file_lock", forbidden_lock)
        code, data = _org_unit_get()
        assert code == 200
        assert data["result"] is None and data["summary"] is None
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes())
                      for p in run_dir.rglob("*") if p.is_file()}


def test_board_state_org_scan_tolerates_damaged_evidence(board_unit, tmp_path, monkeypatch):
    run_dir, unit_dir = board_unit
    root = run_dir.parent
    old = root / "org-old"
    old_unit = old / "units" / unit_dir.name
    old_unit.mkdir(parents=True)
    (old_unit / "spec.json").write_bytes((unit_dir / "spec.json").read_bytes())
    (old / "run.json").write_text(json.dumps({
        "run_id": old.name, "created_at": (datetime.now() - timedelta(days=20)).isoformat(),
    }), encoding="utf-8")
    (run_dir / "run-state.json").write_text("{broken", encoding="utf-8")
    (run_dir / "ledger.jsonl").write_bytes(
        b'garbage\n{"type":"unit_reserved","unit_id":[]}\n'
        b'{"type":"budget_held","unit_id":"u0000-root","amount":{}}\n'
        b'{"type":"unit_reserved","unit_id":"u0000-root"}'
    )
    (unit_dir / "result.json").write_text(json.dumps({
        "artifacts": [None, {"path": "a", "size": 2, "sha256": "abc"}],
        "warnings": ["warn", {}],
    }), encoding="utf-8")

    monkeypatch.setattr(ocsr_board.BoardHandler, "base_root", tmp_path)
    monkeypatch.setattr(ocsr_board.BoardHandler, "groups_path", tmp_path / "groups.json")
    monkeypatch.setattr(ocsr_board.BoardHandler, "log_path", tmp_path / "dispatch-log.jsonl")
    monkeypatch.setattr(ocsr_board, "DISPATCH_ROOTS_REGISTRY", tmp_path / "dispatch-roots.json")
    monkeypatch.setattr(ocsr_board, "_STATE_CACHE", dict(ocsr_board._STATE_CACHE))
    monkeypatch.setattr(ocsr_board, "_WORKER_CACHE", {})
    assert ocsr_board.effective_org_roots([root / ".." / root.name]) == [root]
    assert len(ocsr_board.scan_org_runs([root], days=0)) == 2
    code, state = _board_get("/api/state?fresh=1", [root])
    assert code == 200
    (item,) = state["org_runs"]
    assert set(item) == {"run_id", "goal", "run_status", "created_at", "run_dir", "budget", "tree"}
    assert item["run_id"] == run_dir.name and item["run_status"] == "active"
    assert item["budget"] == {
        "max_calls": 3, "consumed": 0, "reserved": 0, "held": 0, "root_available": 3,
    }
    tree = item["tree"]
    assert set(tree) == {
        "unit_id", "role", "virtual", "backend", "model", "family", "status",
        "budget_alloc", "available", "held", "elapsed_sec", "task_summary",
        "decision", "artifacts", "warnings", "error_reason", "children",
    }
    assert tree["artifacts"] == [{"path": "a", "size": 2, "sha256": "abc"}]
    assert tree["warnings"] == ["warn"]

