# 当前状态与交接

更新时间：2026-08-25

## 当前状态

- dsh 适配层计划已收敛（可执行），Phase 1 已落地：新增根级 `package.json`（`@ocsr/dsh-ocsr`）、`cordis.patch.yml`（插入 `ocsr-skill` 行）、`lib/index.js`（host 平面 `skills` runtime provider，provider 名 `ocsr`、插件名 `ocsr-skill`）、`refs/dsh-integration.md`（安装/配置/边界/版本锁定）。`dsh plugin --profile <p> add .` 后可由 dsh 技能系统发现 `ocsr` 技能，`resourceBase` 为 `{ kind: 'directory', path: <包根目录> }`。一等派发工具（`ocsr_dispatch`）已实现（Phase 2）：`lib/tool-ocsr.js`（`inject=['tools']`，host 平面 `tools` registry）+ `cordis.patch.yml` 的 `ocsr-tools` 行；工具为薄壳，把 schema 参数映射为 `scripts/ocsr_dispatch.py dispatch`，编排语义仍只在 Python。Phase 3（`ctx.jobs` 深度原生）待办。
- 派发驱动器为默认派发路径：`scripts/ocsr_dispatch.py`（dispatch/run/selftest/telemetry/summary/monitor/verify-ownership/preflight）。
- **模型白名单**：由用户可编辑的 `config/allowed-models.json` 加载；仓库默认仅 `xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro`。配置必须是非空、无重复的 qualified ID JSON 数组，错误时命令启动 fail-closed。
- watcher 失败语义分层：exit=0 期望产物缺失时区分「0 产物」与「命名与 pattern 不符」（`_detect_name_mismatch`）。
- 模型调用 tripwire：测试默认 `OCSR_DISABLE_MODEL_CALLS=1`。
- 2026-08-19 复验：`verify_ocsr_skill.py`、`agent_links.py check`、`audit.py check` 与 `git diff --check` 通过；`pytest tests/ -q` 为 257 passed、1 failed。失败项 `TestPidCaptureAndKill.test_kill_actually_terminates_process` 的 `taskkill` 返回 `Access denied`，发生在测试启动的 PowerShell 子进程，未改动源码或测试。
- OCSR 渐进式披露重构已实施，并经最终独立终验批准：`SKILL.md` 仅保留默认单 worker 闭环与全局不变量；按能力条件加载各 `refs/`。fresh 对抗评审细则唯一收敛在 `refs/failure-modes.md`；派发失败切换、静默停滞与终止细则收敛在 `refs/dispatch-patterns.md`；`run --spec` 仅让“已提取但未命中具名 route”的值通过 `"*"` 暂停，其他契约失败 fail-closed。2026-08-19 已清除活动源、文档与测试中指向旧 `SKILL.md` 章节编号的导航，改为具名入口或直接专题链接；定向 `run-spec`/派发回归为 231 passed。

## 接手顺序

1. `git status --short`，保护未提交内容。
2. 读 `AGENTS.md` 与本文件；改 SKILL.md 前先读 `docs/overview.md` 与 `docs/pitfalls.md`。
3. 开发沿革见根目录 `DEVELOPMENT.md`（脱敏摘要）；早期私有开发历史不入本仓。
