import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

DISPATCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_sid", DISPATCH_PATH)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class SessionIdToolingTest(unittest.TestCase):
    """instance_id 的机械化获取（消除人工转录）。"""

    def test_landed_session_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "session-binding.json").write_text(
                json.dumps({"session_id": "ses_abc123", "cwd": "C:/x"}), encoding="utf-8")
            self.assertEqual(", session=ses_abc123", mod._landed_session_note(wd))

    def test_landed_session_note_missing_or_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            self.assertEqual("", mod._landed_session_note(wd))  # 无文件
            (wd / "session-binding.json").write_text('{"session_id": 123}', encoding="utf-8")
            self.assertEqual("", mod._landed_session_note(wd))  # 非字符串

    def test_cmd_session_id_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "session-binding.json").write_text(
                json.dumps({"session_id": "ses_test_xyz"}), encoding="utf-8")

            class Args:
                worker_dir = str(wd)

            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mod.cmd_session_id(Args())
            self.assertEqual(0, rc)
            self.assertEqual("ses_test_xyz", buf.getvalue().strip())

    def test_cmd_session_id_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)

            class Args:
                worker_dir = str(wd)

            self.assertEqual(2, mod.cmd_session_id(Args()))  # 无文件
            (wd / "session-binding.json").write_text('{"session_id": ""}', encoding="utf-8")
            self.assertEqual(2, mod.cmd_session_id(Args()))  # 空 session_id


if __name__ == "__main__":
    unittest.main()
