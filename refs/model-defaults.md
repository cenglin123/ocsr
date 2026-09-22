# 模型池与分工 — 详细参考

> 本表由 [SKILL.md 的模型选择入口](../SKILL.md) 按需加载（数据层，随模型换代更新，对标 converge refs/model-tiers.md）。主文件保留选模型规则、遥测与翻转门槛。
>
> OCSR 的可用模型由用户本地的 [`config/allowed-models.json`](../config/allowed-models.json) 唯一决定。该文件**不入库**（模型池随机器与时间变化）；缺失或为空即「未配置」，首次使用前先询问用户启用哪些模型，之后由用户手动维护。它必须是无重复、无首尾空白的 `provider/model` 字符串数组，格式错误 fail-closed；未列出的模型不可选。`OCSR_ALLOWED_MODELS_PATH` 环境变量可覆盖配置文件路径（测试与多套配置场景）。

下表按**角色**分工，不按任务表面难度；具体 ID 以本机 `opencode models --verbose` 与本地白名单为准，不钉死任何默认池：

| 角色 | 选档规则 | 说明 |
|------|---------|------|
| 批量执行 worker（机械杂活：转换、摘要、矫正、抽取） | 廉价执行档 | 配合 [SKILL.md 的自足 prompt、验收与止损](../SKILL.md)使用 |
| 轻量判断（外部文档摘要/非关键 evd 生成） | 廉价执行档或轻量判断档 | 成本敏感场景优先便宜档 |
| 判断密集角色（评审、verdict、语义审查、meta 判断） | 深度思考档 | 评审通常需要跨 family 异构视角；同族池不能伪装成异构评议，需要异构时把经验证的其他厂商模型加入本地配置。传给 `-m` 时用完整 ID |

换机、或把失败归因到通道后：重新运行 `opencode models --verbose`，按本地实际模型池手动维护 [`config/allowed-models.json`](../config/allowed-models.json)，再以 `preflight --model <qualified-id>` 验证。价格元数据只是启发式风险信号，不单独证明模型能力或免费。
