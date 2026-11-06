"""Adapter-layer contract regression, bridged into the pytest suite.

`tests/adapter/` holds Node's built-in test runner suite. It exercises the real
`lib/*.js` modules against a locally installed DeepSeek Harness >= 0.2 runtime,
because the adapter's failure modes are host API drift that no Python test can
observe. The Node suite skips (and this bridge then skips too) when no runtime
or no Node executable is available; set ``DSH_RUNTIME_ROOT`` to point at one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_TESTS = REPO_ROOT / "tests" / "adapter"
SKIP_MARKER = "OCSR-ADAPTER-SKIP:"
TIMEOUT_SECONDS = 600


def test_dsh_adapter_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not found; adapter contract tests need Node.js")

    test_files = sorted(ADAPTER_TESTS.glob("*.test.mjs"))
    if not test_files:
        pytest.skip(f"no adapter test files under {ADAPTER_TESTS}")

    completed = subprocess.run(
        [node, "--test", *(str(path) for path in test_files)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode == 0 and SKIP_MARKER in output:
        reason = output.split(SKIP_MARKER, 1)[1].splitlines()[0].strip()
        pytest.skip(reason)
    assert completed.returncode == 0, output
