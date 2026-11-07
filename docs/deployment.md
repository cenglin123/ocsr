# 本地运行与验证

OCSR 没有服务部署。这里的“运行”指在本机调用 OpenCode CLI，以及执行离线文档回归检查。

## 依赖

- Windows PowerShell；命令需兼容 Windows PowerShell 5.1。
- Python 3；现有验证脚本仅使用标准库。
- Node.js 24.9+（仅 dsh 适配层回归需要，随 dsh 0.2.2-rc.1 提供；`engines.node` 要求来自 dsh 包）。
- OpenCode CLI 及至少一个已配置、可用的模型 provider。

Windows launcher 通过 PowerShell `Add-Type` 加载内嵌的 Win32 Job helper，运行账号必须允许该操作与 Job 绑定；受限宿主或不兼容的外层 Job 导致失败时会在调用 OpenCode 前退出。Job 属于 launcher，不属于 dispatcher；不新增常驻服务或安装任务。实现见 `scripts/ocsr_launcher_job.cs`，生命周期边界见 [pitfalls.md](pitfalls.md)「opencode 常驻后台服务」。

## 基线检查

```powershell
opencode --version
opencode models
opencode run --help
python scripts/verify_ocsr_skill.py
```

模型可用性和 CLI 参数是实时环境事实。升级 OpenCode 后，应重新验证 `run`、`--dir`、`--continue`、`--session`、`--fork`、stdout 编码行为，以及 `--standalone` + `--session` 的组合续接。

## 运行诊断

launcher 以 UTF-8 接收原生 stdout，JSONL 写入 `run.log`；stderr 独立写入 `stderr.log`。文件为文本事件记录，不是逐字节网络抓包。不要重编码覆盖历史证据。

```powershell
python scripts/ocsr_dispatch.py diagnose --worker-dir <worker-dir>
python scripts/ocsr_dispatch.py diagnose --worker-dir <worker-dir> --session-db <opencode-v2-db>
```

`diagnose` 只输出 JSON，不调用模型、不改 worker 文件。默认不访问数据库；需要原生元数据时显式传入路径，可用 `opencode debug paths` 在本机确认位置。watcher 可通过 `dispatch --diagnostic-session-db <opencode-v2-db>` 在检查点附加同样的元数据。读取使用 Python 标准库 `sqlite3`（须带 JSON 函数），仅支持已核对的 OpenCode v2 `session_message` schema；未知结构、锁或缺记录明确降级，不自动猜测路径或最新会话。查询只取精确绑定且不早于本次 launcher 启动的 assistant 状态/时间/工具输入长度，不取推理、工具输入/输出正文。

看门狗终止或总期限交回时保存 `<worker-dir>/diagnostic.json`；同份摘要进入 worker-state 与派发 ledger。`diagnostic_written` 记录写盘结果，写失败时 ledger 不声明文件存在，内嵌摘要仍保留。`last_phase` 是最后观测值，`phase_source`、`phase_observed_at`、`phase_age_seconds` 给出来源和时效；`event_age_seconds` 仅指 CLI 最后一条事件的年龄。时间观察值/年龄以秒计，原生 `created_at_ms`/`updated_at_ms` 以毫秒计，`last_event_timestamp` 保留 CLI 原值。`generation_pending` 不区分排队、生成、网络或未暴露的权限等待；`tool_call_streaming` 表示参数生成尚未完成，不能当作工具已执行。`assistant_step_complete` / `step_completed` 仅指该条 assistant 消息结束，与产物验收无关；已知活动工具状态优先。诊断不参与续期、重试、恢复资格和预算裁决。

CLI 的工具状态与快照共用同一提取规则。步骤边界不会清除最近未关闭的工具观测；阶段仍保留该工具事件的原始观测时间，不把边界时间当作工具心跳。较新的原生元数据可更新较旧的保留工具观测。没有工具记录用 `not_observed`，已记录但无法解释的状态用 `unknown`。

## 文档体系检查

```powershell
python scripts/audit.py check
git diff --check
git ls-files --eol | Select-String "w/crlf"
```

## dsh 适配层回归

```powershell
node --test tests/adapter/*.test.mjs
python -m pytest tests/test_dsh_adapter.py -q
```

`tests/adapter/*.test.mjs` 用 Node 内置 `node:test`，针对本机真实的 dsh 0.2.2-rc.1 运行时做 11 项契约回归（安装版本门、真实 SkillRegistry 的 skill provider、bundle patch entry 与 package exports 对齐、tool 定义与输出 schema、Config 只收窄语义、后台 job 输出环与状态映射、`job_kill` 的 killed 映射、jobs 缺失时的 code 5）。它需要本机存在 0.2.2-rc.1 的 dsh 运行时，默认自动探测（npm 全局 `@deepseek-ai/dsh/node_modules` > `~/.dsh/profiles/node_modules` > 常见全局路径）；设置 `DSH_RUNTIME_ROOT` 会固定运行时并关闭自动探测（值不可用即整组 skip）。需要把 skip 变成失败（CI）时设 `OCSR_REQUIRE_ADAPTER_TESTS=1`。Node 25 不接受目录参数，须传文件或 glob（等价 `npm run test:adapter`）。

`python -m pytest tests/ -q` 已包含 pytest 桥 `tests/test_dsh_adapter.py`：它枚举 `tests/adapter/*.test.mjs` 并逐个传给 node；本机找不到 0.2.2 运行时或 Node 时整组 skip，不视为失败（设 `OCSR_REQUIRE_ADAPTER_TESTS=1` 则改为失败）。适配契约见 [../refs/dsh-integration.md](../refs/dsh-integration.md)。

## 只读面板

```powershell
python scripts/ocsr_board.py            # 默认 127.0.0.1:8787，自动开浏览器
python scripts/ocsr_board.py --days 30  # 扩大派发目录扫描窗口（默认 7 天；0=全部）
```

面板是纯只读观察者：扫描派发目录下的 worker-state、session-binding、start.marker 与 `~/.ocsr/dispatch-log.jsonl`，不改派发、看门狗或遥测链路；唯一可写文件是面板私有的 `~/.ocsr/board-groups.json`（`assignments` 手动分组 + `roots` 自定义扫描根）。

- **筛选**：顶部状态 chips 点击直筛（再点取消）；状态下拉、模型下拉、关键字、时间窗（近 1h/今天/近 3 天/近 7 天/全部，按最近活动时间判定）；chip 计数跟随其余筛选条件（facet 口径）。遥测历史同样受时间/模型/关键字筛选。
- **排序与视图**：组内按启动时间倒序/正序/状态优先排序（默认时间倒序）；卡片/列表视图切换，列表含启动时间、模型、状态、时长、session 列，行内可展开详情、可拖动分组。失败卡片直接显示人性化原因（如「进程正常退出，但产物未落盘」对应遥测 exit_0_no_artifact；非零退出码、文件名不符等）。排序与视图偏好存 localStorage。
- **分组**：默认按 cwd（session-binding.json 的工作目录）分组，卡片可拖到其他组标题完成手动分组并持久化；组支持折叠/展开、聚焦单组、整组归档、组内默认 6 张 + 显示全部/收起。整组归档按工作目录持续生效（**该目录后续新派发也会被归档**），恢复入口在「已归档」视图；空手动组可在组标题「删除」清除其分配。折叠/隐藏/展开状态存浏览器 localStorage，手动分组存服务端配置。
- **扫描根管理**：默认 TEMP 恒被扫描；`--work-dir` 是**追加**而非替换（可重复）；「⚙ 扫描目录」面板可在线增删自定义根（绝对路径、须存在；默认/CLI 根不可移除；失效目录以「已失效」列出且可移除）。扫描根、分组与显示状态互不影响 OCSR 证据。
- **维护**：复制 session ID 与 `--resume-from` 返工命令（实际执行仍走 CLI）。归档工作流：单卡/整组归档、批量「归档已结束」（当前筛选下所有非进行中卡片）把卡片移出主视图；「已归档」视图只看已归档项，可单张或批量恢复。归档只写浏览器 localStorage，不删任何证据文件。目录扫描默认近 7 天窗口（`--days` 可调），更老的记录随窗口与 TEMP 清理自然退出主视图；遥测历史展示最近 200 条。运行状态以 watcher 落盘证据为准：越过 `total_deadline` 或进程已退出但未验收的条目显示为灰色「滞留」，不冒充进行中；遥测中的 error/timeout 会把徽章校正为失败/超时。

端口：8787 被旧面板实例占用时自动 KILL 旧实例后同端口重启（经 `/api/identity` 识别）；被其他程序占用时依次探测 8788..8797。Windows 上禁用 `SO_REUSEADDR`（它会允许强行绑定已占用端口，使占用探测失效）。

## Git hook

仓库使用 `.githooks/pre-commit` 检查 OCSR 回归脚本并在检测到治理文档变更时输出独立复查提醒。`core.hooksPath` 应指向 `.githooks`。
