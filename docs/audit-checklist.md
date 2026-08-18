# 文档一致性审计清单

## 机械检查

- [ ] `python scripts/agent_links.py check` 通过。
- [ ] `python scripts/audit.py check` 通过。
- [ ] `python scripts/verify_ocsr_skill.py` 通过。
- [ ] `git diff --check` 无空白错误。
- [ ] 受 Git 管理的文本文件没有意外 CRLF。
- [ ] `docs/STRUCTURE.md` 覆盖所有长期文档入口且链接有效。

## 语义检查

- [ ] `SKILL.md` 仍是 OCSR 行为规则的唯一事实源。
- [ ] 文档没有把 OCSR 描述成安全沙箱或通用工作流引擎。
- [ ] 当前模型、CLI 参数和编码说明有本机证据支持。
- [ ] `docs/CURRENT.md` 只记录当前状态，不复制完整计划或历史。
- [ ] active plan 的状态、owner、验证和交接信息足以让新会话继续。
- [ ] completed plan 与 `.converge/done` 的说法符合实际证据，不把失败归档包装成成功。
- [ ] 过时信息已更新或删除，没有“仅供历史追溯”的文档考古副本。

## 独立视角

治理文档变更完成后，让 fresh reviewer 只读指定语料回答：项目是什么、硬约束是什么、复杂任务从哪里开始、完成前必须验证什么。把 reviewer 的实际产物和调用证据保留下来，不由作者代写。
