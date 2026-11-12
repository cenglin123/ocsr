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
        # CRLF 归一化（与原 text=True 的 universal newlines 一致）
        self.assertEqual("V:abc123\n", mod._decode_native(b"V:abc123\r\n"))

    def test_process_table_tolerates_non_utf8_command_line(self):
        # 进程表输出含本机代码页（CP936）中文命令行时仍能正确解析
        if os.name != "nt":
            self.skipTest("Windows 本机代码页用例")
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
        # 真正触达续接拒绝：模型在白名单、prompt 存在、合成 resume 目录可用，
        # 但进程表不可读（_query_process_table 返回空）→ 身份无法确认 → 拒绝 return 1
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp) / "worker"
            wd.mkdir()
            (wd / "worker-state.json").write_text(json.dumps({
                "state": "landed", "attempt_index": 1, "rework_index": 0}), encoding="utf-8")
            (wd / "start.marker").write_text("exit=0\n", encoding="utf-8")
            (wd / "launcher.ps1").write_text(
                "& opencode run $prompt --format json --standalone -m vendor/model-a\n",
                encoding="utf-8")
            (wd / "session-binding.json").write_text(json.dumps({
                "session_id": "ses_x", "cwd": str(wd), "pid": 4321,
                "creation_time": "1000", "command_fingerprint": "fp"}), encoding="utf-8")
            (wd / "process-identity.json").write_text(json.dumps({
                "pid": 4321, "creation_time": "1000", "command_fingerprint": "fp"}),
                encoding="utf-8")
            prompt = Path(tmp) / "prompt.txt"
            prompt.write_text("返工任务\n", encoding="utf-8")

            class Args:
                worker = [f"{prompt}|vendor/model-a|w1"]
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

            with patch.object(mod, "ALLOWED_MODELS", frozenset({"vendor/model-a"})), \
                 patch.object(mod, "_check_model_calls_disabled"), \
                 patch.object(mod, "_lookup_model_cost",
                              return_value={"input": 0.0, "output": 0.0}), \
                 patch.object(mod, "_query_process_table", return_value={}):
                rc = mod.cmd_dispatch(Args())
            self.assertEqual(1, rc)  # 进程表不可读 → 无法确认退出 → 拒绝续接（非零）


class BoardNativeDecodingTest(unittest.TestCase):
    """board 副本的 _decode_native 与 verify_board_process 的编码一致性。"""

    def test_board_decode_native_same_semantics(self):
        board_spec = importlib.util.spec_from_file_location(
            "ocsr_board_native", DISPATCH_PATH.parent / "ocsr_board.py")
        board = importlib.util.module_from_spec(board_spec)
        board_spec.loader.exec_module(board)
        self.assertEqual("中文", board._decode_native("中文".encode("utf-8")))
        if os.name == "nt":
            self.assertEqual("中文路径", board._decode_native("中文路径".encode("gbk")))
        self.assertEqual("", board._decode_native(None))


if __name__ == "__main__":
    unittest.main()
