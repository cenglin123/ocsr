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

## 文档体系检查

```powershell
python scripts/agent_links.py check
python scripts/audit.py check
git diff --check
git ls-files --eol | Select-String "w/crlf"
```

## Git hook

仓库使用 `.githooks/pre-commit` 检查三份 agent 入口一致性和 OCSR 回归脚本。`core.hooksPath` 应指向 `.githooks`。
