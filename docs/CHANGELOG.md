# CHANGELOG

## 2026-08-18

- 仓库重建为 public，历史起点。此前私有开发历史未随仓分发（脱敏摘要见 `DEVELOPMENT.md`）。
- watcher 失败语义分层：`_detect_name_mismatch` 区分「0 产物」与「产物命名与 pattern 不符」。
- 模型池移除 `deepseek/deepseek-v4-pro`（涨价），池收敛为 flash / mimo-v2.5 / mimo-v2.5-pro。
