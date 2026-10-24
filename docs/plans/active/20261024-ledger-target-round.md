# ledger 增加 target_round 透传（converge 轮次可统计化）

- 状态：done（2026-10-24，独立 fresh reviewer ACCEPT，341 passed + verify 13/13）
- 实施 commit：`d6984a477777c2609b4478bce098d98da97461ac`
- 分支：`agent/ledger-target-round`
- 来源：Obsidian 库 converge 度量基线（`converge_metrics.py`）发现 3 个历史收敛目录的轮次信息仅存于 `ocsr-dispatch-ledger.jsonl`，而 ledger 无 `target_round` 字段，轮次不可统计。

## 目标

`dispatch --meta target_round=N` 成为识别键并透传到 `ocsr-dispatch-ledger.jsonl` 的所有相关行，使 converge 轮次在 ocsr 通道可机械统计。向后兼容：不传该键时行为与现状完全一致。

## 非目标

- 不改 converge-skill 仓的 `ocsr_spawn_adapter.py`（自动透传属后续任务）。
- 不改 pisr、不改 run --spec 语义、不动既有 ledger 行结构的其他字段。
- 不修改 SKILL.md 的运行规则正文（仅必要时更新 refs 文档用法示例）。

## 实施要点

1. `scripts/ocsr_dispatch.py`：
   - `cmd_dispatch` 的 `--meta` 解析处识别 `target_round`；**校验为正整数**，非法值（非数字、0、负数）fail-closed 报错退出（返回 1，不派发），避免静默丢字段。
   - 透传到 `launched` 行与 `_watch_loop` 等所有已透传 `converge_invocation_id` 的 ledger 行（同一路径、同一条件风格：有值才写入字段，无缺省）。
2. 测试（`tests/`，沿用合成配置、`OCSR_DISABLE_MODEL_CALLS=1`）：
   - 传 `target_round` 时 ledger 各事件行含该字段且为 int。
   - 非法值（`abc` / `0` / `-1`）拒绝派发且报错。
   - 不传时 ledger 行不含该字段（回归现状）。
3. 文档：
   - `refs/converge-integration.md` 的用法示例补 `--meta target_round=<N>` 及一句口径说明（字段供下游轮次统计，与 converge 主循环 round 编号一致）。
   - `docs/CURRENT.md` 顶部补一条完成记录（含测试数）。
   - `docs/CHANGELOG.md` 按仓内惯例追加。
4. 验证：`pytest tests/ -q` 全绿、`python scripts/verify_ocsr_skill.py` 全过、`python scripts/agent_links.py check`、`python scripts/audit.py check`、`git diff --check`。

## 验收

- 上述验证全过；新增测试覆盖正/反/兼容三类。
- 独立 fresh reviewer 复查 diff（重点：字段透传是否覆盖全部应覆盖的 ledger 行、非法值 fail-closed、无行为回归）。

## 交付边界

- 在 `agent/ledger-target-round` 分支提交；推送与 PR 需用户明确授权后另行执行。
- 合入 main 后由 cc-switch 单跳部署到各宿主技能目录。
