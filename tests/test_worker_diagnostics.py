"""Failure attribution and exact-session metadata regression, no model calls."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ocsr_diagnostic_test", ROOT / "scripts/ocsr_dispatch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
START_MS = 1793466584000


def bind(wd, sid="ses_bound"):
    identity = {"pid": 123, "creation_time": str(621355968000000000 + START_MS * 10000),
                "command_fingerprint": "exact-command"}
    (wd / "pid.txt").write_text("123", encoding="ascii")
    (wd / "process-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (wd / "session-binding.json").write_text(json.dumps({**identity, "session_id": sid}), encoding="utf-8")
    (wd / "run.log").write_text(json.dumps({"sessionID": sid, "type": "step_start",
        "timestamp": START_MS + 1000}) + "\n", encoding="utf-8")


def database(path, rows):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE session_message(session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
        con.executemany("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?)", rows)


def assistant(sid="ses_bound", *, created=None, seq=1, status="streaming", completed=False):
    data = {"content": [{"type": "reasoning", "text": "SECRET_REASONING"},
                       {"type": "tool", "name": "write", "time": {"created": START_MS + 19000},
                        "state": {"status": status, "input": "SECRET_INPUT", "output": "SECRET_OUTPUT"}}],
            "time": {"completed": START_MS + 20000} if completed else {}}
    return (sid, "assistant", seq, START_MS + 10000 if created is None else created,
            START_MS + 20000, json.dumps(data))


def test_cli_silence_is_opaque_not_provider_failure(tmp_path):
    bind(tmp_path)
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000)
    assert result["last_phase"] == "generation_pending"
    assert result["phase_source"] == "cli"
    assert result["root_cause"] == "unknown"
    assert result["event_age_seconds"] == 59
    assert result["native_session"]["status"] == "not_requested"


def test_native_metadata_uses_bound_session_and_excludes_bodies(tmp_path):
    bind(tmp_path)
    db = tmp_path / "native.db"
    database(db, [assistant(), assistant("ses_unrelated", seq=99, status="error")])
    before = db.read_bytes()
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000, db)
    assert db.read_bytes() == before
    assert result["last_phase"] == "tool_call_streaming"
    assert result["phase_source"] == "session_db"
    assert result["native_session"]["tool"]["input_chars"] == len("SECRET_INPUT")
    assert result["root_cause"] == "unknown"
    assert "SECRET_" not in json.dumps(result)
    assert "ses_unrelated" not in json.dumps(result)


@pytest.mark.parametrize("status,phase", [("pending", "tool_pending"), ("running", "tool_running"),
    ("completed", "generation_pending"), ("error", "tool_error_reported")])
def test_native_phase_is_observation_not_permission_or_business_success(tmp_path, status, phase):
    bind(tmp_path)
    db = tmp_path / "native.db"
    database(db, [assistant(status=status)])
    assert mod._read_native_session_metadata(tmp_path, db)["last_phase"] == phase


def test_assistant_completion_is_not_artifact_acceptance(tmp_path):
    bind(tmp_path)
    db = tmp_path / "native.db"
    database(db, [assistant(completed=True, status="completed")])
    result = mod._read_native_session_metadata(tmp_path, db)
    assert result["last_phase"] == "assistant_step_complete"
    assert result["step_completed"] is True
    assert "artifact_state" not in result


@pytest.mark.parametrize("status", [[], {}, 123, None])
def test_unexpected_cli_tool_status_does_not_break_stop_diagnostics(tmp_path, status):
    (tmp_path / "run.log").write_text(json.dumps({"type": "tool", "part": {
        "type": "tool", "state": {"status": status}}}) + "\n", encoding="utf-8")
    assert mod._worker_diagnostics(tmp_path)["last_phase"] == "tool_state_unknown"


@pytest.mark.parametrize("event", [
    {"type": "tool", "status": "running"},
    {"type": "tool", "part": {"type": "tool", "status": "running"}},
    {"type": "tool", "part": {"type": "tool", "state": {"status": "running"}}},
])
def test_cli_phase_uses_same_status_fallback_as_snapshot(tmp_path, event):
    (tmp_path / "run.log").write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert mod._event_runtime_status(tmp_path / "run.log")[1]["status"] == "running"
    assert mod._worker_diagnostics(tmp_path)["last_phase"] == "tool_running"


@pytest.mark.parametrize("closed", [False, True])
def test_step_boundary_does_not_refresh_or_erase_unclosed_tool_observation(tmp_path, closed):
    events = [{"type": "tool", "status": "running", "timestamp": START_MS + 1000}]
    if closed:
        events.append({"type": "tool", "status": "completed", "timestamp": START_MS + 2000})
    events.append({"type": "step_finish", "timestamp": START_MS + 3000})
    (tmp_path / "run.log").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000)
    assert result["last_phase"] == ("between_steps" if closed else "tool_running")
    assert result["event_age_seconds"] == 57
    assert result["phase_age_seconds"] == (57 if closed else 59)


def test_newer_native_completion_closes_retained_cli_tool_observation(tmp_path):
    bind(tmp_path)
    events = [{"sessionID": "ses_bound", "type": "tool", "status": "running", "timestamp": START_MS + 1000},
              {"type": "step_finish", "timestamp": START_MS + 30000}]
    (tmp_path / "run.log").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    db = tmp_path / "native.db"
    database(db, [assistant(completed=True, status="completed")])
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000, db)
    assert result["last_phase"] == "assistant_step_complete"
    assert result["phase_source"] == "session_db"


def test_native_no_tool_is_absent_observation(tmp_path):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = list(assistant())
    data = json.loads(row[-1])
    data["content"] = [{"type": "text", "text": "SECRET_TEXT"}]
    row[-1] = json.dumps(data)
    database(db, [tuple(row)])
    result = mod._read_native_session_metadata(tmp_path, db)
    assert result["tool"]["status"] == "not_observed"
    assert "SECRET" not in json.dumps(result)


def test_tool_timing_metadata_never_projects_non_scalar_body(tmp_path):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = list(assistant())
    data = json.loads(row[-1])
    data["content"][-1]["time"]["created"] = {"unexpected": "SECRET_BODY"}
    data["content"][-1]["name"] = {"unexpected": "SECRET_NAME"}
    row[-1] = json.dumps(data)
    database(db, [tuple(row)])
    result = mod._read_native_session_metadata(tmp_path, db)
    assert result["tool"]["created_at_ms"] is None
    assert result["tool"]["tool"] is None
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("kind,phase", [("reasoning", "reasoning_emitted"),
                                       ("future_event", "event_observed_unknown_phase")])
def test_unmapped_event_is_distinct_from_absent_evidence(tmp_path, kind, phase):
    (tmp_path / "run.log").write_text(json.dumps({"type": kind}) + "\n", encoding="utf-8")
    result = mod._worker_diagnostics(tmp_path)
    assert result["last_phase"] == phase
    assert result["last_event_type"] == kind
    assert result["phase_source"] == "cli"


@pytest.mark.parametrize("case", ["older", "future"])
def test_database_cannot_overwrite_newer_or_known_time_with_future_observation(tmp_path, case):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = list(assistant())
    row[4] = START_MS + (500 if case == "older" else 1000000)
    database(db, [tuple(row)])
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000, db)
    assert result["native_session"]["status"] == "available"
    assert result["phase_source"] == "cli"


def test_object_input_has_no_claimed_character_length_and_active_tool_wins(tmp_path):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = list(assistant(status="running", completed=True))
    data = json.loads(row[-1])
    data["content"][-1]["state"]["input"] = {"secret": "SECRET_OBJECT"}
    row[-1] = json.dumps(data)
    database(db, [tuple(row)])
    result = mod._read_native_session_metadata(tmp_path, db)
    assert result["tool"]["input_chars"] is None
    assert result["last_phase"] == "tool_running"
    assert "SECRET_" not in json.dumps(result)


@pytest.mark.parametrize("case", ["blob_time", "wrong_message_shape"])
def test_unexpected_database_metadata_never_escapes_into_json(tmp_path, case):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = list(assistant())
    if case == "blob_time":
        row[4] = b"SECRET_BLOB"
    else:
        row[-1] = json.dumps({"some_future_schema": "SECRET_BODY"})
    database(db, [tuple(row)])
    result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000, db)
    assert result["native_session"]["status"] == "unavailable_or_unsupported"
    assert "SECRET" not in json.dumps(result)


def test_diagnose_command_is_read_only_and_missing_directory_fails(tmp_path, capsys):
    from argparse import Namespace
    assert mod.cmd_diagnose(Namespace(worker_dir=str(tmp_path / "absent"), session_db=None)) == 2
    bind(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert mod.cmd_diagnose(Namespace(worker_dir=str(tmp_path), session_db=None)) == 0
    assert json.loads(capsys.readouterr().out)["last_phase"] == "generation_pending"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_preflight_classifies_stderr_without_real_model_call(tmp_path, capsys, monkeypatch):
    from argparse import Namespace
    monkeypatch.setattr(mod, "_check_model_calls_disabled", lambda: None)
    monkeypatch.setattr(mod, "_validate_model_allowed", lambda _model: None)

    def launch_fixture(wd):
        (wd / "stderr.log").write_text("404 model not found\n", encoding="utf-8")
        (wd / "start.marker").write_text("exit=1\n", encoding="utf-8")
        return 123

    monkeypatch.setattr(mod, "_launch_worker", launch_fixture)
    assert mod.cmd_preflight(Namespace(model=["vendor/nonexistent"], timeout=1,
                                       work_dir=str(tmp_path))) == 1
    assert "vendor/nonexistent: 404" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["other_session", "older_invocation"])
def test_never_falls_back_to_old_or_latest_session(tmp_path, case):
    bind(tmp_path)
    db = tmp_path / "native.db"
    row = assistant("ses_other") if case == "other_session" else assistant(created=START_MS - 10000)
    database(db, [row])
    assert mod._read_native_session_metadata(tmp_path, db)["status"] == "no_current_assistant"


@pytest.mark.parametrize("case", ["unbound", "identity_mismatch", "ambiguous", "bad_start"])
def test_refuses_unverified_session_before_opening_database(tmp_path, case):
    bind(tmp_path)
    if case == "unbound":
        (tmp_path / "session-binding.json").unlink()
    elif case == "identity_mismatch":
        binding = json.loads((tmp_path / "session-binding.json").read_text())
        binding["pid"] = 999
        (tmp_path / "session-binding.json").write_text(json.dumps(binding))
    elif case == "ambiguous":
        with (tmp_path / "run.log").open("a") as stream:
            stream.write('{"sessionID":"ses_other"}\n')
    else:
        for name in ("process-identity.json", "session-binding.json"):
            data = json.loads((tmp_path / name).read_text())
            data["creation_time"] = "invalid"
            (tmp_path / name).write_text(json.dumps(data))
    result = mod._read_native_session_metadata(tmp_path, tmp_path / "missing.db")
    assert result["status"] in {"unbound", "unverified_start_time"}
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("case", ["missing", "unknown_schema", "not_sqlite", "malformed_json", "locked"])
def test_optional_database_failures_are_nonfatal_and_do_not_create_database(tmp_path, case):
    bind(tmp_path)
    db = tmp_path / "native.db"
    lock = None
    if case == "unknown_schema":
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE future_schema (id TEXT)")
    elif case == "not_sqlite":
        db.write_bytes(b"opaque format")
    elif case in {"malformed_json", "locked"}:
        row = list(assistant())
        if case == "malformed_json":
            row[-1] = "{invalid"
        database(db, [tuple(row)])
        if case == "locked":
            lock = sqlite3.connect(db)
            lock.execute("BEGIN EXCLUSIVE")
    try:
        result = mod._worker_diagnostics(tmp_path, (START_MS + 60000) / 1000, db)
        assert result["native_session"]["status"] == "unavailable_or_unsupported"
        assert result["last_phase"] == "generation_pending"
        assert result["root_cause"] == "unknown"
        if case == "missing":
            assert not db.exists()
    finally:
        if lock is not None:
            lock.close()
