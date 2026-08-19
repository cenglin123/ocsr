# OCSR

OCSR（OpenCode Subagents Run）把 headless `opencode run` 用作独立于宿主框架的子代理执行后端，适合跨厂商异构模型、廉价批处理 worker 和 fresh-context 对抗评审；亦提供层级指挥模式（planner / orchestrator / worker 分层编排，见 [层级指挥专题](refs/hierarchical-command.md)）。

## 快速开始

前置条件：Windows PowerShell、Python 3，以及已配置模型 provider 的 OpenCode CLI。

```powershell
opencode models
opencode run "请完成一个自包含任务" -m deepseek/deepseek-v4-flash
python scripts/verify_ocsr_skill.py
```

运行规则、prompt 六要素、产物回收和模型分工以 [SKILL.md](SKILL.md) 为准。

## 文档

- Agent 协作入口：[AGENTS.md](AGENTS.md)
- 文档导航：[docs/STRUCTURE.md](docs/STRUCTURE.md)
- 详细参考（渐进式披露，按需加载）：[refs/](refs/) — 层级指挥、派发模式、converge 对接、模型默认池、失败模式、陷阱参考、release executor
- 当前状态与交接：[docs/CURRENT.md](docs/CURRENT.md)
- 设计边界：[docs/overview.md](docs/overview.md)
- 本地运行：[docs/deployment.md](docs/deployment.md)
- 已知陷阱：[docs/pitfalls.md](docs/pitfalls.md)

## 验证

```powershell
python scripts/verify_ocsr_skill.py
python scripts/agent_links.py check
python scripts/audit.py check
```

本仓库的核心产物是 skill 文档和机械验证脚本，不提供安全沙箱、常驻服务或通用运行时 wrapper。
