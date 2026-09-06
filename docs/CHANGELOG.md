# CHANGELOG

## 2026-09-06

- 模型选择规则收紧：默认白名单标注为作者本机模型池样例；每台机器首次使用前（及换机、通道异常归因后）先 `opencode models --verbose` 按本地池重配 `config/allowed-models.json` 再决定模型（SKILL.md、refs/model-defaults.md；与 pisr 同逻辑）。
- fresh 对抗评审补运行时验证合同（refs/failure-modes.md §2、SKILL.md 最小规范）：reviewer 以只读命令佐证时 prompt 须钉死禁止写入与修复（生成/评估分离），报告列明执行的命令与退出码；OCSR 无进程级工具白名单，命令只读性属事后审计面，需进程级硬约束的评审走 PISR。

## 2026-08-18

- 仓库重建为 public，历史起点。此前私有开发历史未随仓分发（脱敏摘要见 `DEVELOPMENT.md`）。
- watcher 失败语义分层：`_detect_name_mismatch` 区分「0 产物」与「产物命名与 pattern 不符」。
- 模型池移除 `deepseek/deepseek-v4-pro`（涨价），池收敛为 flash / mimo-v2.5 / mimo-v2.5-pro。
