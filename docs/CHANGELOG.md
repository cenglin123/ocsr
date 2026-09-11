# CHANGELOG

## 2026-09-11

- 默认白名单跟随 DeepSeek 更新：`deepseek/deepseek-v4-flash` → `deepseek/deepseek-flash`（V4.1 Flash，官方公告称旧 V4 Flash 已下线、仅暂时路由；2026-09-14 起 `deepseek-v4-pro` 也将被路由至该模型）。已按规则以 `opencode models --verbose` 核对本地池并 preflight 探测通道可用。
- 看门狗超时与 Session 恢复落地（计划：`docs/plans/active/20260911-ocsr-watchdog-session-recovery.md`）：检查期限与总期限分离（总期限 `timeout × (max_renewals+1)` 写入 `worker-state.json` 后不可变，续期默认 1 次）、worker 单向状态机、`artifact_seen` 与 `landed` 分离；launcher 以 `--format json` 流式解析顶层唯一 `sessionID` 写 `session-binding.json`（缺失/歧义只禁用续接，不改真实退出码）；恢复仅由上层以新 reserve/settle 显式发起，驱动器只产 `resume-material.json`（含 `old_process_stop_verified`、`requires_settle`）；并发 DB 锁从「延迟 30s 自动重试」改为 `recovery_required` 停止交回上层。新增 7 个遥测可选字段，`refs/dispatch-patterns.md` 同步模板与「检查期限、续期与总期限」「session 绑定与恢复材料」两节。
- allowlist 测试改为从 `ALLOWED_MODELS` 加载结果派生，不再在测试内复制模型清单（此前与 #7 合并的 4 模型配置矛盾致 3 例长期红）；`tests/test_audit.py` 临时目录先 `resolve()`，修复 Windows 8.3 短路径下 `relative_to` 假阳性（结构链接测试在本机长期误红）。

## 2026-09-06

- audit 工具词边界修复：drift 检查的子串匹配会把 "windows"（含 "ws"）误判为"文档提到 WebSocket"，自 #3 引入 package.json（manifest 出现）激活该检查起持续误报。改为 `\b` 词边界正则（`DRIFT_KEYWORD_RES` + `_mentions`），tests/test_audit.py 补 2 个回归用例；pisr 仓库同步修复。
- 模型选择规则收紧：默认白名单标注为作者本机模型池样例；每台机器首次使用前（及换机、通道异常归因后）先 `opencode models --verbose` 按本地池重配 `config/allowed-models.json` 再决定模型（SKILL.md、refs/model-defaults.md；与 pisr 同逻辑）。
- fresh 对抗评审补运行时验证合同（refs/failure-modes.md §2、SKILL.md 最小规范）：reviewer 以只读命令佐证时 prompt 须钉死禁止写入与修复（生成/评估分离），报告列明执行的命令与退出码；OCSR 无进程级工具白名单，命令只读性属事后审计面，需进程级硬约束的评审走 PISR。

## 2026-08-18

- 仓库重建为 public，历史起点。此前私有开发历史未随仓分发（脱敏摘要见 `DEVELOPMENT.md`）。
- watcher 失败语义分层：`_detect_name_mismatch` 区分「0 产物」与「产物命名与 pattern 不符」。
- 模型池移除 `deepseek/deepseek-v4-pro`（涨价），池收敛为 flash / mimo-v2.5 / mimo-v2.5-pro。
