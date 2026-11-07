import importlib.util
import json
import socket
import tempfile
import unittest
from pathlib import Path

BOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_board.py"
SPEC = importlib.util.spec_from_file_location("ocsr_board", BOARD_PATH)
board = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(board)


def make_dispatch(root: Path, name="ocsr_dispatch_20261107_120000_abcd1234", label="w1",
                  state="landed", session_id="ses_test123", cwd="C:/work/proj",
                  model="deepseek/deepseek-flash", exit_code=0):
    wd = root / name / label
    wd.mkdir(parents=True)
    (wd / "worker-state.json").write_text(json.dumps({
        "state": state, "label": label, "attempt_index": 1, "rework_index": 0,
        "renewal_count": 0,
        "launch_baseline": {"captured_at": 1794050000.0},
    }), encoding="utf-8")
    (wd / "session-binding.json").write_text(json.dumps({
        "session_id": session_id, "cwd": cwd, "pid": 1234,
    }), encoding="utf-8")
    (wd / "launcher.ps1").write_text(
        f"& opencode run $prompt --format json --standalone -m {model}\n", encoding="utf-8")
    (wd / "start.marker").write_text(f"exit={exit_code}\n", encoding="utf-8")
    return wd


class ScanTest(unittest.TestCase):
    def test_scan_extracts_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root)
            workers = board.scan_workers(root, days=0)
            self.assertEqual(1, len(workers))
            w = workers[0]
            self.assertEqual("w1", w["label"])
            self.assertEqual("landed", w["badge"])
            self.assertEqual("ses_test123", w["session_id"])
            self.assertEqual("C:/work/proj", w["cwd"])
            self.assertEqual("deepseek/deepseek-flash", w["model"])
            self.assertEqual(0, w["exit_code"])
            self.assertEqual("ocsr_dispatch_20261107_120000_abcd1234/w1", w["key"])
            self.assertEqual("C:/work/proj", board.default_group(w))

    def test_active_duration_computed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_dispatch(root, state="launched")
            (wd / "start.marker").unlink()  # 进程尚未退出
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("active", w["badge"])
            self.assertIsNotNone(w["duration_min"])
            self.assertGreater(w["duration_min"], 0)

    def test_nonzero_exit_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="timed_out", exit_code=1)
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("failed", w["badge"])

    def test_active_over_total_deadline_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_dispatch(root, state="launched", exit_code=None)
            (wd / "start.marker").unlink()
            state = json.loads((wd / "worker-state.json").read_text(encoding="utf-8"))
            state["total_deadline"] = 1794050000.0 + 60  # 早已过期
            (wd / "worker-state.json").write_text(json.dumps(state), encoding="utf-8")
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("stale", w["badge"])
            self.assertIn("超期", w["badge_text"])

    def test_active_with_clean_exit_is_stale_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="launched", exit_code=0)
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("stale", w["badge"])
            self.assertIn("未验收", w["badge_text"])

    def test_partial_files_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_dispatch(root)
            (wd / "worker-state.json").write_text('{"state": "laun', encoding="utf-8")
            (wd / "session-binding.json").write_text("", encoding="utf-8")
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("w1", w["label"])  # 目录名兜底
            self.assertEqual("", w["session_id"])
            self.assertEqual("（未知工作目录）", board.default_group(w))

    def test_days_window_and_name_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, name="ocsr_dispatch_20200101_000000_aaaaaaaa", label="old")
            make_dispatch(root, name="ocsr_dispatch_20990101_000000_bbbbbbbb", label="new")
            (root / "not-a-dispatch").mkdir()
            self.assertEqual(1, len(board.scan_workers(root, days=7)))
            self.assertEqual(2, len(board.scan_workers(root, days=0)))


class HistoryTest(unittest.TestCase):
    def test_tail_and_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            rows = [{"ts": f"2026-11-07T1{i}:00:00+08:00", "label": f"l{i}",
                     "outcome": "success", "wall_min": i} for i in range(5)]
            log.write_text("\n".join(json.dumps(r) for r in rows)
                           + '\n{"label": "broken"', encoding="utf-8")
            hist = board.read_history(log, limit=3)
            self.assertEqual(3, len(hist))
            self.assertEqual("l4", hist[-1]["label"])

    def test_attach_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root)
            workers = board.scan_workers(root, days=0)
            hist = [{"label": "w1", "ts": "2026-11-07T13:00:00+08:00",
                     "wall_min": 9.5, "artifact_bytes": 1234,
                     "outcome_detail": "success:completed", "read_audit": "clean"}]
            board.attach_telemetry(workers, hist)
            self.assertEqual(9.5, workers[0]["wall_min"])
            self.assertEqual(1234, workers[0]["artifact_bytes"])
            self.assertEqual("clean", workers[0]["read_audit"])


class GroupsTest(unittest.TestCase):
    def test_save_and_clear_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "board-groups.json"
            board.save_assignment("k1", "我的组", path)
            board.save_assignment("k2", "其他", path)
            self.assertEqual({"k1": "我的组", "k2": "其他"}, board.load_assignments(path))
            board.save_assignment("k1", "", path)
            self.assertEqual({"k2": "其他"}, board.load_assignments(path))
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", raw)


class PortTest(unittest.TestCase):
    @staticmethod
    def _occupied(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s

    def test_fresh_when_free(self):
        port, action = board.ensure_port(port=18787,
                                         prober=lambda *a: {},
                                         killer=lambda pid: None)
        self.assertEqual("fresh", action)
        self.assertEqual(18787, port)

    def test_restart_kills_old_board_and_reuses_port(self):
        holder = self._occupied(0)
        port = holder.getsockname()[1]
        killed = []
        try:
            got, action = board.ensure_port(
                port=port,
                prober=lambda p, h: {"app": "ocsr_board", "pid": 4321},
                killer=killed.append,
                bind=lambda h, p: (_ for _ in ()).throw(OSError())
                if p == port and not killed else None,
                sleep=lambda s: None,
            )
        finally:
            holder.close()
        self.assertEqual([4321], killed)
        self.assertEqual((port, "restarted"), (got, action))

    def test_moved_when_foreign_process_occupies(self):
        holder = self._occupied(0)
        port = holder.getsockname()[1]
        try:
            got, action = board.ensure_port(
                port=port,
                prober=lambda p, h: {},
                killer=lambda pid: self.fail("不应 kill 非面板进程"),
                sleep=lambda s: None,
            )
        finally:
            holder.close()
        self.assertEqual("moved", action)
        self.assertGreater(got, port)


if __name__ == "__main__":
    unittest.main()
