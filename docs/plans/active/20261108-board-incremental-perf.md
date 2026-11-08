# OCSR Board 增量扫描与生命周期（handoff 20261108-ocsr-board-perf）

来源：`C:\wt\handoff\20261108-ocsr-board-perf.md`（记忆底座会话转达）。

## 问题
1. 周期性满载：TTL 到期后 /api/state 仍全量重扫（7 天目录 × 每 worker 多文件 + dispatch-log 尾部 2MB），本机实测每 ~18s 一次 3–4s 单核 94–95% 尖峰；用户游戏时可感知卡顿。
2. 遗留重复实例：孤儿面板进程不随启动会话退出。

## 方案
- **增量扫描**：worker 目录以关键文件（worker-state/session-binding/start.marker/launcher.ps1/run.log）的 mtime+size 指纹缓存 scan_worker_dir 结果；指纹未变则复用，动态字段（进行中时长）每次重算。dispatch-log.jsonl 记住上次偏移，只解析新增行（size 未变直接复用缓存行列表）。
- **空闲自动退出**：--idle-timeout（默认 30 分钟无 /api/state 请求即退出，0=禁用）。
- **单实例**：~/.ocsr/board.lock（pid+port），重复启动时经 identity 识别后复用或 KILL 重启（保持既有重启语义）；写锁文件，退出时清除。
- 前后对比：同一组派发目录的下一次重建耗时与 CPU（冷/热/无变化）。

## 验收
- 新增离线测试（指纹缓存命中/失效、增量日志、空闲退出、锁文件复用/重启）。
- 实测：无变化时 /api/state 重建从秒级降到近零；CPU 尖峰消失；重复启动无多实例；空闲超时自动退出。
- 全仓回归 + verify 13/13 + audit + diff/eol。

## 交付
特性分支 → PR squash；新鲜独立复查 1 位（预算 2：1 preflight + 1 派发）。
