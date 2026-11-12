import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DISPATCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_native", DISPATCH_PATH)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class NativeDecodingTest(unittest.TestCase):
    """原生命令输出的编码容错（本机代码页 CP936 中文不再导致整体失败）。"""

    def test_decode_native_utf8_and_mbcs(self):
        self.assertEqual("中文", mod._decode_native("中文".encode("utf-8")))
        # CP936 编码的中文（本机代码页）：UTF-8 strict 失败后按 mbcs 正确解码
        if os.name == "nt":
            raw = "中文路径".encode("gbk")  # CP936 同义
            self.assertEqual("中文路径", mod._decode_native(raw))
        self.assertEqual("", mod._decode_native(None))
        self.assertEqual("已是字符串", mod._decode_native("已是字符串"))

    def test_process_table_tolerates_non_utf8_command_line(self):
        # 进程表输出含本机代码页（CP936）中文命令行时仍能正确解析
        payload = [{"ProcessId": 100, "ParentProcessId": 0,
                    "CreationTimeUtcTicks": "1000",
                    "CommandLine": "pwsh -File 中文目录\\launcher.ps1"}]
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("gbk")  # 模拟本机代码页输出
        with patch.object(mod.subprocess, "run",
                          return_value=type("R", (), {"returncode": 0,
                                                      "stdout": raw_bytes, "stderr": b""})()):
            table = mod._query_process_table()
        self.assertIn(100, table)
        self.assertEqual("pwsh -File 中文目录\\launcher.ps1", table[100]["command"])

    def test_process_table_utf8_still_works(self):
        payload = [{"ProcessId": 100, "ParentProcessId": 0,
                    "CreationTimeUtcTicks": "1000", "CommandLine": "pwsh -File x.ps1"}]
        raw_bytes = json.dumps(payload).encode("utf-8")
        with patch.object(mod.subprocess, "run",
                          return_value=type("R", (), {"returncode": 0,
                                                      "stdout": raw_bytes, "stderr": b""})()):
            table = mod._query_process_table()
        self.assertEqual("pwsh -File x.ps1", table[100]["command"])

    def test_resume_reject_returns_nonzero(self):
        # 拒绝续接的退出码非零（现有契约 return 1，回归保证）
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp) / "missing-worker"
            # 不存在的目录 → resume 校验拒绝
            class Args:
                worker = ["p|vendor/model-a|w1"]
                output_dir = str(Path(tmp) / "out")
                output_pattern = "{label}.md"
                stagger = 0
                timeout = 15
                timeout_policy = mod.TIMEOUT_POLICY_LEAF_KILL
                watch = False
                progress = False
                work_dir = None
                harness = "test"
                meta = []
                ledger_dir = None
                forbid_paths = []
                attempt_index = None
                rework_index = None
                recovery_safe = False
                resume_from = str(wd)
                resume_session = None

            with patch.object(mod, "_check_model_calls_disabled"):
                rc = mod.cmd_dispatch(Args())
            self.assertNotEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
