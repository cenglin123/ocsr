"""执行层验收语义回归测试（plan 20260809-execution-layer-integrity Phase 1）。

覆盖三条 P0：
  A1 `_watch_loop` 失败结案与退出码契约（0/1/2，混合结局优先级 1>2>0）
  A2 DB 锁恢复必须交回上层，watcher 不得自行重派或暗耗尝试次数
  A3 看门狗按 PID 终止且校验 taskkill 退出码（含一条真起进程的离线集成测试）

全部离线，不触发任何模型调用。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

import pytest

# Fail-safe: 防止任何意外的模型调用
os.environ["OCSR_DISABLE_MODEL_CALLS"] = "1"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_eli", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def _worker(td: Path, name: str, *, marker: str | None = None, log: str = "",
            error: str | None = None, artifact: str | None = None,
            launcher: bool = False) -> dict:
    wd = td / f"wd-{name}"
    wd.mkdir(parents=True, exist_ok=True)
    if artifact is not None and marker is None:
        marker = "exit=0\n"
    if marker is not None:
        (wd / "start.marker").write_text(marker, encoding="utf-8")
    if log:
        (wd / "run.log").write_text(log, encoding="utf-8")
    if error is not None:
        (wd / "error.log").write_text(error, encoding="utf-8")
    if launcher:
        (wd / "launcher.ps1").write_text("noop", encoding="utf-8")
    out = td / f"{name}.md"
    if artifact is not None:
        out.write_text(artifact, encoding="utf-8")
    return {"output": out, "label": name, "model": "xiaomi/mimo-v2.5",
            "prompt_size_bytes": 10, "work_dir": wd}


class _FakeClock:
    """可控时钟：`sleep` 只推进虚拟时间，不耗墙钟。

    必须用假时钟而非单纯 mock 掉 `sleep`——`_watch_loop` 的 deadline 判定读的是
    `time.time()`，只 mock `sleep` 会让它对着真实时钟忙等。假时钟让 deadline
    逻辑仍被真实覆盖。
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(float(seconds), 1.0)


def _run_watch(td: Path, parsed: list[dict], *, timeout_min: int = 5,
               started_ago: float = 0.0, kill_ok: bool = True,
               subprocess_spy=None, **kw):
    """跑 _watch_loop，隔离遥测日志、外部副作用与真实时钟。"""
    old_log = mod.DISPATCH_LOG
    mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
    clock = _FakeClock()
    start_times = [clock.time() - started_ago for _ in parsed]
    runner = subprocess_spy if subprocess_spy is not None else (lambda *a, **k: None)
    try:
        with mock.patch.object(mod.time, "time", clock.time), \
             mock.patch.object(mod.time, "sleep", clock.sleep), \
             mock.patch.object(mod.subprocess, "run", runner), \
             mock.patch.object(mod, "_lookup_model_cost",
                               lambda _m: {"input": 0.0, "output": 0.0}), \
             mock.patch.object(mod, "_kill_worker", lambda _l, _w: kill_ok):
            rc = mod._watch_loop(parsed, start_times, timeout_min=timeout_min,
                                 progress=False, **kw)
        rows = []
        if mod.DISPATCH_LOG.is_file():
            rows = [json.loads(l) for l in
                    mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rc, rows
    finally:
        mod.DISPATCH_LOG = old_log


# ─── A1 · 退出码契约 ─────────────────────────────────────────────────
class TestExitCodeContract:
    """dispatch --watch 的退出码与 stdout 必须忠实反映真实结果。

    历史缺陷：`_watch_loop` 用单一 `landed` 集合混淆「落盘」与「失败结案」，
    失败分支执行 `landed.add(i)`，致循环末尾判定「全部落盘」、
    打印「✅ 全部 worker 完成」并返回 0 —— 失败对外表现为成功。
    """

    def test_nonzero_exit_zero_artifact_returns_2(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", marker="exit=1\n", log="boom")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "全部 worker 完成" not in capsys.readouterr().out

    def test_zero_exit_zero_artifact_returns_2(self, capsys):
        """exit=0 但期望产物未落盘 —— §五 越界写入/路径碰撞的指纹，必须算失败。

        `_watch_loop` 只判 `exit_code is not None`，exit=0 与非零走同一分支；
        契约若只写「非零退出」，这条真实终结路径就会继续表现为成功。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", marker="exit=0\n", log="wrote nothing")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            out = capsys.readouterr().out
            assert "全部 worker 完成" not in out
            assert "exit=0" in out

    def test_launcher_error_returns_2(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", error="launcher blew up")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "全部 worker 完成" not in capsys.readouterr().out

    def test_missing_work_dir_returns_2(self):
        """work_dir 缺失 → 无法双监视、产物无从验证 → 归 failed。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p = _worker(td, "w0")
            p["work_dir"] = None
            rc, _ = _run_watch(td, [p])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_all_landed_returns_0(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", artifact="real content")])
            assert rc == 0
            assert "全部 worker 完成" in capsys.readouterr().out

    def test_partial_landed_partial_failed_returns_2(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "ok", artifact="content"),
                      _worker(td, "bad", marker="exit=1\n", log="boom")]
            rc, _ = _run_watch(td, parsed)
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_failure_plus_timeout_returns_1(self):
        """混合结局优先级：看门狗超时(1) 优先于确定性失败(2)。

        确定性失败是「已结案的失败」；未结案失联的进程仍可能在消耗预算，
        不能被已记录的失败掩盖。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "bad", marker="exit=1\n", log="boom"),
                      _worker(td, "slow")]  # 无 marker → 永不结案 → 超时
            rc, _ = _run_watch(td, parsed, timeout_min=1, started_ago=600)
            assert rc == 1

    def test_timeout_alone_returns_1(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "slow")], timeout_min=1, started_ago=600)
            assert rc == 1

    def test_settled_failure_not_double_killed(self):
        """已结案的 failed worker 不得在 deadline 分支被二次 kill / 二次遥测。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            killed: list[str] = []
            old_log = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
            clock = _FakeClock()
            parsed = [_worker(td, "bad", marker="exit=1\n", log="boom")]
            try:
                with mock.patch.object(mod.time, "time", clock.time), \
                     mock.patch.object(mod.time, "sleep", clock.sleep), \
                     mock.patch.object(mod, "_kill_worker",
                                       lambda label, _w: (killed.append(label), True)[1]):
                    rc = mod._watch_loop(parsed, [clock.time() - 600], timeout_min=1,
                                         progress=False)
            finally:
                mod.DISPATCH_LOG = old_log
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert killed == [], f"已结案的 worker 被二次 kill: {killed}"

    def test_exit_zero_no_artifact_has_own_outcome_detail(self):
        """exit=0 零产物应有可区分的 outcome_detail，便于事后归因写入路径错误。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [_worker(td, "w0", marker="exit=0\n", log="nothing")])
            assert any(r.get("outcome_detail") == "error:exit_0_no_artifact" for r in rows), rows


# ─── A3 · PID 捕获与按 PID 终止 ──────────────────────────────────────
class TestPidCaptureAndKill:
    """看门狗的 kill 必须真的具备终止能力。

    历史缺陷：`_kill_worker` 按 `WINDOWTITLE eq ocsr-*<label>*` 过滤，
    但代码从未设置过 pwsh 的窗口标题（`--title` 是 opencode 会话标题），
    且 launcher 以 `-WindowStyle Hidden` 启动（隐藏进程窗口标题为 N/A）——
    过滤器匹配不到任何进程，函数又无条件 return True，调用点也不检查返回值。
    """

    def test_launch_command_captures_pid(self):
        with tempfile.TemporaryDirectory() as t:
            cmd = mod._launch_command(Path(t))
            assert "-PassThru" in cmd
            assert mod.PID_FILE_NAME in cmd
            assert "$proc.Id" in cmd

    def test_kill_uses_pid_not_window_title(self):
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_text("4242\n", encoding="utf-8")
            (wd / mod.PROCESS_IDENTITY_FILE).write_text(
                json.dumps({"pid": 4242, "creation_time": "t",
                            "command_fingerprint": "fingerprint"}), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")

            tables = [{4242: {"parent_pid": 1, "creation_time": "t"}},
                      {1: {"parent_pid": 0, "creation_time": "system"}}]
            with mock.patch.object(mod, "_query_process_table", side_effect=tables), \
                 mock.patch.object(mod.subprocess, "run", fake_run):
                assert mod._kill_worker("w0", wd) is True
            assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
            assert not any("WINDOWTITLE" in str(a) for a in calls)
            assert not any("/IM" in str(a) for a in calls), "禁止无差别 taskkill /IM"

    def test_kill_reports_failure_on_nonzero_returncode(self):
        """taskkill 非零退出必须返回 False —— 旧实现无条件 return True。"""
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_text("4242", encoding="utf-8")
            (wd / mod.PROCESS_IDENTITY_FILE).write_text(
                json.dumps({"pid": 4242, "creation_time": "t",
                            "command_fingerprint": "fingerprint"}), encoding="utf-8")
            with mock.patch.object(mod, "_query_process_table",
                                   return_value={4242: {"parent_pid": 1, "creation_time": "t"}}), \
                 mock.patch.object(
                mod.subprocess, "run",
                lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "not found"),
            ):
                assert mod._kill_worker("w0", wd) is False

    def test_kill_without_pid_file_returns_false(self):
        with tempfile.TemporaryDirectory() as t:
            assert mod._kill_worker("w0", Path(t)) is False

    def test_kill_without_process_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_text("4242", encoding="utf-8")
            taskkill_calls = []
            with mock.patch.object(
                mod, "_query_process_table",
                return_value={4242: {"parent_pid": 1, "creation_time": "t"}},
            ), mock.patch.object(
                mod.subprocess, "run", side_effect=lambda *a, **k: taskkill_calls.append(a)
            ):
                assert mod._kill_worker("w0", wd) is False
            assert taskkill_calls == []

    def test_read_pid_tolerates_whitespace_and_bom(self):
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_bytes(b"\xef\xbb\xbf 1234 \r\n")
            assert mod._read_pid(wd) == 1234

    def test_kill_failure_recorded_as_killed_failed(self):
        """kill 失败时 outcome_detail 必须是 killed:failed，不得降级为普通 stall。

        `killed:failed` 的定义 = kill 操作本身失败、目标进程可能仍在运行，
        **不**表示「进程已被杀死」。记为普通 stall 会掩盖
        「看门狗已放弃止损而 worker 仍在消耗模型调用」这一事实。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "w0")]
            _, rows = _run_watch(td, parsed, timeout_min=1, started_ago=600,
                                 kill_ok=False,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    def test_kill_success_recorded_as_stall_not_killed_failed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [_worker(td, "w0")], timeout_min=1, started_ago=600,
                                 kill_ok=True,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert not any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    @pytest.mark.skipif(sys.platform != "win32", reason="taskkill 仅 Windows")
    def test_kill_actually_terminates_process(self):
        """离线集成测试：真起一个进程、真杀掉、断言它确实消失。

        单元测试只能证明「走了 PID 路径」，证明不了「进程真的死了」——
        这条补上那一层，且不触发任何模型调用。
        整链（launcher→opencode→进程树终止）仍需一次真实派发的人工复验。
        """
        if not mod._query_process_table():
            pytest.skip("Win32_Process inspection unavailable; stop verification must fail closed")
        exe = "pwsh" if shutil.which("pwsh") else "powershell"
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with tempfile.TemporaryDirectory() as t:
                wd = Path(t)
                (wd / mod.PID_FILE_NAME).write_text(str(proc.pid), encoding="utf-8")
                assert proc.poll() is None, "被测进程未能启动"
                identity = mod._query_process_table().get(proc.pid)
                if not identity or not identity.get("creation_time"):
                    pytest.skip("目标进程创建身份不可读")
                (wd / mod.PROCESS_IDENTITY_FILE).write_text(json.dumps({
                    "pid": proc.pid, "creation_time": identity["creation_time"],
                    "command_fingerprint": "integration-test",
                }), encoding="utf-8")
                assert mod._kill_worker("dummy", wd) is True
                for _ in range(60):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                assert proc.poll() is not None, "taskkill 报告成功但目标进程仍存活"
        finally:
            if proc.poll() is None:
                proc.kill()


# ─── A2 · DB 锁恢复交回 ──────────────────────────────────────────────
class TestDbLockRecoveryHandoff:
    """DB 锁必须交回上层，watcher 不得自行发起第二次模型调用。

    新尝试需要上层重新 reserve/settle，并计入既有预算与尝试上限。
    """

    @staticmethod
    def _locked_worker(td: Path) -> dict:
        return _worker(td, "w0", marker="exit=1\n",
                       log="Error: database is locked", launcher=True)

    def test_db_lock_is_deterministic_failure(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [self._locked_worker(td)],
                               timeout_min=1, started_ago=600)
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_watcher_never_relaunches_on_db_lock(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cmds: list[str] = []

            def spy(argv, **kw):
                if (isinstance(argv, list) and argv
                        and "Start-Process" in str(argv[-1])):
                    cmds.append(str(argv[-1]))
                return None

            _run_watch(td, [self._locked_worker(td)], timeout_min=1,
                       started_ago=600, subprocess_spy=spy)
            assert cmds == [], f"watcher 发起了未过新 reserve/settle 的调用: {cmds}"

    def test_db_lock_never_consumes_hidden_attempt(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p = self._locked_worker(td)
            wd = p["work_dir"]
            relaunches: list[object] = []

            def spy(argv, **kw):
                if isinstance(argv, list) and argv and "Start-Process" in str(argv[-1]):
                    relaunches.append(argv)
                    # 若 watcher 错误重派，持续写回 DB 锁现场以暴露无限重派
                    (wd / "start.marker").write_text("exit=1\n", encoding="utf-8")
                    (wd / "run.log").write_text("database is locked", encoding="utf-8")
                return None

            rc, _ = _run_watch(td, [p], timeout_min=1, started_ago=600,
                               subprocess_spy=spy)
            assert len(relaunches) == 0
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_db_lock_has_no_retry_telemetry(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [self._locked_worker(td)],
                                 timeout_min=1, started_ago=600)
            assert not any(r.get("outcome_detail") == "error:database-locked-retry"
                           for r in rows), rows
            assert all(r.get("failure_retry_index", 0) == 0 for r in rows), rows


class TestWatchdogSessionRecovery:
    """20260911 watchdog/session recovery contract boundaries; all deterministic."""

    def test_launcher_uses_json_and_binds_only_unique_session(self):
        launcher = mod._pwsh_code("-m vendor/model --title worker")
        assert "opencode run $prompt --format json" in launcher
        assert "sessionID" in launcher
        assert "$sessionIDs.Count -ne 1" in launcher
        assert mod.SESSION_BINDING_FILE in launcher
        assert "ToUniversalTime().Ticks.ToString" in launcher
        assert "InvariantCulture" in launcher

    def test_missing_or_ambiguous_session_only_disables_resume(self, tmp_path):
        launcher = mod._pwsh_code("-m vendor/model --title worker")
        session_guard = launcher.split("if ($sessionIDs.Count -ne 1)", 1)[1].split(
            '"exit=$opencodeExit"', 1)[0]
        assert "$opencodeExit = 1" not in session_guard
        assert "error.log" not in session_guard

        missing = _worker(tmp_path, "missing-session", artifact="complete")
        rc, _ = _run_watch(tmp_path, [missing])
        assert rc == 0
        assert mod._write_resume_material(
            missing, old_process_stop_verified=True, recovery_safe=True,
            attempt_index=1, converge_invocation_id="inv-missing",
        ) is False
        missing_material = json.loads(
            (missing["work_dir"] / mod.RESUME_MATERIAL_FILE).read_text(encoding="utf-8"))
        assert missing_material["reason"] == "session_missing"

        ambiguous = _worker(tmp_path, "ambiguous-session", artifact="complete")
        (ambiguous["work_dir"] / "run.log").write_text(
            '{"sessionID":"s1"}\n{"sessionID":"s2"}\n', encoding="utf-8")
        rc, _ = _run_watch(tmp_path, [ambiguous])
        assert rc == 0
        assert mod._write_resume_material(
            ambiguous, old_process_stop_verified=True, recovery_safe=True,
            attempt_index=1, converge_invocation_id="inv-ambiguous",
        ) is False
        ambiguous_material = json.loads(
            (ambiguous["work_dir"] / mod.RESUME_MATERIAL_FILE).read_text(encoding="utf-8"))
        assert ambiguous_material["reason"] == "session_ambiguous"

    def test_process_table_uses_same_shell_stable_creation_identity(self):
        payload = json.dumps([
            {"ProcessId": 42, "ParentProcessId": 1,
             "CreationTimeUtcTicks": "638932608000000000"},
        ])
        completed = subprocess.CompletedProcess([], 0, payload, "")
        with mock.patch.object(mod.subprocess, "run", return_value=completed) as run:
            table = mod._query_process_table()
        command = run.call_args.args[0][-1]
        assert "ToUniversalTime().Ticks.ToString" in command
        assert "InvariantCulture" in command
        assert table[42]["creation_time"] == "638932608000000000"

    def test_only_top_level_unique_session_id_is_accepted(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text(
            '{"sessionID":"s1","nested":{"sessionID":"ignored"}}\n'
            '{"type":"tool","sessionID":"s1"}\n', encoding="utf-8")
        ids, count, _ = mod._extract_session_ids(log)
        assert ids == {"s1"}
        assert count == 2
        log.write_text('{"sessionID":"s1"}\n{"sessionID":"s2"}\n', encoding="utf-8")
        assert mod._extract_session_ids(log)[0] == {"s1", "s2"}

    def test_artifact_seen_is_not_landed_while_process_runs(self, tmp_path):
        worker = _worker(tmp_path, "active", marker="", artifact="partial")
        ledger = tmp_path / "ledger.jsonl"
        rc, _ = _run_watch(
            tmp_path, [worker], timeout_min=1, started_ago=61,
            ledger=ledger, total_timeout_min=2, max_renewals=0,
        )
        events = [json.loads(line)["event"] for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert rc == 1
        assert "artifact_seen" in events
        assert "landed" not in events

    def test_total_deadline_is_immutable_and_renewal_is_finite(self, tmp_path):
        worker = _worker(tmp_path, "active", marker="", artifact="partial")
        ledger = tmp_path / "ledger.jsonl"
        with mock.patch.object(mod, "_has_auditable_progress", side_effect=[True, False]):
            rc, _ = _run_watch(
                tmp_path, [worker], timeout_min=1, started_ago=61,
                ledger=ledger, total_timeout_min=10, renewal_minutes=1, max_renewals=1,
            )
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        renewals = [row for row in rows if row["event"] == "renewed"]
        inspections = [row for row in rows if row["event"] == "inspection_due"]
        assert rc == 1
        assert len(renewals) == 1
        assert len({row["total_deadline"] for row in inspections + renewals}) == 1

    def test_snapshot_records_event_time_tool_and_subagent_status(self, tmp_path):
        worker = _worker(tmp_path, "active", marker="")
        (worker["work_dir"] / "run.log").write_text(
            '{"type":"tool_use","timestamp":100,"part":{"type":"tool",'
            '"tool":"bash","callID":"c1","state":{"status":"completed"}}}\n'
            '{"type":"tool_use","timestamp":200,"part":{"type":"tool",'
            '"tool":"task","callID":"c2","state":{"status":"running"}}}\n',
            encoding="utf-8",
        )
        with mock.patch.object(mod, "_query_process_table", return_value={}):
            snapshot = mod._capture_worker_snapshot(worker, 300.0, None)
        assert snapshot["latest_json_event_timestamp"] == 200
        assert snapshot["tool_status"] == {"tool": "task", "status": "running", "call_id": "c2"}
        assert snapshot["subagent_status"] == snapshot["tool_status"]

    @pytest.mark.parametrize("missing_part", ["pid", "identity", "process_table"])
    def test_unknown_process_identity_never_counts_as_progress(self, tmp_path, missing_part):
        worker = _worker(tmp_path, f"unknown-{missing_part}", marker="", artifact="partial")
        wd = worker["work_dir"]
        if missing_part != "pid":
            (wd / mod.PID_FILE_NAME).write_text("42", encoding="utf-8")
        if missing_part != "identity":
            (wd / mod.PROCESS_IDENTITY_FILE).write_text(json.dumps({
                "pid": 42, "creation_time": "c1", "command_fingerprint": "f1",
            }), encoding="utf-8")
        table = {} if missing_part == "process_table" else {
            42: {"parent_pid": 1, "creation_time": "c1"},
        }
        with mock.patch.object(mod, "_query_process_table", return_value=table):
            snapshot = mod._capture_worker_snapshot(worker, 300.0, None)
        assert snapshot["process_tree_status"] == "unknown"
        assert mod._has_auditable_progress(
            {"json_event_count": 0, "latest_file_change_ns": 0,
             "artifact_state": "missing"},
            snapshot,
        ) is False

    def test_unknown_process_is_reported_then_times_out_without_renewal(self, tmp_path):
        worker = _worker(tmp_path, "unknown-process", marker="", artifact="partial")
        (worker["work_dir"] / "run.log").write_text(
            '{"type":"tool_use","timestamp":100}\n', encoding="utf-8")
        ledger = tmp_path / "ledger.jsonl"
        rc, _ = _run_watch(
            tmp_path, [worker], timeout_min=1, started_ago=61, ledger=ledger,
            total_timeout_min=2, renewal_minutes=1, max_renewals=1,
            timeout_policy=mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT,
        )
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert rc == 1
        assert not any(row["event"] == "renewed" for row in rows)
        reported = next(row for row in rows if row["event"] == "reported")
        assert reported["process_tree_status"] == "unknown"
        assert any(row["event"] == "handoff_required" for row in rows)

    def test_launch_baseline_prevents_historical_events_from_renewing(self, tmp_path):
        worker = _worker(tmp_path, "active", marker="")
        (worker["work_dir"] / "run.log").write_text(
            '{"type":"tool_use","timestamp":100,"part":{"type":"tool",'
            '"tool":"bash","state":{"status":"completed"}}}\n', encoding="utf-8")
        ledger = tmp_path / "ledger.jsonl"
        rc, _ = _run_watch(
            tmp_path, [worker], timeout_min=1, started_ago=61, ledger=ledger,
            total_timeout_min=2, renewal_minutes=1, max_renewals=1,
        )
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert rc == 1
        assert not any(row["event"] == "renewed" for row in rows)
        inspection = next(row for row in rows if row["event"] == "inspection_due")
        assert inspection["inspection_baseline"]["json_event_count"] == 1
        assert inspection["latest_json_event_timestamp"] == 100

    def test_hierarchical_report_tracks_until_explicit_handoff(self, tmp_path):
        worker = _worker(tmp_path, "active", marker="")
        ledger = tmp_path / "ledger.jsonl"
        rc, _ = _run_watch(
            tmp_path, [worker], timeout_min=1, started_ago=61, ledger=ledger,
            total_timeout_min=3, max_renewals=0,
            timeout_policy=mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT,
        )
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        report = next(row for row in rows if row["event"] == "reported")
        handoff = next(row for row in rows if row["event"] == "handoff_required")
        assert rc == 1
        assert report["tracking_continues"] is True
        assert handoff["wall_min"] >= 3
        assert report["total_deadline"] == handoff["total_deadline"]

    def test_resume_material_requires_bound_session_verified_stop_and_new_attempt(self, tmp_path):
        wd = tmp_path / "wd"
        wd.mkdir()
        (wd / mod.PID_FILE_NAME).write_text("42", encoding="utf-8")
        identity = {"pid": 42, "creation_time": "c1", "command_fingerprint": "f1"}
        (wd / mod.PROCESS_IDENTITY_FILE).write_text(json.dumps(identity), encoding="utf-8")
        binding = {**identity, "session_id": "session-1"}
        (wd / mod.SESSION_BINDING_FILE).write_text(json.dumps(binding), encoding="utf-8")
        (wd / "run.log").write_text('{"sessionID":"session-1"}\n', encoding="utf-8")
        p = {"work_dir": wd, "output": tmp_path / "out.md", "model": "vendor/model",
             "execution_dir": tmp_path}
        assert mod._write_resume_material(
            p, old_process_stop_verified=True, recovery_safe=True,
            attempt_index=1, converge_invocation_id="inv-1",
        ) is True
        material = json.loads((wd / mod.RESUME_MATERIAL_FILE).read_text(encoding="utf-8"))
        assert material["requires_new_reservation"] is True
        assert material["requires_settle"] is True
        assert material["next_attempt_index"] == 2
        assert material["resume_argv"][material["resume_argv"].index("--session") + 1] == "session-1"
        assert mod._write_resume_material(
            p, old_process_stop_verified=False, recovery_safe=True,
            attempt_index=1, converge_invocation_id="inv-1",
        ) is False

    def test_residual_descendant_blocks_stop_verification(self, tmp_path):
        (tmp_path / mod.PID_FILE_NAME).write_text("42", encoding="utf-8")
        before = {
            42: {"parent_pid": 1, "creation_time": "c1"},
            43: {"parent_pid": 42, "creation_time": "c2"},
        }
        after = {43: {"parent_pid": 1, "creation_time": "c2"}}
        taskkill = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(mod, "_query_process_table", side_effect=[before] + [after] * 20), \
             mock.patch.object(mod.subprocess, "run", return_value=taskkill), \
             mock.patch.object(mod.time, "sleep", lambda _s: None):
            assert mod._kill_worker("w", tmp_path) is False
