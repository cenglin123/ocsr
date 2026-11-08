import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

BOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_board.py"
SPEC = importlib.util.spec_from_file_location("ocsr_board_inc", BOARD_PATH)
board = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(board)


def make_worker(root: Path, label="w1", state="landed"):
    wd = root / "ocsr_dispatch_20261108_120000_abcd1234" / label
    wd.mkdir(parents=True)
    (wd / "worker-state.json").write_text(json.dumps({
        "state": state, "label": label,
        "launch_baseline": {"captured_at": time.time() - 3600},
    }), encoding="utf-8")
    (wd / "session-binding.json").write_text(
        json.dumps({"session_id": "ses_x", "cwd": "C:/p"}), encoding="utf-8")
    return wd


class IncrementalScanTest(unittest.TestCase):
    def setUp(self):
        board._WORKER_CACHE.clear()

    def test_fingerprint_reuses_and_invalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_worker(root)
            w1 = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            self.assertIn(str(wd), board._WORKER_CACHE)
            # 未变：命中缓存（同一内容）
            w2 = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            self.assertEqual(w1["label"], w2["label"])
            # 修改状态文件 → 指纹变 → 重扫出新状态
            time.sleep(0.02)
            (wd / "worker-state.json").write_text(json.dumps({
                "state": "timed_out", "label": "w1",
                "launch_baseline": {"captured_at": time.time() - 3600},
            }), encoding="utf-8")
            w3 = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            self.assertEqual("timeout", w3["badge"])

    def test_refresh_dynamic_active_duration_and_overdue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_worker(root, state="launched")
            w = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            self.assertEqual("active", w["badge"])
            self.assertIsNotNone(w["duration_min"])
            # 模拟缓存条目：已越过 total_deadline → 刷新时应转 stale
            cached = dict(w)
            cached["total_deadline"] = time.time() - 10
            board._refresh_dynamic(cached)
            self.assertEqual("stale", cached["badge"])

    def test_cache_pruned_when_dir_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_worker(root)
            board.scan_workers(root, days=0)
            self.assertEqual(1, len(board._WORKER_CACHE))
            # 删除整个 dispatch 目录后扫描集为空 → 缓存被清空
            import shutil
            shutil.rmtree(root / "ocsr_dispatch_20261108_120000_abcd1234")
            board.scan_workers(root, days=0)
            self.assertEqual(0, len(board._WORKER_CACHE))


class IncrementalHistoryTest(unittest.TestCase):
    def setUp(self):
        board._HISTORY_CACHE.update(path=None, offset=0, records=[])

    def test_incremental_appends_only_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a","ts":"t1"}\n{"label":"b","ts":"t2"}\n', encoding="utf-8")
            h1 = board.read_history(log)
            self.assertEqual(["a", "b"], [r["label"] for r in h1])
            offset = board._HISTORY_CACHE["offset"]
            # 追加两行 → 只解析新增
            with log.open("a", encoding="utf-8") as fh:
                fh.write('{"label":"c","ts":"t3"}\n{"label":"d","ts":"t4"}\n')
            h2 = board.read_history(log)
            self.assertEqual(["a", "b", "c", "d"], [r["label"] for r in h2])
            self.assertGreater(board._HISTORY_CACHE["offset"], offset)
            # 大小未变 → 直接复用
            h3 = board.read_history(log)
            self.assertEqual(["a", "b", "c", "d"], [r["label"] for r in h3])

    def test_truncated_file_rereads(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a"}\n{"label":"b"}\n', encoding="utf-8")
            board.read_history(log)
            log.write_text('{"label":"z"}\n', encoding="utf-8")  # 截断重写
            h = board.read_history(log)
            self.assertEqual(["z"], [r["label"] for r in h])


class LifecycleTest(unittest.TestCase):
    def test_idle_watchdog_shuts_down(self):
        class FakeServer:
            def __init__(self):
                self.stopped = False

            def shutdown(self):
                self.stopped = True

        server = FakeServer()
        board._LAST_ACTIVITY["at"] = time.monotonic() - 3600
        with patch.object(board.time, "sleep", lambda s: None):
            board._idle_watchdog(server, timeout_sec=60)
        self.assertTrue(server.stopped)

    def test_lock_write_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "board.lock"
            with patch.object(board, "LOCK_PATH", lock):
                board._write_lock(8787)
                data = json.loads(lock.read_text(encoding="utf-8"))
                self.assertEqual(8787, data["port"])
                board._clear_lock()
                self.assertFalse(lock.exists())

    def test_restart_existing_kills_live_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "board.lock"
            lock.write_text(json.dumps({"pid": 4321, "port": 8787}), encoding="utf-8")
            killed = []
            with patch.object(board, "LOCK_PATH", lock), \
                 patch.object(board, "verify_board_process", lambda pid: True), \
                 patch.object(board, "probe_board_identity", lambda port: {"pid": 4321}), \
                 patch.object(board, "kill_pid", killed.append), \
                 patch.object(board.time, "sleep", lambda s: None):
                board._restart_existing_instance()
            self.assertEqual([4321], killed)

    def test_restart_existing_ignores_stale_or_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "board.lock"
            lock.write_text(json.dumps({"pid": 4321, "port": 8787}), encoding="utf-8")
            with patch.object(board, "LOCK_PATH", lock), \
                 patch.object(board, "verify_board_process", lambda pid: False), \
                 patch.object(board, "kill_pid", lambda pid: self.fail("不应 kill 非面板进程")):
                board._restart_existing_instance()


if __name__ == "__main__":
    unittest.main()
