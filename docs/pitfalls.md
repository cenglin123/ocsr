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

## 调用成本

主模型的内建子代理通常继承主模型，可能快速消耗高价 usage。需要廉价执行或异构 reviewer 时使用 OCSR 指定模型；派发前披露调用数上限，失败不能无限重派。

harness 前台超时不足、后台通道 kill、模型端静默停滞的具名故障模式与止损协议（脱管派发、看门狗硬阈值、失败切换阶梯）见 [`refs/dispatch-patterns.md`](../refs/dispatch-patterns.md) 的“失败看护与切换”与“脱管派发模式”——本文件不重复正文。

## 上层流程对齐

OCSR 自身不维护独立 converge 轮次。当上层是 converge 时，旁路 spawn、inline reviewer 或未进入 ledger 的调用都会破坏审计完整性。
