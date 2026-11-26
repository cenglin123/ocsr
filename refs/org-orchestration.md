# 单元组合式 CLI 调度（`scripts/ocsr_org.py`）

`scripts/ocsr_org.py` 在既有 `scripts/ocsr_runtime.py` + `scripts/ocsr_backends.py` 底座上提供面向模型的动态组织树调度原语：仅定义三种原子角色（`orchestrator` / `worker` / `reviewer`），由主控模型按任务复杂度临时组合任意树状结构，底层脚本静默负责预算守恒、深度熔断、OS 文件锁、幂等去重、跨 run 工作树写租约与读时惰性对账。

## 5 条面向 Agent 的核心命令

| 命令 | 用法示例 | 说明 |
|---|---|---|
| `init` | `python scripts/ocsr_org.py init "目标说明" --budget 6` | 初始化根任务、虚拟根 `u0000-root` 与预算池（默认 `--depth 3`） |
| `spawn` | `python scripts/ocsr_org.py spawn worker -m <model> -p <指令或文件>` | 派发 `orchestrator` / `worker`（`executor`）/ `reviewer`；按模型名自动识别 `opencode`（含 `/`）或 `codex`（裸 slug）；子 `orchestrator` 须传 `--budget K`；可选 `--wait` 一步等待 |
| `wait` | `python scripts/ocsr_org.py wait [unit_id...] [--any]` | 有界等待（默认 90s，上限 100s）；不传 ID 默认等待当前所有活跃子单元；到期未完返回 `exit 0` + `state="running"`，可安全重入 |
| `status` | `python scripts/ocsr_org.py status [unit_id]` | 无参输出整棵组织树与预算；带 `unit_id` 输出该单元 `spec/result/summary`、产物路径与 SHA-256 及截断错误尾 |
| `stop` | `python scripts/ocsr_org.py stop [unit_id]` | 递归终止指定单元子树（无参终止整个 run），结算预算并释放已死进程租约 |

可选收尾命令：`python scripts/ocsr_org.py close`（确认无活跃单元与未释放冻结额度后将 run 标为 `closed`）。所有命令支持 `--json` 输出；子单元运行环境自动注入 `OCSR_RUN_ID` / `OCSR_UNIT_ID` / `OCSR_UNIT_ROLE` / `OCSR_RUN_DIR` / `OCSR_ORG_RUNS_ROOT`，子编排器调用 `spawn` / `wait` / `status` 时无需重复手填 `--run` / `--parent` / `--run-root`。

## 退出码契约

- `0`：命令执行成功（含 `wait` 有界到期返回 `state="running"`；单元自身失败通过 JSON 的 `status` 字段表达）
- `1`：基础设施或运行态异常（锁超时、进程表不可读、I/O 错误）
- `2`：参数或规则拒绝（超预算、超深度、叶子派发、身份不匹配、工作树租约冲突、不支持 `--resume-from`、`converge` 上下文禁入等）

## 角色边界与安全说明

- **同用户进程非 OS 安全沙箱**：角色、预算、深度、身份绑定与写租约均为防呆型协作约束，不提供对抗恶意进程的 OS 隔离。
- **`reviewer` 能力分层**：
  - `codex` 后端机械强制 `--sandbox read-only --artifact-mode capture-reply`（显式传可写沙箱返回 `exit 2`）。
  - `opencode` 后端无进程级沙箱，报告写入本单元独占 `artifacts/`，对被审仓库属 prompt 契约与事后审计。
  - 传入 `--target <unit_id>` 时冻结被审目标产物快照并核验模型 `family`（同族输出 `WARN:same_family_review`，未知族输出 `WARN:family_unknown`）。
- **Phase 1 `converge` 边界**：与 `converge` 预算门的 1:1 `reserve/settle` 桥接落地前，检测到 `CONVERGE_*` / `OCSR_CONVERGE_LEDGER` 环境变量或 `--converge-ledger` 时直接 fail-closed（`exit 2`），防止旁路计数。
- **父生命周期 ⊇ 子树**：Windows `KILL_ON_JOB_CLOSE` 下父单元退出或被终止会级联清理后代；子 `orchestrator` 须等全部下级终态后才可正常结束。

## 只读看板（组织树视图）

`ocsr_org.py init` 会把运行根目录登记到 `~/.ocsr/org-roots.json`（单测与 `OCSR_DISABLE_MODEL_CALLS=1` 自动跳过）。运行 `python scripts/ocsr_board.py`（可选 `--org-root <dir>`）即可在浏览器「组织树」Tab 只读查看动态拓扑、预算进度与右侧节点详情抽屉（`GET /api/org-unit`）。
