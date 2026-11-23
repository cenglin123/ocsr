#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline contract tests for OCSR multi-backend (opencode / codex) support."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ocsr_backends as backends
import ocsr_board as board
import ocsr_dispatch as dispatch
import ocsr_run_spec as run_spec


# ─── 1. Codex allowlist loading (B1 / P2-4) ──────────────────────────

def test_load_codex_allowed_models_missing_and_empty(tmp_path: Path):
    assert backends.load_codex_allowed_models(tmp_path / "missing.json") == {}
    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert backends.load_codex_allowed_models(empty) == {}
    empty.write_text("[]", encoding="utf-8")
    assert backends.load_codex_allowed_models(empty) == {}


def test_load_codex_allowed_models_valid_and_rejects_invalid(tmp_path: Path):
    cfg = tmp_path / "allowed-codex-models.json"
    cfg.write_text(json.dumps([
        {
            "model": "gpt-6.1-sol",
            "provider": "openai",
            "family": "gpt",
            "reasoning_efforts": ["low", "medium", "high"],
        }
    ]), encoding="utf-8")
    loaded = backends.load_codex_allowed_models(cfg)
    assert "gpt-6.1-sol" in loaded
    assert loaded["gpt-6.1-sol"]["provider"] == "openai"
    assert loaded["gpt-6.1-sol"]["family"] == "gpt"
    assert loaded["gpt-6.1-sol"]["reasoning_efforts"] == ("low", "medium", "high")

    # Rejects slash (prefix routing forbidden)
    cfg.write_text(json.dumps([
        {"model": "codex/gpt-6.1-sol", "provider": "openai", "family": "gpt"}
    ]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="without '/'"):
        backends.load_codex_allowed_models(cfg)

    # Rejects duplicate models
    cfg.write_text(json.dumps([
        {"model": "gpt-6.1-sol", "provider": "openai", "family": "gpt"},
        {"model": "gpt-6.1-sol", "provider": "openai", "family": "gpt"},
    ]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicate"):
        backends.load_codex_allowed_models(cfg)

    # Rejects malformed JSON
    cfg.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        backends.load_codex_allowed_models(cfg)


# ─── 2. Unified ExecutionConfig validation (B1 / B3 / B5 / P2-4) ─────

SAMPLE_CODEX_POOL = {
    "gpt-6.1-sol": {
        "model": "gpt-6.1-sol",
        "provider": "openai",
        "family": "gpt",
        "reasoning_efforts": ("low", "medium", "high"),
    }
}
SAMPLE_OPENCODE_POOL = frozenset({"deepseek/deepseek-flash"})


def test_validate_opencode_rejects_codex_only_flags(tmp_path: Path):
    with pytest.raises(ValueError, match="--sandbox"):
        backends.validate_execution_config(
            backend="opencode",
            model="deepseek/deepseek-flash",
            work_dir=str(tmp_path),
            sandbox="read-only",
            opencode_allowed_models=SAMPLE_OPENCODE_POOL,
        )
    with pytest.raises(ValueError, match="--reasoning-effort"):
        backends.validate_execution_config(
            backend="opencode",
            model="deepseek/deepseek-flash",
            work_dir=str(tmp_path),
            reasoning_effort="high",
            opencode_allowed_models=SAMPLE_OPENCODE_POOL,
        )
    with pytest.raises(ValueError, match="--artifact-mode"):
        backends.validate_execution_config(
            backend="opencode",
            model="deepseek/deepseek-flash",
            work_dir=str(tmp_path),
            artifact_mode="capture-reply",
            opencode_allowed_models=SAMPLE_OPENCODE_POOL,
        )


def test_validate_codex_read_only_forces_capture_reply_and_rejects_worker_write(tmp_path: Path):
    cfg = backends.validate_execution_config(
        backend="codex",
        model="gpt-6.1-sol",
        work_dir=str(tmp_path),
        sandbox="read-only",
        codex_allowed_models=SAMPLE_CODEX_POOL,
    )
    assert cfg.backend == "codex"
    assert cfg.provider == "openai"
    assert cfg.family == "gpt"
    assert cfg.sandbox == "read-only"
    assert cfg.artifact_mode == "capture-reply"

    with pytest.raises(ValueError, match="capture-reply"):
        backends.validate_execution_config(
            backend="codex",
            model="gpt-6.1-sol",
            work_dir=str(tmp_path),
            sandbox="read-only",
            artifact_mode="worker-write",
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )


def test_validate_codex_workspace_write_output_boundary_and_reasoning_allowlist(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside.md"
    inside = ws / "report.md"

    # Rejects expected output outside work_dir under worker-write
    with pytest.raises(ValueError, match="reside inside work_dir"):
        backends.validate_execution_config(
            backend="codex",
            model="gpt-6.1-sol",
            work_dir=str(ws),
            sandbox="workspace-write",
            artifact_mode="worker-write",
            expected_output=outside,
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )

    # Allows outside output if capture-reply is explicitly selected
    cfg_cap = backends.validate_execution_config(
        backend="codex",
        model="gpt-6.1-sol",
        work_dir=str(ws),
        sandbox="workspace-write",
        artifact_mode="capture-reply",
        expected_output=outside,
        codex_allowed_models=SAMPLE_CODEX_POOL,
    )
    assert cfg_cap.artifact_mode == "capture-reply"

    # Allows inside output under worker-write + valid reasoning effort
    cfg_ok = backends.validate_execution_config(
        backend="codex",
        model="gpt-6.1-sol",
        work_dir=str(ws),
        sandbox="workspace-write",
        reasoning_effort="medium",
        expected_output=inside,
        codex_allowed_models=SAMPLE_CODEX_POOL,
    )
    assert cfg_ok.reasoning_effort == "medium"

    # Rejects unverified reasoning effort
    with pytest.raises(ValueError, match="Reasoning effort"):
        backends.validate_execution_config(
            backend="codex",
            model="gpt-6.1-sol",
            work_dir=str(ws),
            sandbox="workspace-write",
            reasoning_effort="ultra",
            expected_output=inside,
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )


def test_validate_rejects_unknown_backend_and_prefixed_codex_model(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsupported backend"):
        backends.validate_execution_config(
            backend="unknown-cli",
            model="gpt-6.1-sol",
            work_dir=str(tmp_path),
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )
    with pytest.raises(ValueError, match="without '/'"):
        backends.validate_execution_config(
            backend="codex",
            model="codex/gpt-6.1-sol",
            work_dir=str(tmp_path),
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )


# ─── 3. Event parsing & session binding cross-check (B2) ─────────────

def test_codex_session_binding_five_boundary_cases(tmp_path: Path):
    def _setup_dir(name: str, lines: list[str], binding_sid: str | None = None) -> Path:
        wd = tmp_path / name
        wd.mkdir()
        cfg = backends.ExecutionConfig(
            backend="codex", cli_model="gpt-6.1-sol", provider="openai",
            family="gpt", work_dir=str(tmp_path), sandbox="read-only",
            artifact_mode="capture-reply",
        )
        backends.write_dispatch_meta(wd, cfg)
        (wd / "run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (wd / "pid.txt").write_text("4321", encoding="utf-8")
        (wd / "process-identity.json").write_text(json.dumps({
            "pid": 4321, "creation_time": "100", "command_fingerprint": "fp1"
        }), encoding="utf-8")
        if binding_sid is not None:
            (wd / "session-binding.json").write_text(json.dumps({
                "session_id": binding_sid, "backend": "codex", "cwd": str(tmp_path),
                "pid": 4321, "creation_time": "100", "command_fingerprint": "fp1"
            }), encoding="utf-8")
        return wd

    # 1. No ID
    wd1 = _setup_dir("no_id", ['{"type":"turn.started"}'])
    assert dispatch._read_session_binding(wd1) == ("missing", "")

    # 2. Repeated same ID + damaged line -> still uniquely resolves
    wd2 = _setup_dir("dup_same", [
        '{"type":"thread.started","thread_id":"tid-001"}',
        '{broken json line',
        '{"type":"thread.started","thread_id":"tid-001"}',
    ], binding_sid="tid-001")
    assert dispatch._read_session_binding(wd2) == ("available", "tid-001")

    # 3. Multiple distinct IDs -> ambiguous (fail-closed)
    wd3 = _setup_dir("multi_id", [
        '{"type":"thread.started","thread_id":"tid-001"}',
        '{"type":"thread.started","thread_id":"tid-002"}',
    ], binding_sid="tid-001")
    assert dispatch._read_session_binding(wd3) == ("ambiguous", "")

    # 4. Identity mismatch (binding SID != log SID) -> identity_mismatch
    wd4 = _setup_dir("mismatch", [
        '{"type":"thread.started","thread_id":"tid-001"}',
    ], binding_sid="tid-999")
    assert dispatch._read_session_binding(wd4) == ("identity_mismatch", "")

    # 5. Diagnose uses Codex phase mapping and skips OpenCode SQLite
    diag = dispatch._worker_diagnostics(wd2, session_db=tmp_path / "fake.db")
    assert diag["backend"] == "codex"
    assert diag["last_phase"] == "started"
    assert diag["native_session"]["status"] == "not_applicable_for_codex"


# ─── 4. Shared launcher & capture-reply codegen (B5 / B6) ────────────

def test_codex_launcher_uses_shared_skeleton_and_atomic_capture(tmp_path: Path):
    cfg = backends.ExecutionConfig(
        backend="codex", cli_model="gpt-6.1-sol", provider="openai",
        family="gpt", work_dir=str(tmp_path), sandbox="read-only",
        reasoning_effort="low", artifact_mode="capture-reply",
    )
    adapter = backends.get_backend_adapter("codex")
    cwd_lit = dispatch._native_path_str(tmp_path).replace("'", "''")
    cmd_fresh = adapter.build_command_fragment(
        cfg, label="audit", cwd_literal=cwd_lit,
        capture_tmp_expr='"$PSScriptRoot/last-message.tmp"',
    )
    assert "codex exec" in cmd_fresh
    assert "--sandbox read-only" in cmd_fresh
    assert "--skip-git-repo-check" in cmd_fresh
    assert "--output-last-message" in cmd_fresh
    assert "model_reasoning_effort=\"low\"" in cmd_fresh
    assert "--last" not in cmd_fresh

    ps_code = dispatch._pwsh_code(
        cmd_fresh, cwd=str(tmp_path), backend="codex",
        command_line=cmd_fresh, capture_output_path=str(tmp_path / "out.md"),
    )
    assert "OcsrLauncherJob" in ps_code
    assert "Properties['thread_id']" in ps_code
    assert "last-message.tmp" in ps_code
    assert "Move-Item -LiteralPath $atomicTmp" in ps_code

    cmd_resume = adapter.build_command_fragment(
        cfg, label="audit-r2", cwd_literal=cwd_lit,
        resume_session="01a20983-acbf-7331-981e-56dcfdd26ad1",
        capture_tmp_expr='"$PSScriptRoot/last-message.tmp"',
    )
    assert "--sandbox read-only" in cmd_resume
    assert "resume --json" in cmd_resume
    assert "01a20983-acbf-7331-981e-56dcfdd26ad1" in cmd_resume
    assert "--last" not in cmd_resume


# ─── 5. run --spec schema validation for codex fields (B3) ───────────

def test_run_spec_validates_codex_and_rejects_invalid_combos(tmp_path: Path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("hello", encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"

    valid_codex = {
        "version": 1,
        "run": {"id": "codex-spec", "workdir": str(tmp_path / "run")},
        "steps": [
            {
                "id": "s1",
                "type": "dispatch",
                "backend": "codex",
                "model": "gpt-6.1-sol",
                "sandbox": "read-only",
                "reasoning_effort": "low",
                "artifact_mode": "capture-reply",
                "prompt": str(prompt),
                "output": str(tmp_path / "out.md"),
            }
        ],
    }
    summary = run_spec.validate_spec(
        valid_codex, spec_path=spec_path,
        allowed_models=SAMPLE_OPENCODE_POOL,
        codex_allowed_models=SAMPLE_CODEX_POOL,
    )
    assert summary["step_count"] == 1

    # opencode with sandbox must fail-closed at validation time
    bad_opencode = {
        "version": 1,
        "run": {"id": "bad-oc", "workdir": str(tmp_path / "run2")},
        "steps": [
            {
                "id": "s1",
                "type": "dispatch",
                "backend": "opencode",
                "model": "deepseek/deepseek-flash",
                "sandbox": "read-only",
                "prompt": str(prompt),
                "output": str(tmp_path / "out.md"),
            }
        ],
    }
    with pytest.raises(run_spec.SpecError, match="sandbox"):
        run_spec.validate_spec(
            bad_opencode, spec_path=spec_path,
            allowed_models=SAMPLE_OPENCODE_POOL,
            codex_allowed_models=SAMPLE_CODEX_POOL,
        )


# ─── 6. Board reads dispatch-meta.json & invalidates cache (P2-1) ────

def test_board_reads_dispatch_meta_and_tracks_fingerprint(tmp_path: Path):
    batch = tmp_path / "ocsr_dispatch_20261124_010000_abcdef"
    wd = batch / "R1"
    wd.mkdir(parents=True)
    (wd / "launcher.ps1").write_text("# no -m flag here\n", encoding="utf-8")
    (wd / "worker-state.json").write_text(json.dumps({"state": "landed", "label": "R1"}), encoding="utf-8")
    fp_before = board._worker_fingerprint(wd)

    cfg = backends.ExecutionConfig(
        backend="codex", cli_model="gpt-6.1-sol", provider="openai",
        family="gpt", work_dir=str(tmp_path), sandbox="read-only",
        artifact_mode="capture-reply",
    )
    backends.write_dispatch_meta(wd, cfg)
    fp_after = board._worker_fingerprint(wd)
    assert fp_before != fp_after

    workers = board.scan_workers(tmp_path, days=0)
    assert len(workers) == 1
    assert workers[0]["backend"] == "codex"
    assert workers[0]["model"] == "gpt-6.1-sol"


# ─── 7. Resume policy guard (B4) ─────────────────────────────────────

def test_resume_rejects_backend_or_sandbox_escalation(tmp_path: Path, monkeypatch):
    orig = tmp_path / "orig_wd"
    orig.mkdir()
    prompt = tmp_path / "p.txt"
    prompt.write_text("fix it", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    cfg = backends.ExecutionConfig(
        backend="codex", cli_model="gpt-6.1-sol", provider="openai",
        family="gpt", work_dir=str(tmp_path), sandbox="read-only",
        artifact_mode="capture-reply",
    )
    backends.write_dispatch_meta(orig, cfg)
    (orig / "session-binding.json").write_text(json.dumps({
        "session_id": "01a20983-acbf-7331-981e-56dcfdd26ad1",
        "backend": "codex",
        "cwd": str(tmp_path),
        "pid": 999999,
        "creation_time": "100",
        "command_fingerprint": "fp1",
    }), encoding="utf-8")
    (orig / "process-identity.json").write_text(json.dumps({
        "pid": 999999, "creation_time": "100", "command_fingerprint": "fp1"
    }), encoding="utf-8")
    (orig / "worker-state.json").write_text(json.dumps({
        "state": "landed", "attempt_index": 1, "rework_index": 0
    }), encoding="utf-8")

    monkeypatch.setattr(dispatch, "CODEX_ALLOWED_MODELS", SAMPLE_CODEX_POOL)
    monkeypatch.setattr(dispatch, "ALLOWED_MODELS", SAMPLE_OPENCODE_POOL)
    monkeypatch.delenv("OCSR_DISABLE_MODEL_CALLS", raising=False)

    # Attempting to escalate sandbox from read-only -> workspace-write must fail
    import argparse
    args = argparse.Namespace(
        worker=[f"{prompt}|gpt-6.1-sol|R1"],
        output_dir=str(out_dir),
        output_pattern="{label}.md",
        stagger=0, timeout=1, timeout_policy="auto", watch=False, progress=False,
        harness="cli", meta=[], attempt_index=None, rework_index=None,
        recovery_safe=False, forbid_paths=[], resume_from=str(orig),
        backend="codex", sandbox="workspace-write", reasoning_effort=None,
        artifact_mode="capture-reply", work_dir=str(tmp_path),
        dispatch_root=str(tmp_path), ledger_dir=None, allow_overwrite=[],
        total_timeout=None, renewal_minutes=None, max_renewals=1,
        diagnostic_session_db=None,
    )
    rc = dispatch.cmd_dispatch(args)
    assert rc == 1
