# 模型默认池与分工 — 详细参考

> 本表由 [SKILL.md 的模型选择入口](../SKILL.md) 按需加载（数据层，随模型换代更新，对标 converge refs/model-tiers.md）。主文件保留选模型规则、遥测与翻转门槛。
>
> OCSR 的可用模型由仓库根目录的 [`config/allowed-models.json`](../config/allowed-models.json) 唯一决定。默认值是 `xiaomi/mimo-v2.5` 与 `xiaomi/mimo-v2.5-pro`——仅是作者本机模型池的样例，你的机器安装的 opencode 未必提供这些条目；首次使用先运行 `opencode models --verbose` 按本地模型池重配该 JSON，再决定模型。用户可修改该非空、无重复的 JSON 字符串数组，下一次命令启动时加载。未列出的模型不可选。

下表仅描述**仓库默认配置**下的角色分工；用户替换白名单后，必须选择当前配置中存在且已通过 preflight 的模型，不能沿用表中的已移除 ID。

按**角色**分工，不按任务表面难度：

| 角色 | 默认模型 | cost input/output ($/M tok) | 说明 |
|------|---------|---|---|
| 批量执行 worker（机械杂活：转换、摘要、矫正、抽取） | `xiaomi/mimo-v2.5` | 以 `opencode models --verbose` 为准 | MiMo 轻量档，配合 [SKILL.md 的自足 prompt、验收与止损](../SKILL.md)使用 |
| 轻量判断（外部文档摘要/非关键 evd 生成） | `xiaomi/mimo-v2.5` | 0.14 / 0.28 | MiMo 轻量档 |
| 判断密集角色（评审、verdict、语义审查、meta 判断） | `xiaomi/mimo-v2.5-pro` | 以 `opencode models --verbose` 为准 | 默认池只含 MiMo，不能伪装成跨 family 异构评议；需要异构视角时先由用户将经验证的模型加入配置。传给 `-m` 时用完整 ID |
