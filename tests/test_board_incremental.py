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
# 测试环境隔离：登记根默认指向不存在路径（避免本机真实 dispatch-roots.json 串扰）
board.DISPATCH_ROOTS_REGISTRY = Path(tempfile.gettempdir()) / "ocsr-test-no-roots.json"


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
            calls = []
            real = board._scan_worker_dir_fresh

            def counted(*a, **kw):
                calls.append(1)
                return real(*a, **kw)

            with patch.object(board, "_scan_worker_dir_fresh", counted):
                board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
                board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
                self.assertEqual(1, len(calls))  # 未变：第二次命中缓存，未重读
                # 修改状态文件 → 指纹变 → 重扫
                time.sleep(0.02)
                (wd / "worker-state.json").write_text(json.dumps({
                    "state": "timed_out", "label": "w1",
                    "launch_baseline": {"captured_at": time.time() - 3600},
                }), encoding="utf-8")
                w3 = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
                self.assertEqual("timeout", w3["badge"])
                self.assertEqual(2, len(calls))

    def test_same_size_rewrite_invalidates_via_mtime(self):
        # renewed/running 同为 7 字符：同尺寸改写但 mtime 变化也必须失效
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = make_worker(root, state="running")
            board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            time.sleep(0.02)
            (wd / "worker-state.json").write_text(json.dumps({
                "state": "renewed", "label": "w1",
                "launch_baseline": {"captured_at": time.time() - 3600},
            }), encoding="utf-8")
            w = board.scan_worker_dir(wd, "ocsr_dispatch_20261108_120000_abcd1234", None)
            self.assertEqual("renewed", w["state"])

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

            class H:
                groups_path = root / "cfg.json"
                base_root = root
                cli_roots: list = []
                days = 0
                log_path = root / "log.jsonl"

            board._STATE_CACHE.update(at=0.0, payload=None)
            board.build_state(H)
            self.assertEqual(1, len(board._WORKER_CACHE))
            # 删除整个 dispatch 目录后，build_state 统一淘汰缓存
            import shutil
            shutil.rmtree(root / "ocsr_dispatch_20261108_120000_abcd1234")
            board._STATE_CACHE.update(at=0.0, payload=None)
            board.build_state(H)
            self.assertEqual(0, len(board._WORKER_CACHE))
            board._WORKER_CACHE.clear()


class IncrementalHistoryTest(unittest.TestCase):
    def setUp(self):
        board._HISTORY_CACHE.update(path=None, offset=0, records=[])

    def test_incremental_appends_only_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a","ts":"t1"}\n{"label":"b","ts":"t2"}\n', encoding="utf-8")
            batches = []
            real = board._parse_history_lines

            def counted(lines, records):
                batches.append(list(lines))
                real(lines, records)

            with patch.object(board, "_parse_history_lines", counted):
                h1 = board.read_history(log)
                self.assertEqual(["a", "b"], [r["label"] for r in h1])
                with log.open("a", encoding="utf-8") as fh:
                    fh.write('{"label":"c","ts":"t3"}\n{"label":"d","ts":"t4"}\n')
                h2 = board.read_history(log)
                self.assertEqual(["a", "b", "c", "d"], [r["label"] for r in h2])
                # 第二次只解析新增的两行（而非全量）
                self.assertEqual(2, len(batches))
                self.assertEqual(["c", "d"],
                                 [json.loads(x)["label"] for x in batches[1] if x.strip()])
                # 大小未变 → 不再解析
                board.read_history(log)
                self.assertEqual(2, len(batches))

    def test_incomplete_tail_line_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a"}\n', encoding="utf-8")
            board.read_history(log)
            offset = board._HISTORY_CACHE["offset"]
            # 写入不完整行（无换行结尾）：不推进偏移、不解析
            with log.open("a", encoding="utf-8") as fh:
                fh.write('{"label":"b')
            h = board.read_history(log)
            self.assertEqual(["a"], [r["label"] for r in h])
            self.assertEqual(offset, board._HISTORY_CACHE["offset"])
            # 补全该行 → 下次读到
            with log.open("a", encoding="utf-8") as fh:
                fh.write('"}\n')
            h2 = board.read_history(log)
            self.assertEqual(["a", "b"], [r["label"] for r in h2])

    def test_deleted_file_resets_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a"}\n', encoding="utf-8")
            board.read_history(log)
            log.unlink()
            self.assertEqual([], board.read_history(log))
            self.assertIsNone(board._HISTORY_CACHE["path"])
            self.assertEqual(0, board._HISTORY_CACHE["offset"])

    def test_replaced_file_detected_by_last_line_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "dispatch-log.jsonl"
            log.write_text('{"label":"a"}\n{"label":"b"}\n', encoding="utf-8")
            board.read_history(log)
            # 替换为更长的新文件（size > 旧 offset，简单 size 检查无法发现）
            log.write_text('{"label":"z1"}\n{"label":"z2"}\n{"label":"z3"}\n', encoding="utf-8")
            h = board.read_history(log)
            self.assertEqual(["z1", "z2", "z3"], [r["label"] for r in h])  # 全量重读而非混合

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

    def test_idle_watchdog_does_not_stop_when_active(self):
        class FakeServer:
            def __init__(self):
                self.stopped = False

            def shutdown(self):
                self.stopped = True

        server = FakeServer()
        board._LAST_ACTIVITY["at"] = time.monotonic()  # 刚刚还有页面请求
        calls = []
        with patch.object(board.time, "sleep", lambda s: (_ for _ in ()).throw(StopIteration()) if len(calls) > 1 else calls.append(1)):
            # 第一轮不超时（有活动），第二轮由测试中止循环
            try:
                board._idle_watchdog(server, timeout_sec=60)
            except StopIteration:
                pass
        self.assertFalse(server.stopped)  # 有页面在看时不退出

    def test_restart_existing_skips_on_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "board.lock"
            lock.write_text(json.dumps({"pid": 4321, "port": 8787}), encoding="utf-8")
            with patch.object(board, "LOCK_PATH", lock), \
                 patch.object(board, "verify_board_process", lambda pid: True), \
                 patch.object(board, "probe_board_identity", lambda port: {"pid": 9999}), \
                 patch.object(board, "kill_pid", lambda pid: self.fail("identity 不符不应 kill")):
                board._restart_existing_instance()

    def test_restart_existing_skips_missing_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "board.lock"
            lock.write_text(json.dumps({"pid": 4321}), encoding="utf-8")  # 缺 port
            with patch.object(board, "LOCK_PATH", lock), \
                 patch.object(board, "verify_board_process", lambda pid: True), \
                 patch.object(board, "kill_pid", lambda pid: self.fail("缺 port 不应 kill")):
                board._restart_existing_instance()

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


class MultiRootCacheTest(unittest.TestCase):
    def test_multi_root_cache_preserved_across_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_a, root_b = root / "a", root / "b"
            make_worker(root_a)
            make_worker(root_b)

            class H:
                groups_path = root / "cfg.json"
                base_root = root_a
                cli_roots = [root_b]
                days = 0
                log_path = root / "log.jsonl"

            board._STATE_CACHE.update(at=0.0, payload=None)
            board.build_state(H)
            # 淘汰在全部根扫描完成后统一执行：两个根的缓存都应保留
            self.assertEqual(2, len(board._WORKER_CACHE))
            board._STATE_CACHE.update(at=0.0, payload=None)
            board.build_state(H)
            self.assertEqual(2, len(board._WORKER_CACHE))
            board._WORKER_CACHE.clear()


class RegisteredRootsTest(unittest.TestCase):
    def test_registered_roots_merged_and_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reg = root / "dispatch-roots.json"
            extra = root / "custom-dispatch"
            extra.mkdir()
            reg.write_text(json.dumps({"roots": [
                {"path": str(extra), "last_used": datetime.now().astimezone().isoformat()},
                {"path": "C:/gone-xyz", "last_used": datetime.now().astimezone().isoformat()},
                {"path": str(extra), "last_used": (datetime.now().astimezone() - timedelta(days=40)).isoformat()},
                {"path": 123, "last_used": "bad"},
            ]}), encoding="utf-8")
            base = root / "base"
            base.mkdir()
            cfg = root / "cfg.json"
            with patch.object(board, "DISPATCH_ROOTS_REGISTRY", reg):
                roots = board.effective_roots(base, cfg)
            self.assertIn(extra, roots)
            self.assertEqual(2, len(roots))  # base + extra；不存在/超期/非法均跳过

    def test_missing_registry_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(board, "DISPATCH_ROOTS_REGISTRY", root / "none.json"):
                self.assertEqual([], board.registered_dispatch_roots())


if __name__ == "__main__":
    unittest.main()
