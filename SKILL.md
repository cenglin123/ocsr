---
name: ocsr
description: Use when host subagents cannot provide cross-vendor models, cheap parallel workers, or fresh-context adversarial reviewers. Drives headless `opencode run` as a framework-independent subagent backend. "ocsr" is the PRIMARY trigger (any mention means use this skill); also trigger on "opencode run 子代理", "opencode 驱动多代理", "异构 reviewer", "多模型并行评审", or "spawn subagents via opencode". NOT for simple tasks whose prompt cost exceeds the work, or tasks needing shared conversational context.
---

# OCSR — 默认派发入口

OCSR（OpenCode Subagents Run）以 headless `opencode run` 派发可选模型的、一次性 fresh-context 子代理。它是执行后端：任务拆解、预算裁决、verdict 与最终验收仍由顶层 agent 负责。

## 先判断是否派发

适用于需要跨厂商模型、低成本批量 worker、或独立 fresh-context 评审的可验证工作。首次在新 harness 使用时，**价值前提**是派发链路已在本机验证；此时主要价值是异构视角与上下文隔离，不应承诺净省 token。

不要派发单次简单工作（prompt 与回收成本超过工作本身）、需要频繁共享上下文的协作，或无法事后验收的关键判断。评审通常需要跨 family；纯确定性、很小的修复可用原生 executor。converge 等治理流程中，任何通道都不得绕开预算门与证据链；未自动过门的通道须显式 `reserve`/`settle`。

子代理没有本对话的隐含约定；把缺失约束作为「上下文残差」写进 prompt。不要相信其“完成”自述，只相信指定文件的验收证据。

## 默认单 worker 闭环

### 1. 固定模型、调用上限与路径

可用 qualified ID 由用户可编辑的 [`config/allowed-models.json`](config/allowed-models.json) 唯一决定；仓库默认仅含 `xiaomi/mimo-v2.5` 与 `xiaomi/mimo-v2.5-pro`。修改该 JSON 后重新启动命令即可加载；它必须是非空、无重复、无首尾空白的 `provider/model` 字符串数组，格式错误会 fail-closed。`selftest` 未传 `--model` 时使用配置首项。仓库默认白名单来自作者本机模型池，其他机器安装的 opencode 未必提供相同模型——每台机器首次使用前（以及换机、通道异常归因后）先运行 `opencode models --verbose` 看本地实际有哪些模型，从模型块标题原样复制 qualified ID 更新 [`config/allowed-models.json`](config/allowed-models.json) 再决定用哪个；禁止凭 `id`、`providerID`、`name` 或裸名拼接 `-m`。派发前用 `python scripts/ocsr_dispatch.py preflight --model <qualified-id>` 验证选定模型可用性（会消耗真实模型调用）。模型角色与成本资料见 [`refs/model-defaults.md`](refs/model-defaults.md)。价格元数据（包括 `cost=0`）只是启发式风险信号，元数据可能缺失，不能单独证明模型能力或免费。

派发前向用户披露模型与调用总上限；未经新鲜授权不突破该上限。每个 worker 最多 **3 次总尝试**。有副作用但不能证明幂等性的任务，**禁止自动重派**。

为每个 worker 指定唯一、明确的绝对输出路径和唯一 label；`--output-pattern` 不会约束实际写入位置。输入、输出路径、`--dir`、prompt 禁令和路径审计都不是安全沙箱：它们只作 best-effort 协作约束，不能阻止恶意或失控进程访问/改写可访问位置。

### 2. 写自足 prompt

每份 prompt 必须包含六项：

1. **任务**：一句话目标与可验证验收标准。
2. **输入**：允许读取的绝对路径；明确其他位置禁读。
3. **输出**：唯一绝对产物路径；优先使用 Write，若无 Write 工具可回退到受控 shell 的 UTF-8 无 BOM 写入；未实际写入文件即失败。
4. **格式**：schema、模板或示例。
5. **边界与禁区**：不改输入、不写输出路径外；不确定术语保留原文并标 `[UNCERTAIN]`；知识截止可能早于今天；已确认术语不得“矫正”。
6. **执行证据**：返回产物完整路径、字节大小与工具调用情况。

路径约束不是安全隔离。长 prompt 用 UTF-8 文件读入，避免命令行转义；Windows 读取中文一律显式 UTF-8。

| 环境 | 默认输出/重定向风险 | 建议 |
|---|---|---|
| PowerShell 5.1 | `*>` 可为 UTF-16LE | 显式 `Get-Content -Encoding UTF8`，写入显式 UTF-8 |
| PowerShell 7 | 默认 UTF-8 | 仍显式 UTF-8，保证一致 |

### 3. 派发与看护

默认使用驱动器：

```powershell
python scripts/ocsr_dispatch.py dispatch --worker "<prompt>|<model>|<label>" --output-dir <dir> --output-pattern <unique-name> --watch
```

驱动器负责错峰、看门狗、输出存在性/快照比对与 telemetry（`dispatch-log`），不替代编排判断。前台 timeout 足够时优先前台运行。需要并发、`Start-Process` 脱管、双监视、静默停滞处理或完整失败切换阶梯时，读取 [`refs/dispatch-patterns.md`](refs/dispatch-patterns.md)。该文件的**脱管派发模式**定义 launcher、`Start-Process` 与双监视；其中的硬事实仍是：脱管进程必须设阈值；默认 15 分钟且可按 `max(10 分钟, 1.5 × 实测耗时)` 调整；模型端静默停滞须按完整指纹裁决。`harness 前台超时 < 单轮耗时` 时也不能以无限轮询代替看护。用至少 **≥5 次** 同类遥测样本再调整默认模型或阈值，不能以个例翻转默认。

失败切换阶梯、切换 family 与通道例外的操作细则在该专题；达到三次上限则停止并交回用户。`--fork` 不是默认路径；若使用，必须同时有 `--continue` 或 `--session`。

### 4. 回收并验收

依次检查：

1. 每个期望文件存在；
2. 每个文件非空；
3. 数量与期望一致；
4. 抽样打开 1–2 个文件，核对内容和格式。

任何一项失败都不采信“完成”回复。仅在无副作用或已证明幂等时重派，并把失败原因写入下一份 prompt；遵守三次总尝试上限。

## 按需加载的进阶专题

主文件是全局政策、边界和默认路径的唯一入口；下列文件仅在其委托范围内定义操作细节，不得放宽本文件不变量。

| 当你实际需要 | 读取唯一专题 | 该专题的闭环 |
|---|---|---|
| 并发、脱管、手写 launcher、失败看护 | [`refs/dispatch-patterns.md`](refs/dispatch-patterns.md) | 启动/观察、看门狗、回收与终止前置检查 |
| fresh 对抗评审 | [`refs/failure-modes.md`](refs/failure-modes.md) | 输入最小化、布局隔离、禁读、`reads:` 审计、作废或新会话重派 |
| converge Spawn 后端 | [`refs/converge-integration.md`](refs/converge-integration.md) | reserve/settle、invocation 事件、provenance 诚实降级 |
| 多步骤、路由、断点续跑 | [`refs/run-spec.md`](refs/run-spec.md) | schema、journal 与确定性路由；仅已成功提取却未命中具名 route 的值经 `"*"` 进入 `pause`，契约失败 fail-closed |
| 层级指挥 | [`refs/hierarchical-command.md`](refs/hierarchical-command.md) | 归属、状态、看护和独立验收 |
| 发布 executor | [`refs/release-executor.md`](refs/release-executor.md) | 输入合同、manifest 与保护默认 |

对抗评审的最小规范：只给完成审查所需的输入；把被审产物与 reviewer 输出隔离；要求结构化 `reads:`；reviewer 以只读命令做运行时验证佐证时，prompt 须钉死禁止写入与修复、报告列明执行的命令与退出码；审计发现提前获得答案或作弊性读取时，verdict 默认作废并以新会话重评。具体布局、禁读清单、命令执行合同、审计裁定及例外都在 `refs/failure-modes.md`。

`run --spec` 只搬运确定性步骤，不写 prompt、不判 verdict。其 schema、步骤类型、模板、journal 或提取契约失败均 fail-closed；只有已成功提取的值未命中具名 route，才会经必填 `"*"` pause 交回 agent。

## 维护与验证

权威实现为 `scripts/ocsr_dispatch.py`。常规验证：

```powershell
python scripts/verify_ocsr_skill.py
python scripts/agent_links.py check
python scripts/audit.py check
pytest tests/ -q
```

未经重复、可观察的失误证据，不新增运行时机制。完整背景、编码与诊断资料按上表按需读取。
