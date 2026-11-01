# 输出编码与超时诊断修复

状态：complete（修复与本地验收完成；GitHub 交付采用特性分支 PR squash）。

## 证据与边界

MiMo 评审运行中 9 条 CLI 事件 JSON 损坏；CLI 完成事件停止更新后，原生会话直到终止前约 18 秒仍出现 write 调用的 streaming 元数据。六分钟总时限先到，不能据此断言模型卡死或 API 故障。原始证据在本机 `.review/super-agent-eval-20261101/`，不入库。

只修输出文本编码、解析质量可见性和有依据的阶段诊断。保留 Job 生命周期、重试计数、预算门、不可变总期限、完成/恢复契约；不自动延长时限、不自动重派、不改上层计划。

## 实施

1. launcher 明确原生命令的 UTF-8 编解码，并分离 stderr；以 CP936 环境下真实 native UTF-8 输出夹具复现与验证。
2. JSONL 解析按 LF 分帧，显式统计完整坏行、非事件行、未完成尾行和非法 UTF-8；旧日志保持原样，不猜测修复编码。
3. watcher 超时生成派生 `diagnostic.json`，报告最后可见阶段、事件年龄、日志质量、策略终止依据及未知根因；不将日志静默等同于模型卡死。
4. 新增只读 `diagnose --worker-dir ... [--session-db ...]`；dispatch 可显式传 `--diagnostic-session-db` 在检查点读取原生状态。默认不访问数据库。只用现有 session 绑定/进程身份核验过的 session ID 与本次 launcher 起始时间过滤 OpenCode v2 的 assistant 元数据；不回退到最新会话，不读取凭据，不输出内部推理或工具参数正文。未知 schema/锁/缺记录降级为明确 unavailable，不影响派发、验收或预算。
5. 离线回归、历史 MiMo 证据只读诊断、真实短调用冒烟、fresh 跨模型独立复查，更新 refs/deployment/pitfalls/CURRENT/CHANGELOG。

## 验收

- CP936 下中文、emoji、反斜杠和 JSON 内容往返正确，stderr 独立，session 绑定和 native exit marker 保留。
- 损坏 JSON/UTF-8 与尾行不被静默吞掉；U+2028 不切断 JSONL。阶段仅作有来源的观测，未知保持未知。
- SQLite 使用只读连接，只查询精确绑定且不早于本次启动的会话记录；合成 fixture 覆盖 wrong session、旧消息、未知 schema、失败降级和敏感内容不输出。
- 完整 pytest、verify、agent_links、audit、diff/eol/路径隐私检查与独立 fresh 复查通过。
- 模型调用上限 5 次，deepseek/deepseek-flash；副作用可验收的测试输出单独指定，不静默扩大预算。证据保留；GitHub 交付已获用户明确授权，按仓库 PR squash 流程执行。

## 验收结果与评审裁决

- `pytest tests/ -q`：407 passed；verify 13/13；agent_links、audit、diff/eol/新增路径隐私检查通过。
- CP936 原生进程正反对照通过；真实短调用 UTF-8 产物 88 字节、8 条事件全部可解析；历史 MiMo 文件哈希不变，精确会话回查能区分 CLI generation_pending 与 write tool_call_streaming/参数长度 0，根因 unknown。
- 模型实际 5 次：preflight、测试编写、真实短调用、两轮 fresh 静态复查。未超预算，未重跑 MiMo 长任务。
- 两轮静态复查原结论均为 REVISE，报告原样保留；以下有效发现已修补并由主会话运行新增回归验收，不宣称取得修补后的模型 ACCEPT。治理文档已经取得两次跨模型独立视角；未改预算、止损、恢复或产物验收准则。

| 独立发现 | 修复及验证 |
|---|---|
| stderr 分离导致 preflight 漏判 404 | 预检读取 stderr；离线 launcher fixture 验证 404 分类 |
| 未映射事件与无观测混淆 | reasoning_emitted / event_observed_unknown_phase 与 not_observed 分离 |
| assistant 完成标签与任务完成混淆 | assistant_step_complete / step_completed，仅步骤元数据，活动工具优先 |
| 非预期数据库类型使摘要无法序列化 | 类型/形状检查、标量投影、异常降级；BLOB、未知 schema、锁、坏 JSON、正文隔离回归 |
| CLI 工具状态提取与快照不一致 | 共用提取器；步骤边界保留未关闭观测及原始时效，较新数据库完成状态可关闭旧观测 |
| 诊断写失败仍显示文件路径、控制台漏报其他损坏 | 写入结果显式记账，终端路径仅在写成功时显示，四类质量信号均显示 |
| 没有原生工具记录时标成 unknown | 明确 not_observed；只对实际未知状态使用 unknown |

本机证据：`.review/output-diagnostics-20261101/` 下 `runtime-verification.json`、`review/independent.md`、`final-review/independent.md` 与 `review-tool-audit.json`。终审实际读路径无禁区、无 shell 命令；初审在写完自身报告后读取该报告并查询自身元数据，超出命令合同但没有取得其他答案，已如实记入审计，初审发现经主会话核对后采纳。
