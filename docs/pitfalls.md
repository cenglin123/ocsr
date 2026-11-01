# 已知陷阱

## 安全边界

`--dir` 只设置工作目录，不是沙箱。Prompt 禁令和输出路径审计只能降低误访问风险，不能阻止恶意模型越界读取或外发数据。敏感材料需要宿主层低权限账户、文件系统隔离和出站网络限制。

## 证据真实性

- 子代理说“完成”不等于文件已写入。
- reviewer 报告由 orchestrator 代写，不构成独立评审。
- 目录被复制到 `.converge/done`，不等于归档脚本和 manifest 契约通过。
- invocation 的 started/completed 事件共享 invocation ID 可能是正常配对，诊断重复证据时必须按 schema 判断。

## Windows 编码

PowerShell 5.1、PowerShell 7、终端显示和文件字节是不同层。先以 UTF-8 显式重读并检查原始字节，再决定是否修复文件。不要用 `>` 生成需要被其他工具读取的 UTF-8 文本。

原生命令输出经过 PowerShell 管道时，文件写入 UTF-8 不等于上游解码正确：`[Console]::OutputEncoding` 决定原生 stdout 的解码；`$OutputEncoding` 控制通过管道向原生命令发送字符串时的编码，launcher 将两者设为 UTF-8。JSONL 只按 LF 分帧，不能用会切开字符串内 U+2028/U+0085 的 `splitlines()`。诊断分别统计完整坏 JSON、非事件行、非法 UTF-8 和未完成尾行；历史乱码不能靠替换解码静默修复。

CLI 只暴露部分事件，日志静止不能证明服务端停滞。超时表示时限策略触发；最后阶段与根因分开报告，缺乏证据时根因保持 unknown。只读诊断入口与数据库限制见 [deployment.md](deployment.md)「运行诊断」。

## 调用成本

主模型的内建子代理通常继承主模型，可能快速消耗高价 usage。需要廉价执行或异构 reviewer 时使用 OCSR 指定模型；派发前披露调用数上限，失败不能无限重派。

harness 前台超时不足、后台通道 kill、模型端静默停滞的具名故障模式与止损协议（脱管派发、看门狗硬阈值、失败切换阶梯）见 [`refs/dispatch-patterns.md`](../refs/dispatch-patterns.md) 的“失败看护与切换”与“脱管派发模式”——本文件不重复正文。

## opencode 常驻后台服务

opencode 2.x 的 `opencode run` 默认连接常驻后台服务（`opencode-cli serve --service`，桌面版启动或首个 run 拉起），服务为每个新工作目录常驻一对 MCP 子进程（配置里的 Playwright、cua 等），run 结束后不释放；派发目录因此被占用、无法删除（git worktree 任务目录尤其明显），且每派发一个新目录多一对进程。launcher 因此一律加 `--standalone`（私有服务器，随 run 退出；会话仍可用 `--session` 续接）。已残留的进程要 `opencode service restart` 或退出桌面版后结束服务才释放。

Windows launcher 在调用 OpenCode 前把自身加入带 `KILL_ON_JOB_CLOSE` 的 Job Object，唯一非继承句柄由 launcher 持有。launcher 正常或异常退出时，Job 内后代随之终止；dispatcher 退出不关闭该句柄，因此不影响脱管 worker。Job 初始化或绑定失败时写 `error.log` 与 `exit=1`，不调用 OpenCode。helper 源码嵌入生成的 launcher，运行中的 worker 不依赖技能目录后续更新。worker 内启动的后台进程也受此生命周期约束；需要长期保留的服务应由独立宿主管理。Job 不提供安全沙箱，不覆盖既有共享服务或经外部服务创建的进程；旧版已启动的 launcher 不会被追溯绑定。

## opencode 会话绑定原工作目录

`opencode run --session <id>` 打开的是会话创建时的工作目录。原目录被删除（例如 worktree 合入后清理）时，续接会以 `Location not found: <原目录>` 退出（exit=1），而不是在当前目录继续。launcher 因此把 worker 的工作目录写入 `session-binding.json` 的 `cwd`（绝对路径）；`--resume-from` 在启动前校验：目录不存在则拒绝并提示在原路径重建（如 `git worktree add <路径>`）或新开 worker；旧派发没有 `cwd` 记录时只提示无法核对，不阻止。当前 cwd 与原 `cwd` 不同时允许续接，但要在返工提示词里写明实际要改的目录。续接成功的新绑定沿用原 `cwd`。

## 上层流程对齐

OCSR 自身不维护独立 converge 轮次。当上层是 converge 时，旁路 spawn、inline reviewer 或未进入 ledger 的调用都会破坏审计完整性。
