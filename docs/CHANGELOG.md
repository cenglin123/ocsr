# CHANGELOG

## 2026-10-18

### 返工可接续原会话（dispatch --resume-from）

#### 变更内容
- `dispatch` 新增 `--resume-from <原 worker 派发目录>`：worker 正常完成后按验收清单返工时，可接续原会话。只允许单个 worker；原派发模型须与本次一致（从原目录 `launcher.ps1` 核对，无法核对即拒绝）；`session-binding.json` 须有合法唯一 session ID；旧进程须确认已退出（PID 复用视为已退出，进程表不可读或身份不一致则拒绝），且进程表中不得有仍以该 PID 为父的存活后代（opencode 残留）。
- launcher 以 `opencode run <prompt> --format json --standalone --session <id> -m <model>` 启动，照常走看门狗、产物验收、ledger 与 telemetry；账本与遥测记录 `resumed_from`、`resume_session`、`resume_mode` 与 `rework_index`，账本另记 `attempt_index`，`worker-state.json` 同样记录模式与两种计数。
- `--resume-from` 按原目录状态自动区分两种模式：`worker-state.json` 为 `landed`，或无 `worker-state.json`/仅启动态且 `start.marker` 为 `exit=0` → **返工**，沿用原 `attempt_index`、`rework_index` + 1，上限 3 轮；非 0 退出、无 exit 记录或 `timed_out`/`stopped`/`failed` 等 → **故障恢复**，`attempt_index` + 1，三次总上限。返工模式拒绝 `--attempt-index`，恢复模式拒绝 `--rework-index`；原目录缺 `rework_index` 可用 `--rework-index` 显式指定（须大于原值且 ≤3）。
- 非 `--watch` 派发也会在启动时把模式与计数写入 `worker-state.json`，使失败重试与返工上限在非 watch 路径同样有据可依。
- 新会话仍写新的 `session-binding.json`；返回的 sessionID 与原 id 不同时照实记录并提示（回读与提示只在 `--watch` 路径执行）。
- 文档同步：`docs/overview.md` 第 2 条、`refs/dispatch-patterns.md`「接续原会话（返工）」、`SKILL.md` 入口指针、`docs/deployment.md` 升级复验清单。

#### 迁移影响
- 无配置变更。默认派发行为不变；仅在显式传 `--resume-from` 时启用续接。

### worker 改用私有服务器（opencode run --standalone）

#### 变更内容
- launcher 与 resume_argv 加 --standalone：默认连接的常驻后台服务为每个工作目录常驻 MCP 子进程且不随 run 释放，派发目录（尤其 git worktree）无法删除、进程逐次累积；首个 run 还会自行拉起常驻服务。测试断言 launcher 与续接参数含 --standalone。

#### 迁移影响
- 无配置变更；已残留的 MCP 进程需 opencode service restart 释放。

---

## 2026-09-22

- **模型白名单本地化**：`config/allowed-models.json` 排除 git（`.gitignore` + 解除跟踪），每台机器各自维护。文件缺失/为空 = 未配置：首次使用前询问用户启用哪些模型（`opencode models --verbose` 展示 → 征求 → 写入）；非空直接使用。非法格式仍 fail-closed。加载器支持 `OCSR_ALLOWED_MODELS_PATH` 覆盖配置路径（测试/多套配置）。npm 包不再携带 `config/`。
- 测试与用户配置解耦：全部改用合成白名单或临时配置注入（含子进程 CLI 测试），无 `config/allowed-models.json` 时也能全绿。
- 修复 dsh 工具面致命 bug：`lib/tool-ocsr.js` 曾用 `node` 执行 Python 驱动（`spawn(process.execPath, ...)`），前台与后台路径均不可用；改为 Python 解释器（`OCSR_PYTHON` 可覆盖）。
- 工具层错误码与 driver 退出码契约脱钩：模型门拒绝 = `4`、基础设施失败 = `5`（原先借用 `2`/`3` 会与「确定性失败/路径碰撞」混淆）；driver 退出码 `0/1/2/3` 透传不变。
- `agent_links.py repair` 覆盖派生文件不再要求 `--force`（与「只编辑 AGENTS.md」工作流一致；`--force` 保留为兼容 no-op）。
- 清理：删除已完成的 5 份 active 计划（历史在 Git）；修复 `20260810-deterministic-run-spec.md` 死链引用；`refs/dsh-integration.md` 真实用户路径改 `<user-home>` 占位；刷新陈旧测试基线数字；`audit.py` birth-record 去掉悬空候选；`projection-ocsr.js` schema 与 fold 对齐（可选字段）；tool schema 数值字段改 integer。

## 2026-09-11

- 默认白名单跟随 DeepSeek 更新：`deepseek/deepseek-v4-flash` → `deepseek/deepseek-flash`（V4.1 Flash，官方公告称旧 V4 Flash 已下线、仅暂时路由；2026-09-14 起 `deepseek-v4-pro` 也将被路由至该模型）。已按规则以 `opencode models --verbose` 核对本地池并 preflight 探测通道可用。
- 看门狗超时与 Session 恢复落地（计划 20260911-ocsr-watchdog-session-recovery，已执行）：检查期限与总期限分离（总期限 `timeout × (max_renewals+1)` 写入 `worker-state.json` 后不可变，续期默认 1 次）、worker 单向状态机、`artifact_seen` 与 `landed` 分离；launcher 以 `--format json` 流式解析顶层唯一 `sessionID` 写 `session-binding.json`（缺失/歧义只禁用续接，不改真实退出码）；恢复仅由上层以新 reserve/settle 显式发起，驱动器只产 `resume-material.json`（含 `old_process_stop_verified`、`requires_settle`）；并发 DB 锁从「延迟 30s 自动重试」改为 `recovery_required` 停止交回上层。新增 7 个遥测可选字段，`refs/dispatch-patterns.md` 同步模板与「检查期限、续期与总期限」「session 绑定与恢复材料」两节。
- allowlist 测试改为从 `ALLOWED_MODELS` 加载结果派生，不再在测试内复制模型清单（此前与 #7 合并的 4 模型配置矛盾致 3 例长期红）；`tests/test_audit.py` 临时目录先 `resolve()`，修复 Windows 8.3 短路径下 `relative_to` 假阳性（结构链接测试在本机长期误红）。

## 2026-09-06

- audit 工具词边界修复：drift 检查的子串匹配会把 "windows"（含 "ws"）误判为"文档提到 WebSocket"，自 #3 引入 package.json（manifest 出现）激活该检查起持续误报。改为 `\b` 词边界正则（`DRIFT_KEYWORD_RES` + `_mentions`），tests/test_audit.py 补 2 个回归用例；pisr 仓库同步修复。
- 模型选择规则收紧：默认白名单标注为作者本机模型池样例；每台机器首次使用前（及换机、通道异常归因后）先 `opencode models --verbose` 按本地池重配 `config/allowed-models.json` 再决定模型（SKILL.md、refs/model-defaults.md；与 pisr 同逻辑）。
- fresh 对抗评审补运行时验证合同（refs/failure-modes.md §2、SKILL.md 最小规范）：reviewer 以只读命令佐证时 prompt 须钉死禁止写入与修复（生成/评估分离），报告列明执行的命令与退出码；OCSR 无进程级工具白名单，命令只读性属事后审计面，需进程级硬约束的评审走 PISR。

## 2026-08-18

- 仓库重建为 public，历史起点。此前私有开发历史未随仓分发（脱敏摘要见 `DEVELOPMENT.md`）。
- watcher 失败语义分层：`_detect_name_mismatch` 区分「0 产物」与「产物命名与 pattern 不符」。
- 模型池移除 `deepseek/deepseek-v4-pro`（涨价），池收敛为 flash / mimo-v2.5 / mimo-v2.5-pro。
