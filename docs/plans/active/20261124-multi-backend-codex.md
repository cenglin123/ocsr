# 计划：OCSR 抽象执行后端以兼容 OpenAI Codex CLI

- **状态**：active（v3 已通过 Codex 独立三轮只读对抗审计 `APPROVE`）
- **目标仓库**：`<user-home>/Documents/GitHub/ocsr`
- **定位**：将 OCSR 从单一 `opencode run` 执行后端演进为「统一看护 / 状态机 / 记账 / 验收底座 + 可插拔执行后端（`BackendAdapter`）」，支持 `opencode`（默认，零破坏兼容）与 `codex`（OpenAI Codex CLI `codex exec`）。
- **前置铁律**：
  1. 严格遵守 `docs/overview.md` 核心定位：仍是执行后端，不是工作流引擎，不引入常驻 daemon。
  2. 执行两层实证门禁：先通过 **Phase 0（编码前可行性门禁）** 用独立最小探针验证 Windows/CLI 前提（仅证明可行性），再进入 Phase 1/2 编码；编码完成后必须通过 **实现后启用门禁（Activation Gate）** 经最终通用 launcher 与真实入口复验，未通过前保持对应能力禁用，不以探针证据冒充最终集成验收。
  3. 本计划触及 `SKILL.md` 与 `docs/overview.md` 治理边界（引入 `--sandbox` 明确触碰「不提供文件系统或网络安全隔离」非目标条款），修改治理文档前**强制走 `AGENTS.md:30` 规定的独立跨 family 治理审查**，不得省略或降级。

---

## 一、背景与动机

1. OCSR 80% 以上的工程价值沉淀在外围底座：看门狗与不可变总期限、`KILL_ON_JOB_CLOSE` 后代防残留、UTF-8 编码容错、单向状态机（`worker-state.json`）、返工/恢复双计数（`--resume-from`）、`dispatch-log` 遥测、Board 网页面板，以及与 `converge` 预算门/账本的对齐契约。
2. 相比 `opencode run`，本地 `codex exec` 具备进程级 `--sandbox`（`read-only` / `workspace-write`）、`--output-last-message` 最终回复落盘与 `-c model_reasoning_effort=...` 控制，可补齐“需要进程级权限策略控制”的派发场景。
3. 在 OCSR 内抽离薄 `BackendAdapter` 接入 `codex`，可完整复用看门狗、Job 生命周期、状态机、返工双计数与面板，避免另起独立驱动脚本造成维护分裂。

---

## 二、核心架构与契约设计（先定合同，后写代码）

### 2.1 独立身份与白名单合同（采纳修订版 2B）

- **三元身份分离**：`backend`（执行 CLI：`opencode` | `codex`）、`provider`（模型提供方，如 `openai` / `deepseek` / `xiaomi`）、`model`（模型标识）与 `family`（模型族，用于跨 family 评审判定）严格正交。
  - **禁止**把 `codex` 塞进 `provider` 槽位；**禁止**从模型名前缀隐式推断 `backend`。
  - 未显式指定 `backend` 时，默认 `opencode`；传入未知 `backend` 在启动前 fail-closed（exit `2`）。
- **按 backend 分离唯一权威白名单**（每个 backend 各自有且仅有一份权威文件，禁止合并为无 backend 信息的集合）：
  - **`opencode` 后端**：保持 `~/.ocsr/allowed-models.json`（`OCSR_ALLOWED_MODELS_PATH` 覆盖）与 `SKILL.md:22` 从 `opencode models --verbose` 原样复制 `provider/model` 的规则零变更；旧 `ALLOWED_MODELS` 常量与默认 `selftest` 首项**仅代表 `opencode`**。
  - **`codex` 后端**：独立白名单 `~/.ocsr/allowed-codex-models.json`（`OCSR_CODEX_ALLOWED_MODELS_PATH` 覆盖），缺失或为空即未配置 fail-closed（首次使用前向用户展示本机确认结果并征求选定清单）。每条为结构化对象：
    ```json
    {
      "model": "gpt-6.1-sol",
      "provider": "openai",
      "family": "gpt",
      "reasoning_efforts": ["low", "medium", "high"]
    }
    ```
  - **Reasoning 能力白名单**：`--reasoning-effort` 仅接受目标模型在 `allowed-codex-models.json` 的 `reasoning_efforts` 中经实测确认的档位；未声明或不在列表内的组合启动前 fail-closed 拒绝。持久化与遥测中区分 `reasoning_effort_requested` 与实际可确认字段。
- **跨 family 独立性**：`family` 独立于 `backend`。同一模型或同一 `family` 分别经 `opencode` 与 `codex` 两个 CLI 派发，仍属同一 `family`，不得计为跨 family 独立评审。

### 2.2 统一执行配置与三入口支持矩阵（CLI / `run --spec` / `dsh`）

所有入口必须先构造并校验同一个 `ExecutionConfig` 结构（`backend`, `cli_model`, `provider`, `family`, `work_dir`, `sandbox`, `reasoning_effort`, `artifact_mode`），再进入 `_dispatch_batch`；不允许任何入口绕过校验或静默忽略参数：

| 入口 | 代码位置 | Phase 2 支持合同 | 非法/不支持组合处置 |
|---|---|---|---|
| **CLI** | `scripts/ocsr_dispatch.py` (`cmd_dispatch` / `cmd_preflight` / `cmd_selftest`) | 全量支持 `--backend`、`--sandbox`、`--reasoning-effort`、`--artifact-mode` | `opencode` 传 `--sandbox` / `--reasoning-effort` / `capture-reply` → 启动前 fail-closed（exit `2`） |
| **`run --spec`** | `scripts/ocsr_run_spec.py:276–306` + `scripts/ocsr_dispatch.py:3510–3535` | `dispatch` 步骤 schema 正式新增并强校验可选字段 `backend`、`sandbox`、`reasoning_effort`、`artifact_mode`，并在进程内 `_dispatch_batch` 完整透传 | schema 校验阶段 fail-closed；绝不出现“校验通过、执行忽略”；保持 `run --spec` 独立退出码（含暂停 `10`） |
| **`dsh` 适配层** | `lib/tool-ocsr.js:111–134` + `lib/model-gate.js:76–91` | **两步落地**：Phase 2A 在 `tool-ocsr.js` 显式校验并拒绝非 `opencode` backend（不静默降回 `opencode`）；Phase 2B 升级 `model-gate.js` 按 `backend` 选对应基础白名单并透传 `backend`/`sandbox`/`reasoning_effort`/`artifact_mode` | 未知或未启用的 backend 立即 fail-closed；适配层回归必须设 `OCSR_REQUIRE_ADAPTER_TESTS=1` 并核对实际执行用例数与 `0 skipped` |

### 2.3 两种产物交付与验收模式

1. **`worker-write`**（`opencode` 唯一模式；`codex --sandbox workspace-write` 默认模式）：
   - 由 worker 自行写入指定的目标绝对路径；验收维持 OCSR 现行规则（文件存在 + 非空 + 数量一致 + 抽样核对）。
   - 当 `codex --sandbox workspace-write` 且输出路径位于 `work_dir` 之外时：启动前校验——除非显式提供可验证的授权策略或将输出路径置于 `work_dir` 内，否则 fail-closed 拒绝。
2. **`capture-reply`**（`codex --sandbox read-only` 强制默认模式；`workspace-write` 可显式选用）：
   - 由 `codex exec --output-last-message <staging_tmp>` 写出最终回复到派发目录下的暂存文件；通用 launcher 在确认 `codex` 退出码为 `0` 且暂存文件非空后，以 UTF-8 无 BOM 原子落盘到目标绝对路径。
   - 失败语义：退出码非 `0` 或捕获为空/仅空白时，不写入/不覆盖目标产物，退出码按既定契约归为确定性失败（`2`）。
   - 验收约束：上层仍须对落盘文件执行存在性、非空与内容抽样核对。

### 2.4 通用 Launcher 与薄 `BackendAdapter` 边界

保留单一通用 launcher 骨架统一负责：`KILL_ON_JOB_CLOSE` Job 绑定（`scripts/ocsr_launcher_job.cs`）、`process-identity.json`、工作目录与 MSYS `$env:PWD` 归一化、`stdout`/`stderr` 重定向、原子产物落盘（`capture-reply`）、以及真实退出码写入 `start.marker`。

`BackendAdapter` 仅封装与具体 CLI 强耦合的 **6 个薄职责**：
1. `validate_config(cfg, allowed_models)`
2. `build_command_fragment(cfg, resume_ctx)`
3. `build_stream_binding_fragment()`
4. `parse_events(log_file)`（纳入 `_read_session_binding` 与 `diagnose` 读取链；无法观测时诚实返回 `unknown`）
5. `build_resume_argv(meta, session_id)`
6. `run_preflight(cfg)`

### 2.5 版本化持久化元数据与续接安全合同

- **新增 `dispatch-meta.json`（`schema_version: 1`）**：记录 `schema_version`、`backend`、`cli_model`、`provider`、`family`、`work_dir`、`sandbox_requested`、`sandbox_effective`、`reasoning_effort_requested`、`artifact_mode`、`config_source`。
- **向下兼容与面板同步**：`_read_dispatch_model` 与 `scripts/ocsr_board.py` 优先读取 `dispatch-meta.json` 展示 `backend` 与 `cli_model`，旧目录回退 `-m` 正则；`ocsr_board.py` 将 `dispatch-meta.json` 纳入目录指纹缓存失效键。
- **续接安全合同**：`--resume-from` 恢复前强制核对 `dispatch-meta.json` 的 `backend`、`cli_model`、`work_dir`、`sandbox_requested`、`artifact_mode` 一致；严禁静默扩权；若 Phase 0 无法证明 `codex exec resume <session_id>` 能严格保持原 `sandbox` 与 `cwd`，则在代码层对无法保证策略复原的组合强制禁用 `--resume-from`（fail-closed）；严禁使用 `resume --last`。

---

## 三、分阶段实施路线图

将全部 10 项实证要求拆分为两层，消除“编码前要求验收未实现系统”的循环依赖；凡涉及真实模型调用的项，执行前先披露模型与调用上限并留存原始日志与退出码证据。

### Phase 0：编码前可行性门禁（已完成 — 2026-11-24 Windows 实证，共 3 次真实 `codex exec` 调用）

- **实测环境**：`codex-cli 0.171.0`（模型 `gpt-6.1-sol`）、`node v25.2.0`、Windows。
- **探针 1（非 Git 中文空格路径 + UTF-8 无 BOM stdin + `--sandbox read-only` + `--json` + `--output-last-message`）**：`rc=0`，OS 级拦截写入（`Access ... is denied`，`should_fail.txt` 不存在），`--json` 首行吐出 `{"type":"thread.started","thread_id":"..."}`，中文回复无损落盘。
- **探针 2（精确 `resume <thread_id>` + 前置 `--sandbox read-only` + 跨 `cwd`）**：`rc=0`，前置 `--sandbox read-only` 在 `resume` 中真实拦截写入；`resume` 使用调用时的当前工作目录（通用 launcher 通过 `Set-Location` 锁回原 `work_dir`），事件流输出同一 `thread_id`。
- **探针 3（`--sandbox workspace-write` 目录边界）**：`rc=0`，目录内 `inside.txt` 写入成功，但父目录 `%TEMP%\...\outside.txt` **亦写入成功**（Windows `workspace-write` 允许写 `%TEMP%` 等可写路径）。**诚实边界结论**：Windows 下 `read-only` 具备 OS 级写拦截，`workspace-write` 不构成对 `%TEMP%` 等路径的绝对防越界隔离，Phase 3 文档须如实声明。

### Phase 2.5：实现后启用门禁（已完成核心实证 — 2026-11-24，1 次真实端到端调用）

- `dispatch --backend codex --sandbox read-only --watch`：0.3min 正常落盘（33B UTF-8 中文），`E2E_RC=0`，`session-id` 输出唯一 `thread_id`，`diagnose` 返回 `backend: "codex"`, `log.status: "ok"`, `last_phase: "completed"`, `native_session.status: "not_applicable_for_codex"`。
- 离线回归：511 passed（500 基线 + 11 新增契约测试）、`verify_ocsr_skill.py` 13/13、`audit.py check` 全绿。

### Phase 1–3 路线图（基线设计）

1. **固定 CLI 与配置基线**
2. **输入链可行性探针**（UTF-8 无 BOM stdin、中文、引号、长 prompt、CRLF、空输入）
3. **非 Git 与路径场景探针**（TEMP 目录、`--skip-git-repo-check`、空格/中文路径、MSYS `PWD`）
4. **权限与进程树可行性探针**（`read-only` 拦写、`workspace-write` 拦目录外写入、Job 关闭零残留）
5. **事件流与会话边界样本**（`codex exec --json` 真实字段 + 5 种边界样本）
6. **并发与精确恢复前提**（≥2 并发 session 交叉按精确 ID 恢复，验证 `cwd` 与 `read-only` 策略保持）

### Phase 1：抽离薄 `BackendAdapter` 接口（纯重构，零行为变更）

- 抽离 `BackendAdapter` 与通用 launcher 骨架，新增 `dispatch-meta.json` 写出与向下兼容读取。
- 验收：全量回归（500+ 基线）+ `verify_ocsr_skill.py` + `audit.py` + `dsh` 适配层测试（`OCSR_REQUIRE_ADAPTER_TESTS=1` 且 `0 skipped`）全绿。

### Phase 2：实现 `codex` 后端适配器与离线契约测试

- 落地 `CodexBackendAdapter`、`~/.ocsr/allowed-codex-models.json`、`ExecutionConfig` 三入口接线、`capture-reply` 原子落盘、`parse_events` 交叉绑定、`diagnose` 与 `ocsr_board.py` 适配、确定性离线契约测试。

### Phase 2.5：实现后启用门禁（Activation Gate）

7. **最终 launcher 输入链与完整生命周期复验**（含 `start.marker` 双写竞态单列验证）
8. **产物与权限矩阵复验**（`read-only` + `capture-reply`、`workspace-write` 内/外路径、路径碰撞、空产物）
9. **退出码矩阵复验**（`dispatch` `3 > 1 > 2 > 0` 与 `run --spec` 暂停 `10`）
10. **入口集成矩阵复验**（`run --spec` 与 `dsh` 合成事件 + mock CLI，`0 skipped`）

### Phase 3：外部 `converge` 适配层门禁与治理文档同步

1. **外部 `converge` 适配层独立启用门禁**：外部适配层未通过前，`converge` 流程保持禁止选择 `codex` 后端。
2. **文档与模板同步**：吸收 3 份自足 prompt 示例模板到 `refs/`，更新 `SKILL.md`、`docs/overview.md`、`docs/CURRENT.md`、`refs/`；在 `docs/overview.md` 中诚实记录委托 CLI `--sandbox` 的边界裁决。
3. **强制治理审查**：修改 `SKILL.md` 与 `docs/overview.md` 前，按 `AGENTS.md:30` 执行独立跨 family 治理审查（`ultraverge`）。
