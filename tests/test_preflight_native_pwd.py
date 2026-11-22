"""preflight 的 MSYS PWD/cwd 规范化、dispatch 输出目录自建与失败诊断回归。

全部测试离线：沿用 ``OCSR_DISABLE_MODEL_CALLS`` tripwire 思路，用
``_launch_worker`` / fake runner 替代真实模型调用，不读写 ``~/.ocsr``。
"""
from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ocsr_preflight_native_pwd_test", ROOT / "scripts/ocsr_dispatch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="MSYS /c/... paths only exist on Windows")


# ─── 缺陷 A：MSYS 路径规范化 ─────────────────────────────────────────
@WINDOWS_ONLY
def test_msys_drive_path_is_converted_to_native_form():
    assert mod._native_path_str("/c/foo") == "C:/foo"
    assert mod._native_path_str("/d/a/b c") == "D:/a/b c"
    env = mod._child_env_with_native_pwd("/c/foo")
    assert env is not None
    assert env["PWD"] == "C:/foo"


@WINDOWS_ONLY
def test_preflight_launcher_uses_native_cwd_and_pwd(tmp_path, monkeypatch):
    """$PWD=/c/... 时，preflight 给下一层的 cwd 与 PWD 都是原生路径。"""
    monkeypatch.setattr(mod, "_check_model_calls_disabled", lambda: None)
    monkeypatch.setattr(mod, "_validate_model_allowed", lambda _model: None)
    monkeypatch.setenv("PWD", "/c/definitely-missing-msys-path")
    seen: dict[str, str] = {}

    def launch_fixture(wd):
        seen["launcher"] = (wd / "launcher.ps1").read_text(encoding="utf-8")
        (wd / "start.marker").write_text("exit=0\n", encoding="ascii")
        return 4242

    monkeypatch.setattr(mod, "_launch_worker", launch_fixture)
    rc = mod.cmd_preflight(Namespace(model=["vendor/model-a"], timeout=1,
                                     work_dir=str(tmp_path)))
    assert rc == 0

    launcher = seen["launcher"]
    native = mod._native_path_str(Path.cwd())
    assert f"$env:PWD = '{native}'" in launcher
    assert f"'{native}'" in launcher          # opencode 的位置参数目录
    assert "/c/definitely-missing-msys-path" not in launcher
    assert launcher.index("$env:PWD") < launcher.index("& opencode run")


@WINDOWS_ONLY
def test_launch_worker_passes_native_pwd_in_child_env(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 4321

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod._launch_worker(tmp_path)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PWD"] == mod._native_path_str(tmp_path)


# ─── 缺陷 B：dispatch 自动创建 --output-dir ─────────────────────────
def _dispatch_args(tmp_path: Path, out_dir: Path) -> SimpleNamespace:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("test\n", encoding="utf-8")
    return SimpleNamespace(
        worker=[f"{prompt}|vendor/model-a|w1"],
        output_dir=str(out_dir), output_pattern="{label}.md",
        stagger=0, timeout=15, timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL,
        watch=False, progress=False, work_dir=str(tmp_path / "work"),
        dispatch_root=str(tmp_path / "dispatch-root"),
        harness="test", meta=[], ledger_dir=None, forbid_paths=[],
        attempt_index=None, rework_index=None, recovery_safe=False,
        resume_from="",
    )


def test_dispatch_creates_missing_output_dir(tmp_path, monkeypatch):
    out_dir = tmp_path / "nested" / "out"        # 两级父目录都不存在
    launched: list[Path] = []

    monkeypatch.setattr(mod, "ALLOWED_MODELS", frozenset({"vendor/model-a"}))
    monkeypatch.setattr(mod, "_check_model_calls_disabled", lambda: None)
    monkeypatch.setattr(mod, "_lookup_model_cost",
                        lambda _m: {"input": 0.0, "output": 0.0})
    monkeypatch.setattr(mod, "_query_process_table", lambda: {})
    monkeypatch.setattr(mod, "DISPATCH_LOG", tmp_path / "dispatch-log.jsonl")
    monkeypatch.setattr(mod, "DISPATCH_ROOTS_REGISTRY",
                        tmp_path / "dispatch-roots.json")
    monkeypatch.setattr(mod, "_append_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_launch_worker",
                        lambda wd: launched.append(wd) or 1234)

    assert mod.cmd_dispatch(_dispatch_args(tmp_path, out_dir)) == 0
    assert out_dir.is_dir()
    assert launched, "worker 应在目录创建后继续启动"


def test_dispatch_still_rejects_output_path_that_is_a_file(tmp_path, monkeypatch):
    out_file = tmp_path / "out"
    out_file.write_text("not a directory\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ALLOWED_MODELS", frozenset({"vendor/model-a"}))
    monkeypatch.setattr(mod, "_check_model_calls_disabled", lambda: None)
    monkeypatch.setattr(mod, "_lookup_model_cost",
                        lambda _m: {"input": 0.0, "output": 0.0})
    monkeypatch.setattr(mod, "DISPATCH_ROOTS_REGISTRY",
                        tmp_path / "dispatch-roots.json")

    assert mod.cmd_dispatch(_dispatch_args(tmp_path, out_file)) == 1
    assert out_file.is_file()


# ─── 缺陷 A/C3：preflight 失败诊断 ──────────────────────────────────
def _run_fixture_preflight(tmp_path, monkeypatch, exit_code, stderr_text):
    monkeypatch.setattr(mod, "_check_model_calls_disabled", lambda: None)
    monkeypatch.setattr(mod, "_validate_model_allowed", lambda _model: None)

    def launch_fixture(wd):
        (wd / "stderr.log").write_text(stderr_text, encoding="utf-8")
        (wd / "start.marker").write_text(f"exit={exit_code}\n", encoding="ascii")
        return 4242

    monkeypatch.setattr(mod, "_launch_worker", launch_fixture)
    return mod.cmd_preflight(Namespace(model=["vendor/model-a"], timeout=1,
                                       work_dir=str(tmp_path)))


def test_preflight_env_error_is_not_reported_as_model_unavailable(
        tmp_path, capsys, monkeypatch):
    rc = _run_fixture_preflight(
        tmp_path, monkeypatch, 7,
        "node:internal/process/per_thread:1\nFailed to change directory to /c/foo\n")
    out = capsys.readouterr().out
    assert rc == 1
    assert "env_error:chdir(exit_7)" in out
    assert "exit=7" in out
    assert "Failed to change directory" in out
    assert "不是模型不可用" in out
    assert "error:exit_7" not in out


def test_preflight_nonzero_exit_reports_stderr_tail(tmp_path, capsys, monkeypatch):
    rc = _run_fixture_preflight(
        tmp_path, monkeypatch, 3, "provider exploded\nsecond line\n")
    out = capsys.readouterr().out
    assert rc == 1
    assert "error:exit_3" in out
    assert "exit=3" in out
    assert "provider exploded" in out
    assert "second line" in out


def test_stderr_tail_is_bounded():
    text = "\n".join(f"line-{i}" for i in range(30))
    tail = mod._preflight_stderr_tail(text, max_lines=5, max_chars=40)
    assert len(tail) <= 5
    assert len("\n".join(tail)) <= 40
    assert "line-29" in tail[-1]
