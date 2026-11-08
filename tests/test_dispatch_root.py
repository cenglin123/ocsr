import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

DISPATCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_root", DISPATCH_PATH)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class DispatchRootDecoupleTest(unittest.TestCase):
    def test_dispatch_base_ignores_work_dir(self):
        # --work-dir 不再影响派发目录位置；--dispatch-root 才决定
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_root = Path(tmp) / "dr"
            self.assertEqual(dispatch_root, mod._resolve_dispatch_base(dispatch_root))
            self.assertEqual(Path(os.environ.get("TEMP", "/tmp")),
                             mod._resolve_dispatch_base(None))

    def test_execution_dir_follows_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp) / "worktree"
            self.assertEqual(wd, mod._resolve_execution_dir(wd))
            self.assertEqual(Path.cwd(), mod._resolve_execution_dir(None))

    def test_register_dispatch_root_writes_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "dispatch-roots.json"
            base = Path(tmp) / "base"
            base.mkdir()
            with patch.object(mod, "DISPATCH_ROOTS_REGISTRY", reg):
                mod._register_dispatch_root(base)
                mod._register_dispatch_root(base)  # 重复登记去重
                data = json.loads(reg.read_text(encoding="utf-8"))
                self.assertEqual(1, len(data["roots"]))
                self.assertEqual(str(base.resolve()), data["roots"][0]["path"])
                self.assertIn("last_used", data["roots"][0])
                raw = reg.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_register_prunes_stale_and_tolerates_bad_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "dispatch-roots.json"
            old = (datetime.now().astimezone() - timedelta(days=40)).isoformat()
            reg.write_text(json.dumps({"roots": [
                {"path": "C:/old", "last_used": old},
                {"path": "not-a-dir-xyz", "last_used": "bad-date"},
                "garbage",
            ]}), encoding="utf-8")
            base = Path(tmp) / "base"
            base.mkdir()
            with patch.object(mod, "DISPATCH_ROOTS_REGISTRY", reg):
                mod._register_dispatch_root(base)
                data = json.loads(reg.read_text(encoding="utf-8"))
                # 超期与坏条目被清理，只剩本次登记
                self.assertEqual([str(base.resolve())],
                                 [r["path"] for r in data["roots"]])

    def test_register_failure_does_not_break_dispatch(self):
        # 登记路径不可写时静默跳过，不影响派发
        with patch.object(mod, "DISPATCH_ROOTS_REGISTRY",
                          Path("Z:/no/such/dir-xyz/dispatch-roots.json")):
            mod._register_dispatch_root(Path(tempfile.gettempdir()))  # 不抛异常


if __name__ == "__main__":
    unittest.main()
