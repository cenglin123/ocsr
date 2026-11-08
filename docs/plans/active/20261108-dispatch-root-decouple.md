# OCSR 派发目录解耦与面板自动发现（handoff 20261108）

来源：Claude Code 会话「本机知识库记忆底座建设」转交，用户要求由 ocsr 侧修复。

## 问题
1. 面板看不到非 TEMP 的 --work-dir 派发（无登记关系，需手工加扫描根）。
2. --work-dir 被误当 worker 工作目录：实际 worker cwd=Path.cwd()（dispatcher 进程目录），--work-dir 仅是派发目录父目录；派发目录落进 git worktree 有污染风险。

## 方案
- **解耦**：派发目录默认放 TEMP（新增 --dispatch-root 显式覆盖父目录）；--work-dir 明确为 worker 执行目录（opencode --dir，默认 Path.cwd()）。converge_loop/dsh 均不使用 --work-dir，影响面仅限直接 CLI 用户；--resume-from 按绝对路径定位，不受影响。
- **自动发现**：dispatcher 启动派发时把派发目录父目录写入 ~/.ocsr/dispatch-roots.json（驱动器写、面板只读）；面板扫描根 = TEMP + CLI roots + 配置 roots + 登记根（近 30 天、存在目录、失效跳过）。

## 验收
- dispatcher：--work-dir 只影响 --dir（worker cwd），--dispatch-root 影响派发目录位置，登记文件写入正确。
- 面板：登记根自动并入、失效跳过、时间窗生效。
- 实测：真实派发验证登记 + 面板自动显示（现场样本 C:\wt\perf-001 / s7d-dispatch 只读观察，不动其文件）。
- 全仓回归 + verify 13/13 + audit + diff/eol。
- fresh review 1 位；PR squash 合并；更新 SKILL/docs 中对 --work-dir 与面板扫描范围的描述。

## 禁区
- 不终止/移动/修改正在运行的 perf-rework2-001、s7-verify-004 两个 worker，不改 C:\wt\ 下任何文件（只读观察）。
