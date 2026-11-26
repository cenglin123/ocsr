---
status: completed
created: 2026-11-26
owner: orchestrator
scope: architecture-design
repo: ocsr
---

# Plan: OCSR 单元组合式 CLI 调度框架（Composable CLI Orchestrator）

## 1. 背景与目标

### 1.1 问题与契机
OCSR 当前是单层无头子代理派发器（`scripts/ocsr_dispatch.py` + `scripts/ocsr_backends.py`），支持 `opencode run --standalone` 与 `codex exec` 双后端，具备 Windows `KILL_ON_JOB_CLOSE` Job 生命周期管理、进程身份校验、看门狗、白名单与只读看板（`scripts/ocsr_board.py`）。

既有 `refs/hierarchical-command.md` 已给出层级指挥的文档协议（并指出三层是默认形态而非定数），但仍缺少**机械预算/深度熔断、CLI 调度原语与文件系统状态机**。将 OCSR 演进为抽象的、基于 Harness CLI 运行机制的任务调度框架，核心目标是：

- **三种原子角色（策略画像预设）**：`orchestrator`、`worker`（`executor`）、`reviewer`。
- **模型驱动动态建树**：主控模型按任务复杂度临时构建任意树状组织架构（扁平扇出、多层子编排、多视角并审），脚本只负责确定性防呆与记账，不替模型做任务拆解、拓扑设计、模型选择或语义裁决。
- **无常驻守护进程（Daemonless UNIX 原语）**：每次单元调用仍是一次性脱管 CLI 进程，但通过**幂等请求、可恢复进程身份锁、读时惰性对账（reconcile-on-read）、三段式预算不变量与父生命周期包含子树**闭环分布式状态机。

### 1.2 非目标与安全边界
- **不建常驻 daemon / WebSocket 消息总线 / tmux PTY 复用器**。
- **不提供 OS 级安全沙箱**：同用户 OS 进程共享权限，角色、预算、深度、调用者身份绑定与写租约均属**防呆型协作约束（mechanical fail-closed guardrails）**，而非对抗恶意进程的安全隔离。`codex --sandbox read-only` 由 Codex CLI 提供机械只读；`opencode` 无进程级沙箱，仅提供 prompt 契约与事后审计。
- **Phase 1 禁止在 `converge` 上下文中使用**：未实现与 `converge` 预算门 1:1 `reserve/settle` 桥接前，`ocsr_org.py` 检测到 `--converge-ledger` 或 converge 上下文环境变量时直接 fail-closed（`exit 2`），杜绝旁路计数。
- **不破坏现有 `ocsr_dispatch.py` 兼容契约**：既有 `dispatch` / `run --spec` / `preflight` / `dsh-ocsr` 保持向后兼容；`dispatch`、`run --spec`、`ocsr_org` 三入口共享底层 `ocsr_runtime.py` + `ocsr_backends.py`。
- **Phase 1 不支持单元内 `--resume-from`**：每个 `unit` 严格对应 1 次 fresh CLI invocation，传入 `--resume-from` 一律 fail-closed（`exit 2`）。

---

## 2. 核心抽象：三原语角色、策略画像与虚拟根

### 2.1 三种原子角色与策略画像
`spec.json` 将能力显式落盘为策略字段（`spawn_allowed`、`write_policy`、`fresh_context`），`role` 为三种内置预设：

| 角色 (`role`) | `spawn_allowed` | `write_policy` | `fresh_context` | 后端只读/隔离边界与机械防呆 |
|---|---|---|---|---|
| `orchestrator` | `true` | `artifacts-only` | `true` | 只能在剩余预算与 `depth < max_depth` 内向下派发；须等待所有子单元终态后才可结算为 `succeeded`；若 `budget_alloc > 1` 且零子单元结束，结算记录 `WARN:zero_children_orchestrator`（除非显式 `--allow-self-execute`）；git 仓库内结算检测 `git status --porcelain`，若改动仓库代码则记录 `WARN:orchestrator_mutated_repo` |
| `worker` (`executor`) | `false` | `repo` 或 `artifacts-only` | `true` | 叶子节点，调用 `unit spawn` 直接 fail-closed `exit 2`；声明写代码目录时须在 `reserved` 期取得跨 run 规范工作树写租约 |
| `reviewer` | `false` | `none`（Codex）或 `artifacts-only`（OpenCode） | `true` | 叶子节点，禁止 `--resume-from`；可绑定 `--review-target <unit_id>`（冻结被审单元的 commit SHA / 产物 SHA-256 快照）；**能力分层**：`codex` 机械强制 `--sandbox read-only --artifact-mode capture-reply`（缺省自动注入，显式传可写沙箱则 fail-closed `exit 2`）；`opencode` 无进程级沙箱，报告写入本单元独占 `artifacts/`，对被审仓库属 prompt 契约 + `reads:`/`forbid_paths` 事后审计 |

### 2.2 虚拟根单元 `u0000-root` 与调用者身份绑定
- **`u0000-root` 是虚拟根**：`run init` 创建 `units/u0000-root/spec.json`，标记 `virtual: true, role: "orchestrator", depth: 0, budget_alloc: max_calls`，无 launcher、无进程、无 `result.json`。宿主主控自身回合**不消耗** `max_calls`（仅记录在 `run.json` 中明示）。
- **全角色调用者身份绑定**：`unit spawn` 为**所有角色**（`orchestrator`、`worker`、`reviewer`）统一注入环境变量 `OCSR_RUN_ID`、`OCSR_UNIT_ID`、`OCSR_UNIT_ROLE`、`OCSR_RUN_DIR`。
  - 当进程环境存在 `OCSR_UNIT_ID` 时，`unit spawn` 将其视为调用者权威身份，并核对 `OCSR_RUN_ID` 与 `--run` 一致；若同时传入 `--parent` 且与 `OCSR_UNIT_ID` 不一致，直接 fail-closed `exit 2`（防止叶子单元或兄弟单元误用 `u0000-root` 或他人 ID）。
  - 当进程环境存在 `OCSR_UNIT_ROLE`（即处于任意子单元内部）时，调用 `run init` 直接 fail-closed `exit 2`（防止子模型误开新 run 绕过预算树）。
  - 仅在未设置 `OCSR_UNIT_ID` 的顶层宿主环境中，允许以 `--parent u0000-root` 派发。

### 2.3 动态组织拓扑与传递原则
- 支持扁平单派、并行多工作树扇出、以及任意深度（受 `max_depth` 约束）的递归子树。
- **原文传递原则**：单元间传递文件路径、commit SHA 与 SHA-256 摘要；`reviewer` 直读原文与确定性验证输出，禁止读取被审作者的 `prompt.txt` / `run.log`（防思维链污染）。
- **模型族（`family`）核验**：`spec.json` 记录 `family`（优先读取后端配置，其次查内置/本地 provider-model 映射；无法识别时记为 `"unknown"`）。若 `reviewer` 绑定了 `--review-target`，同族时输出 `WARN:same_family_review`，任一方为 `unknown` 时输出 `WARN:family_unknown`（不静默视为跨族）。

---

## 3. 文件系统布局、并发锁与状态机

### 3.1 目录布局（原子写 + 不可变/可变分离）
默认根目录为 `~/.ocsr/runs/`（支持 `--run-root <dir>` 覆盖，测试使用临时目录）。所有 JSON 状态文件均采用 `tmp + os.replace` 原子替换：

```text
<runs_root>/
├── init.lock                        # 根级 OS 文件锁（保护 run init 幂等表）
├── init-requests.json               # 根级 run init 幂等索引：{request_id: {run_id, created_at}}
├── leases.lock                      # 根级 OS 文件锁（串行化跨 run 工作树写租约抢占与释放）
├── leases/
│   └── <worktree_key_sha256>.json   # 跨 run 工作树写租约：{worktree_key, worktree_path, run_id, unit_id,
│                                    #   branch, base_sha, acquired_at}
└── <run_id>/
    ├── run.lock                     # run 级 OS 文件锁（进程退出即由内核自动释放，无无主锁窗口）
    ├── run.json                     # 不可变根配置：{run_id, goal, repo_root, max_calls, max_depth,
    │                                #   virtual_root_id, root_turns_counted: false, created_at, deadline_at?}
    ├── run-state.json               # 可变运行态：{status: active|closed|aborted, updated_at, closed_at?}
    ├── requests.json                # 单元幂等请求表：{request_id: {action, unit_id, created_at}}
    ├── ledger.jsonl                 # append-only 权威审计与结算事件流（所有写操作在 run.lock 内；读端容忍尾部半行）
    └── units/
        └── <unit_id>/               # 如 u0000-root, u0001-orch-sub, u0002-worker-auth, u0003-rev-auth
            ├── spec.json            # 不可变规格：{unit_id, parent_id, depth, role, virtual,
            │                        #   spawn_allowed, write_policy, fresh_context, backend, model, family,
            │                        #   work_dir, worktree_ref?, review_target?, sandbox?, artifact_mode?,
            │                        #   budget_alloc, expected_outputs[], timeout_sec, created_at}
            ├── prompt.txt           # 自足任务卡原文（UTF-8 无 BOM，对齐既有 launcher 读取路径）
            ├── launcher.ps1         # 复用 ocsr_runtime 生成的脱管启动脚本（KILL_ON_JOB_CLOSE）
            ├── process-identity.json # {pid, creation_time, job_bound}
            ├── start.marker         # launcher 写入的启动/退出标记（含 exit=<code>）
            ├── run.log              # 底层 CLI 原始输出流
            ├── stderr.log           # 原始 stderr
            ├── artifacts/           # 本单元独占产物目录
            ├── summary.json         # orchestrator 结构化交接物（运行时填充 children/artifacts/budget + 模型决策字段）
            └── result.json          # 终态物化快照（ledger 结算落盘后原子写入）：{status, exit_code, elapsed_sec,
                                     #   artifacts[{path, size, sha256}], warnings[], error_reason?, reconciled_at}
```

### 3.2 进程退出自动释放的 OS 文件锁与幂等性
- **OS 级互斥文件锁**：使用 `msvcrt.locking`（Windows）/ `fcntl.flock`（POSIX）锁定 `init.lock`、`leases.lock` 与 `<run_id>/run.lock`。进程异常崩溃时由操作系统内核自动释放锁句柄，彻底消除「建锁后写 owner 前崩溃遗留无主锁」窗口；等待锁超时返回 `exit 1`。
  - **缩小临界区**：`git rev-parse`、进程表扫描、launcher 文件准备尽量在锁外完成，锁内仅执行预算检查/预留、幂等登记与账本追加；同时所有跨 run 工作树写租约抢占/释放统一经根级 `leases.lock` 原子串行化。
- **两级幂等键（`--request-id`）**：
  - `run init --request-id <key>` 在 `init.lock` 内查写 `<runs_root>/init-requests.json`，命中直接返回已有 `run_id`；
  - `unit spawn --request-id <key>` 在 `run.lock` 内查写 `<run_id>/requests.json`，命中直接返回原 `unit_id` 与当前状态，不重复扣预算、不重复抢租、不重复拉起进程。

### 3.3 硬不变量：父生命周期 ⊇ 子树（Windows `KILL_ON_JOB_CLOSE` 对齐）
Windows launcher 的 Job Object 仅设 `KILL_ON_JOB_CLOSE`（`0x2000`，不设 `BREAKAWAY_OK`），因此嵌套在子 `orchestrator` launcher 内的孙单元自动归属父 Job：
1. **结算硬约束**：`orchestrator` 单元进入终态时，若仍有非终态子单元，拒绝结算为 `succeeded`（记为 `failed` 或 `uncertain`，并将未终态子树标记为需清算）。子 `orchestrator` 必须等待全部下级进入终态后方可正常结束。
2. **级联清理保证**：父单元退出、超时被杀或被 `unit abort` 终止时，其后代进程树随 Job 确定性销毁（且 `unit abort` 同时复用身份校验逐级确认后代退出）。
3. **恢复语义**：不存在「接管已断联的活子树」；父节点恢复永远是读取 `run tree` / `unit inspect` 核对工作树与产物证据后，显式派发新单元或中止。
4. **实测要求**：Phase 1 测试必须包含**真实两级嵌套 Windows launcher 测试**（父进程正常退出、父进程被强杀、`unit abort` 三场景），验证孙进程实际存活/退出状态与预算结算，而非仅测 fake CLI。

### 3.4 状态机、读时惰性对账与三段式预算不变量
- **单向终态**：`reserved` → `running` → `succeeded` | `failed` | `timed_out` | `aborted` | `uncertain`。
- **读时惰性对账与可重放结算（Reconcile-on-Read）**：`ledger.jsonl` 为结算权威事实源，`result.json` 为物化快照。任何读命令（`unit status`、`unit wait`、`run tree`、`unit inspect`）在发现 ledger 尚未结算的单元满足以下任一条件时，取 `run.lock` 执行一次可重放对账：
  1. `start.marker` 已包含 `exit=<code>`；
  2. `process-identity.json` 对应的 `(pid, creation_time)` 经进程表确认已退出；
  3. 单元超过 `timeout_sec` 截止时间（执行身份校验终止后结算为 `timed_out`）；
  4. `reserved` 超出启动宽限期且无 `process-identity.json`（标记为 `uncertain`）。
  对账时计算 `artifacts/` 与声明产物的 SHA-256，**先**在 `run.lock` 内将 `unit_terminal` 及预算结算事件（`budget_returned` 或 `budget_held`）幂等追加至 `ledger.jsonl`（同一 `unit_id` 不重复记账），**再**原子写入 `result.json`（若上次在写完 ledger 后、写 `result.json` 前崩溃，下次对账会补写 `result.json` 而不重复结算预算）。对已确认退出的单元经 `leases.lock` 释放写租约。
- **三段式预算硬不变量**：
  ```text
  consumed(已发生的 CLI 调用次数) + held(uncertain 子树冻结额度) + reserved(活跃子树未分配额度) <= run.max_calls
  ```
  - 派发 `worker` / `reviewer`：父节点可用余额减 `1`，`consumed += 1`。
  - 派发子 `orchestrator`（`--budget K`，`K >= 1`）：父节点可用余额减 `1 + K`，`consumed += 1`，子 `orchestrator` 获得 `reserved = K`。
  - **正常关闭回流**：当子 `orchestrator` 进入终态且其全部子孙均已确认终态且无 `uncertain` 冻结时，其剩余 `reserved` 自动回流给父节点（写入 `budget_returned` 事件）。
  - **`uncertain` 冻结（Hold）与解冻证据**：若子 `orchestrator` 以 `uncertain` 结束，或子树中存在进程存活未证实的 `uncertain` 单元，剩余子树额度转入 `held`（**绝不自动归还**）。仅当父节点显式调用 `budget release --run <id> --unit <uid>`，且满足以下证据之一时才允许解冻释放并清租：(a) `ledger.jsonl` 无 `unit_spawned` 且 `pid.txt` 不存在（证实从未启动）；或 (b) `pid.txt`/`process-identity.json` 对应进程及其后代经进程表证实已全部退出。
  - **账本事件集**：`run_init`、`unit_reserved`、`unit_spawned`、`unit_terminal`、`budget_returned`、`budget_held`、`budget_released`、`unit_aborted`、`run_closed`（不保留无对应命令的死事件）。

---

## 4. 面向模型的极简 Shell 原语（CLI 契约）

新入口 `scripts/ocsr_org.py` 提供 10 条子命令（全部支持 `--json` 输出）：

- **统一退出码契约**：
  - `0`：命令成功执行（包括 `unit wait` 在有界等待到期后返回 `state: "running"`；单个单元的失败通过 JSON 中 `status` 字段表达，不借用 `exit 1`）。
  - `1`：基础设施或运行态异常（如进程表不可读、锁竞争超时、不可恢复 I/O 错误）。
  - `2`：参数、契约或防呆规则拒绝（fail-closed：超预算、超深度、叶子派发、调用者身份不匹配、converge 上下文禁用、工作树写租约冲突、reviewer 沙箱违规、不支持 `--resume-from`）。

| 命令 | 作用 | 关键参数与防呆语义 |
|---|---|---|
| `run init` | 初始化根任务、虚拟根与预算池 | `--goal <text>` `--max-calls <N>` `--max-depth <D>`（默认 3）`[--repo <path>]` `[--run-root <dir>]` `[--request-id <key>]` `[--dry-run]`；在 `OCSR_UNIT_ROLE` 已设置或 converge 上下文激活时 fail-closed `exit 2` |
| `unit spawn` | 预约预算、获取写租约并脱管拉起原子单元 | `--run <id>` `[--parent <uid>]` `--role <orchestrator\|worker\|reviewer>` `--backend <opencode\|codex>` `--model <m>` `--prompt-file <p>` `[--request-id <key>]` `[--budget <k>]` `[--work-dir <p>]` `[--worktree-dir <p> --worktree-branch <b> --worktree-base <sha>]` `[--output <path>]...` `[--review-target <uid>]` `[--sandbox <mode>]` `[--artifact-mode <mode>]` `[--timeout <sec>]` `[--reasoning-effort <e>]` `[--allow-self-execute]` `[--allow-dirty]` |
| `unit wait` | 有界等待一个或多个单元（含惰性对账） | `--run <id>` `--units <u1,u2,...>` `--mode <any\|all>` `[--timeout <sec>]`（默认 `90`，上限 `100` 以适配宿主/CLI 120 秒 shell 超时；限时覆盖整次命令含锁等待与产物哈希，未完对账允许下次续做）`[--once]` `[--poll-interval <sec>]`；到期仍有运行中单元时返回 `exit 0` + `{state: "running", done_units, running_units}`；全部/任一满足时返回 `exit 0` + `{state: "done", ...}` |
| `unit status` | 对账并查询单/多单元状态 | `--run <id>` `[--units <...>]`；触发读时惰性对账 |
| `run tree` | 一屏输出当前动态组织树与预算三段账 | `--run <id>`；紧凑文本/JSON：每个节点的 `unit_id / role / model / family / status / budget(used/reserved/held/alloc) / elapsed / artifacts_count / warnings` |
| `unit inspect` | 链式召回某单元详细证据 | `--run <id>` `--unit <uid>`；输出 `spec.json` 摘要、`result.json`、`summary.json`（若有）、产物路径与 SHA-256、以及严格截断（≤60 行 / ≤4 KB）的末段错误诊断 |
| `unit abort` | 递归终止指定单元及其子树 | `--run <id>` `--unit <uid>`；锁内封闭该子树新派发，按 `process-identity.json` 校验后终止 launcher 与后代进程，写入 `aborted` 终态并按规则退回或冻结预算、释放已死进程租约 |
| `run abort` | 终止整个 run 下所有活跃单元 | `--run <id>`；等价对根下所有活跃子树执行 `unit abort`，更新 `run-state.json` 为 `aborted` |
| `budget release` | 核实无活进程后释放 `uncertain` 冻结额度 | `--run <id>` `--unit <uid>`；通过进程表证实该单元及其后代无活进程后，将 `held` 释放回父节点并清理残留租约；若仍有活进程则 fail-closed `exit 2` |
| `run close` | 封闭并结算整个 run | `--run <id>`；触发全树对账，若仍有活跃单元或未释放 `held` 额度则 fail-closed `exit 2`，否则写 `run-state.json = closed` |

---

## 5. 关键工程防呆与共享内核切分

### 5.1 工作树任务身份绑定与跨 Run 写租约
- 写代码类 `worker` 传入 `--worktree-dir` 时，规范化其 Git 顶层路径（`git rev-parse --show-toplevel` + `os.path.normcase(os.path.realpath(...))`），同时记录 `worktree_ref = {path, toplevel, branch, base_sha}`；若 `base_sha` 存在则校验 `git merge-base --is-ancestor`；默认拒绝脏工作树（`--allow-dirty` 可显式放行）。
- 在 `<runs_root>/leases/` 中经根级 `leases.lock` 原子串行化，以规范工作树顶层路径的 SHA-256 为键落盘写租约（覆盖所有 `write_policy == "repo"` 的单元）：
  - 覆盖范围：`reserved` + `running` + 进程存活未证实的 `uncertain`，跨所有 run 生效，同一工作树的不同子目录映射到同一个租约键；
  - 冲突时先校验持租约单元的进程身份：若对应单元已终态且进程已死，自动回收过期租约；否则 fail-closed `exit 2`。

### 5.2 超时执行点与嵌套期限
- 无 daemon 模式下，单元 `--timeout`（单位秒，默认 `1800`）的明确执行点为 `unit wait` / `unit status` / `run tree` / `unit inspect` 的对账路径（在 CLI `--help` 中明示：无人观察时不自动终止，观察到超期时执行身份校验终止并标记 `timed_out`）。
- 派发子 `orchestrator` 时，若其 `--timeout` 小于合理下限或父节点剩余期限，输出 `WARN:nested_timeout_tight` 提醒父节点。

### 5.3 子 `orchestrator` 速查头与防退化守卫
- 为 `role=orchestrator` 生成 `prompt.txt` 时，前置注入 **≤25 行调度速查头**：
  - 给出字面量环境变量用法（`--run $env:OCSR_RUN_ID --parent $env:OCSR_UNIT_ID`）、可用子预算 `budget_alloc` 与剩余允许深度；
  - 明确有界等待循环规则：`unit wait` 返回 `state == "running"` 不是失败，须重入等待（上限 6 次，超限则 `run tree` 并写明现状上报父级）；
  - 三条底线：不亲自写重型业务产物、子单元失败/不明时先查证据不盲目重派、结束前写入 `summary.json` 决策字段且确认所有子单元已终态。
- **结算期机械守卫**：
  - 运行时在结算子 `orchestrator` 时自动生成/合并 `summary.json`（机械填充 `children[]`、`artifacts[]`、`budget` 汇总，保留模型写入的 `decision` / `unresolved` / `self_executed` 字段）；
  - `budget_alloc > 1` 且 `children_count == 0` 且未传 `--allow-self-execute` → 记录 `WARN:zero_children_orchestrator`；
  - git 工作区出现非 `artifacts/` 改动 → 记录 `WARN:orchestrator_mutated_repo`。

### 5.4 最小公共运行时切分（`scripts/ocsr_runtime.py`）
Phase 1 仅抽取 `ocsr_dispatch.py` 中两条路径真正共享且最易漂移的薄原语到 `scripts/ocsr_runtime.py`（`ocsr_runtime.py` 不反向 import `ocsr_dispatch.py`）：
1. **进程表与身份校验**：`_query_process_table`、`_is_process_alive`、`_kill_process_tree_verified`（基于 `pid + creation_time` 与后代轮询，进程表不可读时 fail-closed）。
2. **脱管 Launcher 生成与启动**：`_pwsh_code` / `_spawn_windowless`（含 Windows `KILL_ON_JOB_CLOSE` Job 包装，与重构前逐字节/行为等价）。
3. **状态/Marker/快照工具**：`_atomic_write_json`、`start.marker` 解析、产物文件 SHA-256 与存在性快照。

`ocsr_dispatch.py` 通过 import 复用上述原语，保持零语义变更，既有 517 项测试 100% 全绿。

---

## 6. 分阶段实施与验证计划

### Phase 1a：最小公共原语抽离（`scripts/ocsr_runtime.py`）
- 抽离进程身份表、验证式终止、脱管 launcher 生成与原子写原语到 `scripts/ocsr_runtime.py`，`ocsr_dispatch.py` 切为直接引用。
- **验收**：既有 517 项 `pytest`、`verify_ocsr_skill.py`、`audit.py check` 100% 全绿。

### Phase 1b：核心调度器 `scripts/ocsr_org.py` + 离线与嵌套 Job 回归测试
- 实现 10 条 CLI 原语、可恢复进程身份锁、`--request-id` 幂等、跨 run 规范工作树写租约、读时惰性对账、三段式预算守恒与 `held` 释放、`reviewer` 能力分层与 `family` 告警、子 `orchestrator` 速查头与 `summary.json` 汇总。
- `tests/test_ocsr_org.py` 覆盖：
  1. 预算预扣 `1+K`、正常回流、超预算 fail-closed、并发抢最后 1 次额度；
  2. 深度熔断、叶子节点禁派发、调用者身份 `OCSR_UNIT_ID` 绑定与防伪造 `--parent`、子单元禁 `run init`；
  3. `--request-id` 幂等重放（含模拟崩溃后同 key 恢复不重复扣款）；
  4. `lock/owner.json` 死锁确定性夺锁恢复；
  5. 读时惰性对账与 `uncertain` 预算 `held` + `budget release` 释放；
  6. 跨 run 规范工作树写租约冲突（含不同子目录映射同一 worktree 顶层）；
  7. `unit wait` 有界返回 `exit 0` + `state: "running"` 与 `--mode any/all`；
  8. `reviewer` 禁 `--resume-from`、Codex 强制 `read-only + capture-reply`、同族/未知族告警；
  9. `converge` 上下文 fail-closed 拒绝；
  10. **真实 Windows 两级嵌套 launcher 测试**（真实 PowerShell Job Object：父正常退出/强杀/`unit abort` 时孙进程生命周期与预算清算验证）。

### Phase 1c：真实双后端递归狗食
- 披露模型池与调用上限后执行一次真实递归狗食：顶层虚拟根 `u0000-root` → 1 个子 `orchestrator`（`codex` `gpt-6.2-astra`，`budget=3`）→ 子 `orchestrator` 动态派发 1 个 `worker`（`opencode` `alibaba-token-plan-cn/deepseek-v4.1-flash-1017`）产出独立文件，待其落盘后再派发 1 个跨族 `reviewer`（`codex` `gpt-6.2-sol` 只读 `capture-reply` 或 `opencode` `qwen4-max`）审阅固定产物；核对 `run tree`、`summary.json`、预算回流与零残留进程。

### Phase 2：工作树深度联动 + `ocsr_board.py` 组织树视图
- `ocsr_board.py` 复用 runs 登记机制，新增只读 Org Tree 视图。
- 演练 `worktree_task.py` 联动与故障恢复（父节点核对中断子树证据后重派新单元）。

### Phase 3：文档升级、converge 桥接与治理收口
- 评估 Phase 1/2 实证指标（对比根直接扇出与一层子编排的调用数、人工接管数与漏验数），决定是否引入 `converge` 1:1 `reserve/settle` 桥接与正式归并 `refs/hierarchical-command.md`，最终经 `ultraverge` 治理审查收口。

---

## 7. 经跨家族评议锁定的设计决策

1. **预算模型**：采用预划扣 `1 + K` + 整棵子树闭环后回流 + `uncertain` 转 `held` 需显式核验释放；不采用先到先得全局池。
2. **运行时抽离时机**：Phase 1 先抽最薄共享切片 `ocsr_runtime.py`（517 测试全绿作证明），避免维护两份 Windows Job launcher 导致漂移。
3. **子 `orchestrator` 交接形态**：强制结构化 `summary.json`，主体（`children[]` / `artifacts[]` / `budget`）由运行时机械填充，模型仅填写决策与未决事项；原文仍按路径直读。
4. **与 `dispatch` / `run --spec` 的关系**：收敛到单一 `ocsr_runtime.py` + `ocsr_backends.py` 底座，三个入口按语义正交并存（单次派发 / 确定性 DAG / 模型驱动动态树），不提前吞并 `run --spec`。
5. **防子 `orchestrator` 退化**：≤25 行字面量速查头作引导，真正守卫落在结算期机械检查（零子单元告警、git 仓库改动探测与 `summary.json` 产物索引校验）。
