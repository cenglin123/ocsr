# 当前状态与交接

更新时间：2026-08-18

## 当前状态

- 派发驱动器为默认派发路径：`scripts/ocsr_dispatch.py`（dispatch/run/selftest/telemetry/summary/monitor/verify-ownership/preflight）。
- **模型白名单**（2026-08-18 起三个 qualified ID）：`deepseek/deepseek-v4-flash`、`xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro`。
- watcher 失败语义分层：exit=0 期望产物缺失时区分「0 产物」与「命名与 pattern 不符」（`_detect_name_mismatch`）。
- 模型调用 tripwire：测试默认 `OCSR_DISABLE_MODEL_CALLS=1`。
- `verify_ocsr_skill.py` / `agent_links.py check` / `audit.py check` / `pytest tests/ -q` 全部通过（258 passed）。

## 接手顺序

1. `git status --short`，保护未提交内容。
2. 读 `AGENTS.md` 与本文件；改 SKILL.md 前先读 `docs/overview.md` 与 `docs/pitfalls.md`。
3. 开发沿革见根目录 `DEVELOPMENT.md`（脱敏摘要）；早期私有开发历史不入本仓。
