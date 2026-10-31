"""Offline regression tests for OCSR event-log framing and native console encoding.

Only the documented contract is exercised here: no production function is
implemented in this module, and no real ``opencode`` process, model call or
network request is ever made.  The native cases drive a generated pwsh
launcher whose ``opencode`` command is a local Python fixture that writes raw
UTF-8 bytes to its own stdout/stderr.
"""
from __future__ import annotations

import codecs
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ocsr_encoding_test", ROOT / "scripts/ocsr_dispatch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

native_only = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None,
    reason="requires Windows and pwsh",
)

_REQUIRED_METRICS = (
    "status",
    "json_event_count",
    "json_errors",
    "non_json_lines",
    "partial_line",
    "invalid_utf8_lines",
)


def _write_bytes(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def read_log(path: Path):
    """Call ``_read_event_log`` and validate the shared metrics shape."""
    events, metrics = mod._read_event_log(path)
    assert isinstance(events, list), f"events must be a list, got {type(events)!r}"
    for event in events:
        assert isinstance(event, dict), f"events must contain dicts, got {type(event)!r}"
    for key in _REQUIRED_METRICS:
        assert key in metrics, f"metrics missing {key!r}: {sorted(metrics)}"
    assert metrics["status"] in {"missing", "ok", "damaged", "unreadable"}
    assert isinstance(metrics["partial_line"], bool)
    for key in ("json_event_count", "json_errors", "non_json_lines", "invalid_utf8_lines"):
        assert isinstance(metrics[key], int) and not isinstance(metrics[key], bool)
        assert metrics[key] >= 0
    return events, metrics


def _ids(result):
    """Tolerate either ``(ids, ...)`` or ``(id_string, ...)`` returns."""
    assert isinstance(result, tuple), f"expected a tuple API, got {type(result)!r}"
    value = result[0]
    if isinstance(value, str):
        return {value}
    return {item for item in value}


def test_missing_log_is_safe(tmp_path):
    events, metrics = read_log(tmp_path / "absent.ndjson")
    assert events == []
    assert metrics["status"] == "missing"
    assert metrics["json_event_count"] == 0
    assert metrics["json_errors"] == 0
    assert metrics["non_json_lines"] == 0
    assert metrics["invalid_utf8_lines"] == 0
    assert metrics["partial_line"] is False


def test_clean_lf_log_round_trips(tmp_path):
    payload = [{"type": "text", "n": 1}, {"type": "tool", "n": 2}, {"type": "step", "n": 3}]
    data = "".join(json.dumps(item) + "\n" for item in payload).encode("utf-8")
    events, metrics = read_log(_write_bytes(tmp_path / "clean.ndjson", data))
    assert events == payload
    assert metrics["status"] == "ok"
    assert metrics["json_event_count"] == 3
    assert metrics["json_errors"] == 0
    assert metrics["non_json_lines"] == 0
    assert metrics["partial_line"] is False


def test_bom_is_accepted_only_at_beginning(tmp_path):
    data = (
        codecs.BOM_UTF8
        + b'{"first": 1}\n'
        + codecs.BOM_UTF8
        + b'{"second": 2}\n'
    )
    events, metrics = read_log(_write_bytes(tmp_path / "bom.ndjson", data))
    assert events == [{"first": 1}]
    assert metrics["json_event_count"] == 1
    # A BOM in the middle of the stream is not stripped, so that line is rejected.
    assert metrics["json_errors"] + metrics["non_json_lines"] >= 1
    assert metrics["status"] == "damaged"


def test_broken_object_and_array_lines_count_as_json_errors(tmp_path):
    data = b'{"a": }\n[1, 2\n'
    events, metrics = read_log(_write_bytes(tmp_path / "broken.ndjson", data))
    assert events == []
    assert metrics["json_errors"] == 2
    assert metrics["non_json_lines"] == 0
    assert metrics["json_event_count"] == 0
    assert metrics["status"] == "damaged"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_values_are_not_valid_json_events(tmp_path, constant):
    path = _write_bytes(tmp_path / "nonfinite.ndjson", ('{"timestamp":' + constant + '}\n').encode("ascii"))
    events, metrics = read_log(path)
    assert events == []
    assert metrics["json_errors"] == 1


def test_plain_text_and_scalars_are_not_json_errors(tmp_path):
    data = b'hello world\n123\n"quoted"\nnull\ntrue\n'
    events, metrics = read_log(_write_bytes(tmp_path / "scalars.ndjson", data))
    assert events == []
    assert metrics["non_json_lines"] == 5
    assert metrics["json_errors"] == 0
    assert metrics["status"] == "damaged"


def test_invalid_utf8_line_is_skipped_without_replacement(tmp_path):
    data = b'{"ok": 1}\n{"bad": "\xff"}\n{"tail": 2}\n'
    events, metrics = read_log(_write_bytes(tmp_path / "bad-utf8.ndjson", data))
    assert events == [{"ok": 1}, {"tail": 2}]
    assert metrics["invalid_utf8_lines"] == 1
    assert metrics["json_errors"] == 0
    assert metrics["non_json_lines"] == 0
    assert metrics["json_event_count"] == 2
    assert metrics["status"] == "damaged"
    assert "\ufffd" not in json.dumps(events, ensure_ascii=False)


def test_truncated_final_line_is_partial_not_json_error(tmp_path):
    data = b'{"a": 1}\n{"b": 2, "c"'
    events, metrics = read_log(_write_bytes(tmp_path / "partial.ndjson", data))
    assert events == [{"a": 1}]
    assert metrics["partial_line"] is True
    assert metrics["json_errors"] == 0
    assert metrics["json_event_count"] == 1
    # An in-flight tail is incomplete evidence, not a completed corrupt event.
    with (tmp_path / "partial.ndjson").open("ab") as stream:
        stream.write(b': 3}\n')
    completed, final_quality = read_log(tmp_path / "partial.ndjson")
    assert completed == [{"a": 1}, {"b": 2, "c": 3}]
    assert final_quality["status"] == "ok"


def test_complete_object_without_trailing_newline_is_accepted(tmp_path):
    data = b'{"a": 1}\n{"b": 2}'
    events, metrics = read_log(_write_bytes(tmp_path / "no-final-lf.ndjson", data))
    assert events == [{"a": 1}, {"b": 2}]
    assert metrics["partial_line"] is False
    assert metrics["json_errors"] == 0
    assert metrics["json_event_count"] == 2
    assert metrics["status"] == "ok"


def test_literal_u2028_and_u0085_do_not_split_events(tmp_path):
    first = {"type": "text", "text": "start\u2028middle\u0085end"}
    second = {"type": "text", "text": "after"}
    data = (
        json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(second, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    events, metrics = read_log(_write_bytes(tmp_path / "separators.ndjson", data))
    assert events == [first, second]
    assert metrics["json_event_count"] == 2
    assert metrics["status"] == "ok"
    assert events[0]["text"] == "start\u2028middle\u0085end"


def test_crlf_lines_still_frame_on_lf_only(tmp_path):
    data = b'{"a": 1}\r\n{"b": 2}\r\n'
    events, metrics = read_log(_write_bytes(tmp_path / "crlf.ndjson", data))
    assert events == [{"a": 1}, {"b": 2}]
    assert metrics["json_event_count"] == 2


def test_directory_path_reports_unreadable(tmp_path):
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    events, metrics = read_log(directory)
    assert events == []
    assert metrics["status"] == "unreadable"


TOOL_EVENT = {
    "type": "tool",
    "timestamp": 1234,
    "sessionID": "ses_x",
    "part": {
        "type": "tool",
        "tool": "write",
        "state": {"status": "completed", "input": {"text": "baseline"}},
    },
}


def _tool_event_with_text(text: str) -> dict:
    event = json.loads(json.dumps(TOOL_EVENT))
    event["part"]["state"]["input"]["text"] = text
    return event


def test_session_and_runtime_status_survive_literal_separators(tmp_path):
    baseline = _tool_event_with_text("baseline")
    exotic = _tool_event_with_text("alpha\u2028beta\u0085gamma")

    plain_path = _write_bytes(
        tmp_path / "plain.ndjson",
        (json.dumps(baseline, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    exotic_path = _write_bytes(
        tmp_path / "exotic.ndjson",
        (json.dumps(exotic, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    # A corrupt line before the event must not change what the session sees.
    noisy_path = _write_bytes(
        tmp_path / "noisy.ndjson",
        b"garbage line\n" + (json.dumps(exotic, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    assert _ids(mod._extract_session_ids(plain_path)) == {"ses_x"}
    assert _ids(mod._extract_session_ids(exotic_path)) == {"ses_x"}
    assert _ids(mod._extract_session_ids(noisy_path)) == {"ses_x"}

    events, metrics = read_log(exotic_path)
    assert metrics["json_event_count"] == 1, "literal separators must not split an event"

    plain_status = mod._event_runtime_status(plain_path)
    exotic_status = mod._event_runtime_status(exotic_path)
    assert plain_status is not None
    assert exotic_status == plain_status, "U+2028/U+0085 changed tool runtime status"


# --- native console integration -------------------------------------------------

# The launcher is generated with a legacy CP936 console, exactly like a default
# Windows shell.  The new launcher must install its own native UTF-8 encoding.
CP936_PREFIX = (
    "[Console]::OutputEncoding=[Text.Encoding]::GetEncoding(936);\n"
    "$OutputEncoding=[Console]::OutputEncoding;\n"
)

STUB = """function opencode {
    & $env:OCSR_TEST_PYTHON $env:OCSR_TEST_PAYLOAD
    $global:LASTEXITCODE = $LASTEXITCODE
}
"""

NATIVE_EVENT = {
    "type": "tool",
    "timestamp": 1234,
    "sessionID": "ses_encoding",
    "part": {
        "type": "tool",
        "tool": "write",
        "state": {
            "status": "completed",
            "input": {"text": "C:\\temp\\out 中文 \"quoted\" 😀 分隔\u2028行\u0085尾"},
        },
    },
}

NATIVE_WARNING = "警告：编码回归测试 ⚠ 中文告警"

PAYLOAD_TEMPLATE = """import sys
LINE = @@LINE@@
WARN = @@WARN@@
sys.stdout.buffer.write((LINE + '\\n').encode('utf-8'))
sys.stdout.buffer.flush()
sys.stderr.buffer.write((WARN + '\\n').encode('utf-8'))
sys.stderr.buffer.flush()
sys.exit(7)
"""


def _payload_source() -> bytes:
    # Python literals need \U escapes for astral characters, not JSON's surrogate
    # pairs: the fixture must actually emit valid UTF-8 bytes before testing pwsh.
    line = ascii(json.dumps(NATIVE_EVENT, ensure_ascii=False))
    warn = ascii(NATIVE_WARNING)
    source = PAYLOAD_TEMPLATE.replace("@@LINE@@", line).replace("@@WARN@@", warn)
    return source.encode("utf-8")


def _strip_encoding_init(source: str):
    """Remove only the launcher's own encoding initialization lines."""
    original = source.splitlines(keepends=True)
    kept = [
        line
        for line in original
        if "[Console]::OutputEncoding = " not in line and "$OutputEncoding = " not in line
    ]
    removed = len(original) - len(kept)
    assert removed >= 1, (
        "negative control requires an encoding initialization line containing "
        "'[Console]::OutputEncoding = ' or '$OutputEncoding = '"
    )
    assert "[OcsrLauncherJob]::AttachCurrentProcess()" in "".join(kept)
    return "".join(kept)


def _prepare_job(tmp_path: Path, mutate=None):
    work = tmp_path / "job"
    work.mkdir()
    (work / "fixture_opencode.py").write_bytes(_payload_source())
    (work / "prompt.txt").write_bytes("offline encoding test".encode("utf-8"))
    source = CP936_PREFIX + STUB + mod._pwsh_code("-m test/model", cwd=str(work))
    if mutate is not None:
        source = mutate(source)
    (work / "launcher.ps1").write_bytes(source.encode("utf-8"))
    env = dict(os.environ)
    env.pop("OCSR_DISABLE_MODEL_CALLS", None)  # child-only; never touch os.environ
    env["OCSR_TEST_PYTHON"] = sys.executable
    env["OCSR_TEST_PAYLOAD"] = str(work / "fixture_opencode.py")
    return work, env


def _run_launcher(work: Path, env: dict, timeout: int = 25):
    process = None
    try:
        process = subprocess.Popen(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(work / "launcher.ps1")],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return process.wait(timeout=timeout)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _marker_text(work: Path) -> str:
    return (work / "start.marker").read_text(encoding="utf-8-sig", errors="replace")


@native_only
def test_native_console_utf8_end_to_end(tmp_path):
    work, env = _prepare_job(tmp_path)
    _run_launcher(work, env)

    assert not (work / "error.log").exists(), (work / "error.log").read_text(
        encoding="utf-8", errors="replace"
    )
    marker = _marker_text(work)
    assert "exit=7" in marker, marker

    # run.log must survive the CP936 console and parse to the exact original dict.
    log_text = (work / "run.log").read_text(encoding="utf-8-sig")
    assert "\ufffd" not in log_text, repr(log_text)
    log_lines = [line for line in log_text.split("\n") if line.strip()]
    assert len(log_lines) == 1, log_lines
    assert json.loads(log_lines[0]) == NATIVE_EVENT
    assert "\u2028" in log_text, "U+2028 must survive verbatim in the log"

    events, metrics = read_log(work / "run.log")
    assert events == [NATIVE_EVENT]
    assert metrics["status"] == "ok"
    assert metrics["json_event_count"] == 1

    # stderr is a separate UTF-8 stream; it must not be folded into JSON stdout.
    stderr_text = (work / "stderr.log").read_bytes().decode("utf-8-sig")
    assert NATIVE_WARNING in stderr_text, repr(stderr_text)
    assert "\ufffd" not in stderr_text, repr(stderr_text)

    binding = json.loads((work / "session-binding.json").read_text(encoding="utf-8-sig"))
    assert binding["session_id"] == "ses_encoding"


@native_only
def test_native_console_utf8_negative_control(tmp_path):
    work, env = _prepare_job(tmp_path, mutate=_strip_encoding_init)
    _run_launcher(work, env)

    raw = (work / "run.log").read_bytes()
    assert raw.strip(), "negative control produced no launcher output"
    decoded = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    expected = json.dumps(NATIVE_EVENT, ensure_ascii=False)

    try:
        parsed = json.loads([line for line in decoded.split("\n") if line.strip()][0])
    except (ValueError, IndexError):
        parsed = None

    assert parsed is None or parsed != NATIVE_EVENT, (
        "removing the encoding initialization must corrupt the native output, got: "
        f"{decoded[:200]!r}"
    )
    assert decoded.strip() != expected.strip()
