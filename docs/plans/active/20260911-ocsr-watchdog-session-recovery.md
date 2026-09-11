# OCSR 看门狗超时与 Session 恢复修复计划

状态：已执行（2026-09-11 落地；验收标准 1-7 全部通过）

## 执行记录（2026-09-11）

- 实现与测试已落地：session 绑定（`session-binding.json`，仅认 `--format json` 顶层唯一 `sessionID`）、进程身份指纹（`process-identity.json`）、worker 状态机与不可变总期限（`worker-state.json`）、恢复材料（`resume-material.json`，含 `old_process_stop_verified` 与 `requires_settle`）、7 个新遥测字段。
- reviewer 非阻断改进已纳入：文档/遥测同步（`refs/dispatch-patterns.md` 模板补 9 字段 + 「检查期限、续期与总期限」「session 绑定与恢复材料」两节）、`requires_settle` 字段、kill 后代进程复核、kill 失败测试、元数据 mtime 记录。
- 计划外顺带修复（提交前套件全绿的必要条件）：`tests/test_ocsr_dispatch.py` allowlist 测试改为从实现加载结果派生（此前与 #7 合并的 4 模型配置矛盾）；`tests/test_audit.py` 临时目录 8.3 短路径导致的 `relative_to` 假阳性（Windows 专属测试 bug）。
- 验收第 7 项实测：`verify_ocsr_skill.py` 13 项通过（无 INFO）、pytest 295 passed、`agent_links.py check`、`audit.py check`、`git diff --check` 通过。

## 交接结论

本计划已经完成 converge 收敛，可以交给其他 agent 执行。最终独立 OCSR reviewer 判定为“可执行”，无阻断问题；证据见归档目录：
`.converge/done/20260911-ocsr-watchdog-session-recovery/evidence/ocsr-final-review.md`。

执行 agent 应优先完成本计划中的代码、测试和文档同步，并在提交前运行验收标准第 7 项。独立 reviewer 提出的文档/遥测同步、元数据 mtime 续期、kill 后代进程复核、`requires_settle` 字段和 kill 失败测试属于非阻断改进，应纳入执行清单；不得降低本计划的不变量。

## 目标

修复 `scripts/ocsr_dispatch.py` 的超时看护闭环，使 worker 到达检查期限时先取得可验证状态，再决定有限续期、交回上层或终止；在明确旧进程已停止且 session ID 可验证时，允许以 `opencode run --session` 续接未完成任务，避免重复执行已落盘内容。

## 范围

- 记录并传播 worker 的 OpenCode session ID、PID、最近事件/进展与超时快照。
- 将“检查期限”与“总时限”分离；续期不得重置总时限，也不得绕过三次尝试上限或 converge 账本。
- 超时策略在终止前支持可验证的状态检查；无法确认状态时 fail-closed，不启动第二个执行者。
- 恢复命令必须复用明确 session ID，并先核对旧进程已停止、产物与副作用状态。
- 修正产物已出现但进程仍在运行时的过早结案风险。
- 补充离线测试、文档与遥测字段。

## 不变量与边界

- OCSR 仍是执行后端，不变成常驻 daemon 或通用工作流引擎。
- `--dir`、prompt 禁令和路径审计不是安全沙箱。
- session ID 不得通过同目录“最新 session”猜测用于自动恢复；候选不唯一必须停机交回。
- 恢复只允许无副作用或已证明幂等的 worker，并纳入原任务预算与三次总尝试上限。
- 不得因日志增长单独判定有效进展；状态证据必须可审计。

## 验收标准

1. worker 启动后能从 JSON 事件或明确的 OpenCode 运行证据记录 session ID；缺失时标记不可恢复，不猜测。
2. 检查期限到达时不会立即强杀；先记录状态快照，并按策略执行有限续期/报告/终止。
3. 总时限和续期次数有硬上限，超过后不会继续等待。
4. 续接前确认旧 PID/进程树已结束；恢复命令包含原 session ID、工作目录和恢复指令。
5. 产物出现但执行仍活跃时进入 `artifact_seen`，不能直接宣告 worker 已完成。
6. 现有退出码、路径碰撞、预算账本和三次尝试语义不回退。
7. `python scripts/verify_ocsr_skill.py`、相关 pytest、`agent_links.py check`、`audit.py check`、`git diff --check` 通过。

## Round 1 修订后的执行契约

### 责任与预算

watcher 只观察、记录和返回状态，不得自行发起第二次模型调用。session 恢复必须由上层作为新的、显式授权的 invocation 发起，使用新的 reserve/settle 记录，沿用同一任务的 attempt index、`converge_invocation_id` 关联和三次总尝试上限。驱动器可以生成“可恢复建议/恢复命令材料”，但不能旁路执行。

### Worker 状态机

每个 worker 按以下单向状态运行：`running → inspection_due → renewed | reported | terminating → stopped | timed_out | landed`。`inspection_due` 必须记录快照：PID/启动身份、session ID 状态、进程树状态、最近 JSON 事件时间、最近文件变化时间、工具/子代理状态、artifact 状态、检查期限、不可变总期限和续期次数。日志字节增长本身不能作为有效进展；无法证明状态时进入 `reported`/`timed_out`，不得并发重派。

检查期限到达只允许有限续期；续期步长与最大次数固定在 worker 元数据中，任何续期都不得延长总期限。总期限到达后必须终止或交回上层。`reported` 保留进程只作为明确的上层裁决状态，不能从观察器中删除跟踪。

### Session 与进程身份

session ID 只接受 `opencode run --format json` 事件中顶层 `sessionID` 的明确值；缺失、格式错误或同一次运行出现多个候选均标记 `session_unavailable`，不通过同目录 SQLite 最新行猜测。session ID、PID 和进程创建时间/命令指纹一起持久化。终止后必须轮询确认目标 PID 与其派生进程均消失，并记录 `old_process_stop_verified=true`；PID 复用、残留子进程或确认超时均为未知状态，禁止恢复。

### 完成与退出码

`artifact_seen` 与 `landed` 分离。成功必须同时满足：执行终止证据、期望产物存在且非空/发生变化、产物抽样验收通过。产物先出现而进程仍活跃时只能进入 `artifact_seen` 并继续看护。产物出现后进程非零退出仍按确定性失败处理；总期限耗尽沿用看门狗超时码 1；路径碰撞 3 的最高优先级和现有 1 > 2 > 0 混合优先级保持不变，并同步 `refs/dispatch-patterns.md` 与测试。

### 需要覆盖的测试

增加假时钟/可控进程测试：总期限不可被续期重置、最大续期数、artifact_seen 后继续监视、kill 失败不恢复、残留子进程不恢复、session 缺失/歧义不恢复、恢复材料包含明确 session、以及既有数值退出码和混合优先级不变。

## 计划修订记录

- 初稿：依据 2026-09-11 对本地 OpenCode 1.18.30、项目看门狗实现及官方 Server API 的审计形成。
