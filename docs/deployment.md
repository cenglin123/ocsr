# 本地运行与验证

OCSR 没有服务部署。这里的“运行”指在本机调用 OpenCode CLI，以及执行离线文档回归检查。

## 依赖

- Windows PowerShell；命令需兼容 Windows PowerShell 5.1。
- Python 3；现有验证脚本仅使用标准库。
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
python scripts/agent_links.py check
python scripts/audit.py check
git diff --check
git ls-files --eol | Select-String "w/crlf"
```

## Git hook

仓库使用 `.githooks/pre-commit` 检查三份 agent 入口一致性和 OCSR 回归脚本。`core.hooksPath` 应指向 `.githooks`。
