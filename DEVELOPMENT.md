# DEVELOPMENT.md — 开发沿革（脱敏摘要）

> 本仓库 2026-08-18 重建为 public。此前它在私有仓中开发；私有历史的完整 git 内容未随本仓分发——其中的 converge 证据现场、计划文档与机器状态属开发侧材料。本文件是那段历史的脱敏简要记述。

## 沿革要点

- **2026-07 上旬**：OCSR 立项——把 headless `opencode run` 作为框架无关的子代理执行后端，解决框架内建子代理「锁模型、无跨厂商、共享上下文污染评审」三个痛点。确立核心心智模型「上下文残差」与 prompt 六要素模板。
- **2026-07 中旬**：执行层硬化——脱管派发（launcher + 双监视观察器）、看门狗阈值、并行扇出纪律、失败切换阶梯（同模型重试→换 family→硬停，每 worker 最多 3 次）。
- **2026-07 下旬**：派发驱动器落地为默认路径（错峰/看门狗/快照比对/遥测内置）；与 converge 的 Spawn 后端对接（预算门与 Archive Contract 事件流端到端接线）；模型白名单强制化 + `OCSR_DISABLE_MODEL_CALLS` 测试 tripwire。
- **2026-08 上旬**：确定性步骤运行器 `run --spec`（hook/dispatch/pause/assert 四步封闭集合、模板推导、断点续跑）；层级指挥模式与 release executor 合同。
- **2026-08-18**：watcher 失败语义分层（`_detect_name_mismatch`：「0 产物」与「产物命名与 pattern 不符」分流）；**模型池移除 `deepseek/deepseek-v4-pro`**（涨价，成本兜不住），池收敛为 `deepseek-v4-flash` / `mimo-v2.5` / `mimo-v2.5-pro` 三个 qualified ID。

## 当前状态

- 全部测试绿（258 passed）；`verify_ocsr_skill.py` 离线校验通过
- 分发渠道：`cenglin123/cenglins-skills` 的 `skills/ocsr` 子目录（cc-switch 拉取源），本仓为其上游开发仓
- 已知边界见 `docs/pitfalls.md` 与 `refs/pitfalls-reference.md`
