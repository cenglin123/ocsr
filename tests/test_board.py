import importlib.util
import json
import socket
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_board.py"
SPEC = importlib.util.spec_from_file_location("ocsr_board", BOARD_PATH)
board = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(board)


def make_dispatch(root: Path, name="ocsr_dispatch_20261107_120000_abcd1234", label="w1",
                  state="landed", session_id="ses_test123", cwd="C:/work/proj",
                  model="deepseek/deepseek-flash", exit_code=0,
                  captured_at=None, total_deadline=None):
    wd = root / name / label
    wd.mkdir(parents=True)
    state_doc = {
        "state": state, "label": label, "attempt_index": 1, "rework_index": 0,
        "renewal_count": 0,
        "launch_baseline": {"captured_at": captured_at or (time.time() - 3600)},
    }
    if total_deadline is not None:
        state_doc["total_deadline"] = total_deadline
    (wd / "worker-state.json").write_text(json.dumps(state_doc), encoding="utf-8")
    (wd / "session-binding.json").write_text(json.dumps({
        "session_id": session_id, "cwd": cwd, "pid": 1234,
    }), encoding="utf-8")
    (wd / "launcher.ps1").write_text(
        f"& opencode run $prompt --format json --standalone -m {model}\n", encoding="utf-8")
    if exit_code is not None:
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
            make_dispatch(root, state="launched", exit_code=None)  # 进程尚未退出
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("active", w["badge"])
            self.assertIsNotNone(w["duration_min"])
            self.assertGreater(w["duration_min"], 0)

    def test_nonzero_exit_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="launched", exit_code=1)
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("failed", w["badge"])

    def test_stopped_not_overridden_by_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="stopped", exit_code=1)
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("stopped", w["badge"])  # 看门狗止损语义优先于退出码

    def test_timed_out_not_overridden_by_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="timed_out", exit_code=1)
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("timeout", w["badge"])

    def test_active_over_total_deadline_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="launched", exit_code=None,
                          total_deadline=time.time() - 60)
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
            hist = [{"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                     "wall_min": 9.5, "artifact_bytes": 1234,
                     "outcome_detail": "success:completed", "read_audit": "clean"}]
            board.attach_telemetry(workers, hist)
            self.assertEqual(9.5, workers[0]["wall_min"])
            self.assertEqual(1234, workers[0]["artifact_bytes"])
            self.assertEqual("clean", workers[0]["read_audit"])

    def test_attach_picks_earliest_run_after_start(self):
        # 同 label 串行两次运行：旧 worker 目录应配启动后最早一条，而非最新一条
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now().astimezone()
            make_dispatch(root, captured_at=(now - timedelta(hours=2)).timestamp())
            workers = board.scan_workers(root, days=0)
            hist = [
                {"label": "w1", "ts": (now - timedelta(hours=1)).isoformat(), "wall_min": 5.0},
                {"label": "w1", "ts": now.isoformat(), "wall_min": 99.0},
            ]
            board.attach_telemetry(workers, hist)
            self.assertEqual(5.0, workers[0]["wall_min"])

    def test_telemetry_error_promotes_stale_to_failed(self):
        # exit_0_no_artifact：worker-state 无 failed 值域，靠遥测 outcome_detail 校正
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="launched", exit_code=0)
            workers = board.scan_workers(root, days=0)
            self.assertEqual("stale", workers[0]["badge"])
            hist = [{"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                     "outcome_detail": "error:exit_0_no_artifact", "wall_min": 3.0}]
            board.attach_telemetry(workers, hist)
            self.assertEqual("failed", workers[0]["badge"])

    def test_landed_not_overridden_by_telemetry_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="landed", exit_code=0)
            workers = board.scan_workers(root, days=0)
            hist = [{"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                     "outcome_detail": "error:exit_0_no_artifact"}]
            board.attach_telemetry(workers, hist)
            self.assertEqual("landed", workers[0]["badge"])


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

    def test_assignment_preserves_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "board-groups.json"
            extra = Path(tmp) / "elsewhere"
            extra.mkdir()
            board.mutate_roots("add", str(extra), Path(tmp), path)
            board.save_assignment("k1", "组", path)
            config = board.load_config(path)
            self.assertEqual([str(extra.resolve())], config["roots"])
            self.assertEqual({"k1": "组"}, config["assignments"])

    def test_legacy_config_without_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "board-groups.json"
            path.write_text('{"assignments": {"a": "b"}}', encoding="utf-8")
            self.assertEqual([], board.load_config(path)["roots"])
            self.assertEqual({"a": "b"}, board.load_assignments(path))


class RootsTest(unittest.TestCase):
    def test_add_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            extra = Path(tmp) / "extra"
            extra.mkdir()
            path = Path(tmp) / "cfg.json"
            roots, err = board.mutate_roots("add", str(extra), base, path)
            self.assertEqual("", err)
            self.assertEqual([str(extra.resolve())], roots)
            # 重复添加 / 不存在路径 / 默认根 / 相对路径均拒绝
            self.assertTrue(board.mutate_roots("add", str(extra), base, path)[1])
            self.assertTrue(board.mutate_roots("add", str(Path(tmp) / "nope"), base, path)[1])
            self.assertTrue(board.mutate_roots("add", str(base), base, path)[1])
            self.assertTrue(board.mutate_roots("add", "relative\\x", base, path)[1])
            # 移除默认根或不在配置中的目录均拒绝
            self.assertTrue(board.mutate_roots("remove", str(base), base, path)[1])
            roots, err = board.mutate_roots("remove", str(extra), base, path)
            self.assertEqual("", err)
            self.assertEqual([], roots)

    def test_effective_roots_skip_missing_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            extra = Path(tmp) / "extra"
            extra.mkdir()
            path = Path(tmp) / "cfg.json"
            board.mutate_roots("add", str(extra), base, path)
            board.mutate_roots("add", str(extra), base, path)  # 重复被拒绝
            cfg = board.load_config(path)
            cfg["roots"].append(str(Path(tmp) / "gone"))  # 已不存在的根被跳过
            board._write_config(cfg, path)
            roots = board.effective_roots(base, path)
            self.assertEqual([str(base.resolve()), str(extra.resolve())],
                             [str(r.resolve()) for r in roots])


class ValidationTest(unittest.TestCase):
    def test_group_payload(self):
        self.assertEqual(("k", "g"), board.validate_group_payload({"key": "k", "group": "g"}))
        self.assertEqual(("k", ""), board.validate_group_payload({"key": "k"}))
        self.assertIsNone(board.validate_group_payload({"key": 123}))
        self.assertIsNone(board.validate_group_payload({"key": "k", "group": ["x"]}))
        self.assertIsNone(board.validate_group_payload({"key": ""}))
        self.assertIsNone(board.validate_group_payload({"key": "x" * 513}))
        self.assertIsNone(board.validate_group_payload({"key": "k", "group": "g" * 121}))
        self.assertIsNone(board.validate_group_payload({"key": "k", "group": "a\x00b"}))
        self.assertIsNone(board.validate_group_payload("not-a-dict"))

    def test_host_allowed(self):
        self.assertTrue(board.host_allowed("127.0.0.1:8787", 8787))
        self.assertTrue(board.host_allowed("localhost:8787", 8787))
        self.assertTrue(board.host_allowed("[::1]:8787", 8787))
        self.assertFalse(board.host_allowed("evil.com:8787", 8787))
        self.assertFalse(board.host_allowed("127.0.0.1:9999", 8787))
        self.assertFalse(board.host_allowed("", 8787))


class PortTest(unittest.TestCase):
    @staticmethod
    def _occupied(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s

    @staticmethod
    def _free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_fresh_when_free(self):
        port, action = board.ensure_port(port=self._free_port(),
                                         prober=lambda *a: {},
                                         killer=lambda pid: None)
        self.assertEqual("fresh", action)

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
