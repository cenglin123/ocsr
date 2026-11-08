import importlib.util
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_board.py"
SPEC = importlib.util.spec_from_file_location("ocsr_board", BOARD_PATH)
board = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(board)
# 测试环境隔离：登记根默认指向不存在路径（避免本机真实 dispatch-roots.json 串扰）
board.DISPATCH_ROOTS_REGISTRY = Path(tempfile.gettempdir()) / "ocsr-test-no-roots.json"


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
            self.assertTrue(w["key"].endswith("/ocsr_dispatch_20261107_120000_abcd1234/w1"),
                            f"key 应含扫描根前缀与 dispatch/label 后缀: {w['key']}")
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

    def test_keys_include_root_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_a, root_b = Path(tmp) / "a", Path(tmp) / "b"
            make_dispatch(root_a)
            make_dispatch(root_b)
            keys = {w["key"] for root in (root_a, root_b)
                    for w in board.scan_workers(root, days=0)}
            self.assertEqual(2, len(keys))  # 同名 dispatch/label 在不同根下不碰撞

    def test_model_id_with_colon_tag(self):
        # 模型 ID 含 :/@ 时不得被截断（与 dispatcher 的取参一致）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, model="zhipuai/glm-5.3-flash:nightly")
            w = board.scan_workers(root, days=0)[0]
            self.assertEqual("zhipuai/glm-5.3-flash:nightly", w["model"])


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

    def test_single_run_reported_then_final_prefers_final(self):
        # 一次运行写多条遥测：reported 中间行（log 大小/检查时刻时长）不得当最终值
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now().astimezone()
            make_dispatch(root, captured_at=(now - timedelta(hours=1)).timestamp())
            workers = board.scan_workers(root, days=0)
            hist = [
                {"label": "w1", "ts": (now - timedelta(minutes=30)).isoformat(),
                 "outcome_detail": "reported:running", "wall_min": 30.0,
                 "artifact_bytes": 999999},  # 中间行：log 大小而非产物
                {"label": "w1", "ts": (now - timedelta(minutes=10)).isoformat(),
                 "outcome_detail": "success:completed", "wall_min": 50.0,
                 "artifact_bytes": 4321, "read_audit": "clean"},
            ]
            board.attach_telemetry(workers, hist)
            self.assertEqual(50.0, workers[0]["wall_min"])
            self.assertEqual(4321, workers[0]["artifact_bytes"])
            self.assertEqual("clean", workers[0]["read_audit"])

    def test_only_reported_rows_do_not_bind(self):
        # 真正的中间行（reported:running）不是最终状态：不绑定 wall/artifact
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now().astimezone()
            make_dispatch(root, captured_at=(now - timedelta(hours=1)).timestamp())
            workers = board.scan_workers(root, days=0)
            hist = [{"label": "w1", "ts": now.isoformat(),
                     "outcome_detail": "reported:running", "wall_min": 30.0,
                     "artifact_bytes": 999999}]
            board.attach_telemetry(workers, hist)
            self.assertIsNone(workers[0]["wall_min"])
            self.assertIsNone(workers[0]["artifact_bytes"])
            self.assertEqual("", workers[0]["outcome_detail"])

    def test_handoff_required_terminal_row_binds(self):
        # BR-B1：reported:handoff_required 是总期限交回的终态行（非中间行），
        # 应参与绑定且 outcome_detail 原样保留；徽章仍以 worker-state 终态为准
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now().astimezone()
            make_dispatch(root, state="timed_out", exit_code=0,
                          captured_at=(now - timedelta(hours=1)).timestamp())
            workers = board.scan_workers(root, days=0)
            self.assertEqual("timeout", workers[0]["badge"])
            hist = [{"label": "w1", "ts": now.isoformat(),
                     "outcome_detail": "reported:handoff_required",
                     "wall_min": 42.0, "artifact_bytes": 87654}]
            board.attach_telemetry(workers, hist)
            self.assertEqual(42.0, workers[0]["wall_min"])
            self.assertEqual(87654, workers[0]["artifact_bytes"])
            self.assertEqual("reported:handoff_required",
                             workers[0]["outcome_detail"])
            self.assertEqual("timeout", workers[0]["badge"])

    def test_malformed_telemetry_fields_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root)
            workers = board.scan_workers(root, days=0)
            hist = [
                {"label": "w1", "ts": 12345, "outcome_detail": None},  # 非字符串 ts/od
                {"label": "w1", "ts": "not-a-date", "outcome_detail": {"x": 1}},
                {"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                 "outcome_detail": "success:completed", "wall_min": 7.0},
            ]
            board.attach_telemetry(workers, hist)  # 不应抛 TypeError/AttributeError
            self.assertEqual(7.0, workers[0]["wall_min"])

    def test_non_string_label_tolerated_in_state_build(self):
        # L2：遥测行 "label": [] 等非字符串值在 read_history 被过滤、worker-state
        # label 异常时以目录名兜底，attach_telemetry/build_state 不抛 TypeError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class H:
                groups_path = root / "cfg.json"
                base_root = root / "base"
                cli_roots: list = []
                days = 0
                log_path = root / "log.jsonl"

            H.base_root.mkdir()
            wd = make_dispatch(H.base_root)
            state_doc = json.loads((wd / "worker-state.json").read_text(encoding="utf-8"))
            state_doc["label"] = ["bad"]  # 非字符串 label：扫描侧应回退目录名
            (wd / "worker-state.json").write_text(json.dumps(state_doc), encoding="utf-8")
            now = datetime.now().astimezone().isoformat()
            rows = [
                {"label": [], "ts": now, "outcome_detail": "success:completed", "wall_min": 1.0},
                {"label": "w1", "ts": now, "outcome_detail": "success:completed", "wall_min": 7.0},
            ]
            H.log_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                                  encoding="utf-8")
            payload = board.build_state(H, force=True)
            self.assertEqual(1, len(payload["workers"]))
            w = payload["workers"][0]
            self.assertEqual("w1", w["label"])  # 目录名兜底
            self.assertEqual(7.0, w["wall_min"])  # 合法行正常绑定，[] 行被过滤

    def test_landed_not_overridden_by_telemetry_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="landed", exit_code=0)
            workers = board.scan_workers(root, days=0)
            hist = [{"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                     "outcome_detail": "error:exit_0_no_artifact"}]
            board.attach_telemetry(workers, hist)
            self.assertEqual("landed", workers[0]["badge"])

    def test_telemetry_upper_bound_next_run(self):
        # B1：旧 worker 区间内无终态行时，同 label 下一次运行的 error:* 行
        # 不得越界绑定——徽章不被改写为 failed、wall_min 不被覆盖
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now().astimezone()
            make_dispatch(root, name="ocsr_dispatch_20261107_100000_aaaaaaaa",
                          state="launched", exit_code=0,
                          captured_at=(now - timedelta(hours=2)).timestamp())
            make_dispatch(root, name="ocsr_dispatch_20261107_120000_bbbbbbbb",
                          state="launched", exit_code=None,
                          captured_at=(now - timedelta(minutes=30)).timestamp())
            workers = board.scan_workers(root, days=0)
            workers.sort(key=lambda w: w["started_at"])
            old, new = workers
            hist = [{"label": "w1", "ts": (now - timedelta(minutes=10)).isoformat(),
                     "outcome_detail": "error:exit_0_no_artifact", "wall_min": 3.0}]
            board.attach_telemetry(workers, hist)
            self.assertEqual("stale", old["badge"])
            self.assertIsNone(old["wall_min"])
            self.assertEqual("", old["outcome_detail"])
            self.assertEqual("failed", new["badge"])  # 新运行自身区间内正常绑定
            self.assertEqual(3.0, new["wall_min"])

    def test_telemetry_stall_timeout_promotes_to_timeout(self):
        # S3：dispatcher 超时遥测是 stall:watchdog-timeout / killed:harness-timeout，
        # active/stale worker 据此校正为 timeout 徽章
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dispatch(root, state="launched", exit_code=0)
            workers = board.scan_workers(root, days=0)
            self.assertEqual("stale", workers[0]["badge"])
            hist = [{"label": "w1", "ts": datetime.now().astimezone().isoformat(),
                     "outcome_detail": "stall:watchdog-timeout", "wall_min": 3.0}]
            board.attach_telemetry(workers, hist)
            self.assertEqual("timeout", workers[0]["badge"])


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
                verifier=lambda pid: True,
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


class HttpApiTest(unittest.TestCase):
    """真实 BoardServer + urllib 的端点契约测试（独立配置与根，不碰本机数据）。

    全类共享一个 server：Windows 下逐用例起停 server 存在间歇性连接中断
    （WinError 10053）竞态窗口；每个用例仅重置配置文件与扫描缓存。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)

        class Handler(board.BoardHandler):
            pass

        Handler.groups_path = root / "cfg.json"
        Handler.base_root = root / "base"
        Handler.base_root.mkdir()
        Handler.cli_roots = []
        Handler.days = 0
        Handler.log_path = root / "dispatch-log.jsonl"
        cls.server = board.BoardServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        board._STATE_CACHE.update(at=0.0, payload=None)  # 隔离其他测试类遗留的扫描缓存
        # 每个用例独立配置文件，避免 roots/assignments 串扰
        self.server.RequestHandlerClass.groups_path.unlink(missing_ok=True)

    def _post(self, path, payload, content_type="application/json", host=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        req.add_header("Content-Type", content_type)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_roots_content_type_rejected(self):
        status, _ = self._post("/api/roots", {"action": "add", "path": "x"}, content_type="text/plain")
        self.assertEqual(415, status)

    def test_foreign_host_forbidden(self):
        status, _ = self._post("/api/roots", {"action": "add", "path": "x"}, host="evil.com")
        self.assertEqual(403, status)

    def test_unknown_action_400(self):
        status, _ = self._post("/api/roots", {"action": "purge", "path": "x"})
        self.assertEqual(400, status)

    def test_add_remove_roundtrip_and_missing_root_visible(self):
        extra = Path(self.tmp.name) / "extra"
        extra.mkdir()
        status, body = self._post("/api/roots", {"action": "add", "path": str(extra)})
        self.assertEqual(200, status)
        self.assertEqual([str(extra.resolve())], body["roots"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state?fresh=1", timeout=10) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(state["custom_roots"][0]["exists"])
        # 目录失效后仍列出（exists=False）且可移除，不留幽灵配置
        extra.rmdir()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state?fresh=1", timeout=10) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        self.assertFalse(state["custom_roots"][0]["exists"])
        status, body = self._post("/api/roots", {"action": "remove", "path": str(extra.resolve())})
        self.assertEqual(200, status)
        self.assertEqual([], body["roots"])

    def test_negative_content_length_400(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(b"POST /api/groups HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Content-Type: application/json\r\nContent-Length: -5\r\n"
                      b"Connection: close\r\n\r\n")
            chunks = []
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
        self.assertIn(b"400", b"".join(chunks).split(b"\r\n")[0])


class StateCacheTest(unittest.TestCase):
    def setUp(self):
        board._STATE_CACHE.update(at=0.0, payload=None)

    def tearDown(self):
        board._STATE_CACHE.update(at=0.0, payload=None)

    def test_cache_hit_and_invalidation_on_config_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class H:  # 轻量 handler 替身（仅提供 build_state 所需属性）
                groups_path = root / "cfg.json"
                base_root = root / "base"
                cli_roots: list = []
                days = 0
                log_path = root / "log.jsonl"

            H.base_root.mkdir()
            p1 = board.build_state(H)
            self.assertIs(p1, board.build_state(H))  # TTL 内命中缓存，未重扫
            self.assertIsNot(p1, board.build_state(H, force=True))  # fresh 强制重扫
            board.save_assignment("k", "g", H.groups_path)
            self.assertEqual(0.0, board._STATE_CACHE["at"])  # 配置写使缓存失效
            p3 = board.build_state(H)
            self.assertEqual({"k": "g"}, p3["assignments"])


    def test_generation_retry_when_config_written_during_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class H:
                groups_path = root / "cfg.json"
                base_root = root / "base"
                cli_roots: list = []
                days = 0
                log_path = root / "log.jsonl"

            H.base_root.mkdir()
            calls = {"n": 0}
            real_scan = board.scan_workers

            def scan_and_write(r, days):
                calls["n"] += 1
                if calls["n"] == 1:
                    # 模拟扫描期间发生配置写：gen 递增，应触发重扫而非旧值回填
                    board.save_assignment("late", "组", H.groups_path)
                return []

            board.scan_workers = scan_and_write
            try:
                payload = board.build_state(H)
            finally:
                board.scan_workers = real_scan
            self.assertGreaterEqual(calls["n"], 2)
            self.assertEqual({"late": "组"}, payload["assignments"])


class ShutdownTest(unittest.TestCase):
    def test_shutdown_stops_server(self):
        class Handler(board.BoardHandler):
            pass

        Handler.groups_path = Path(tempfile.gettempdir()) / "ocsr-test-shutdown-cfg.json"
        Handler.base_root = Path(tempfile.gettempdir()) / "ocsr-test-shutdown-base"
        Handler.base_root.mkdir(exist_ok=True)
        Handler.cli_roots = []
        Handler.log_path = Handler.base_root / "no-log.jsonl"
        server = board.BoardServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown",
                                     data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(200, resp.status)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())  # serve_forever 已退出
        server.server_close()
        Handler.groups_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
