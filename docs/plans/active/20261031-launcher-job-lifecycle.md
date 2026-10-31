# Windows launcher 后代生命周期修复

状态：done（修复与验收完成，未合入/部署宿主）。

## 问题与范围

真实 OCSR launcher 被单独强制终止后，私有 OpenCode server 与 MCP 后代仍存活；根 PID 消失后现有 `_kill_worker` 按身份校验拒绝清理。原始进程身份和时间序列保存在本机 `.review/mcp-lifecycle-20261031/`，不提交真实路径或运行数据。

只修 launcher 的后代生命周期：新增 Windows Job Object 支持，launcher 自己绑定并持有非继承句柄，启动 OpenCode 之前启用 KILL_ON_JOB_CLOSE。关闭 launcher 自动清理其 Job 内后代。dispatcher 不持有 Job，不改默认脱管、不自动重派、不修改预算及 resume 契约。非 Windows 不加载 Win32 helper。不是安全沙箱，不覆盖既存共享服务或经外部服务创建的进程。

## 实施

1. `scripts/ocsr_launcher_job.cs`：最小 Win32 Job helper，正确布局、错误码与句柄生命周期。
2. `scripts/ocsr_dispatch.py`：将 helper 嵌入生成的自包含 launcher，在 OpenCode 前绑定；失败写 error.log 与 exit=1。
3. Windows 离线真实进程回归：正常退出、仅杀 launcher、多层后代、兄弟进程不受影响、dispatcher 退出后 worker 存活、绑定失败不派发。
4. 修复后真实 OpenCode 正常/异常探测；独立 fresh 跨模型复查；更新 pitfalls/deployment/CURRENT/CHANGELOG。

## 验收

- 新回归先证明旧版异常退出残留，再证明新版正常/异常清理；禁止连接真实 provider 的离线夹具。
- pytest、verify_ocsr_skill、agent_links、audit、diff/eol/新增路径隐私检查通过。
- 真实测试仅清理本轮 PID + 创建时间匹配的进程，不碰其他会话和共享服务。
- 模型上限 4 次：deepseek/deepseek-flash，用于真实正常/异常验证、独立 fresh 复查及一次必要的修订复查；不超限、不自动重派。
- 不直接提交 main，不自动发布或部署各宿主技能副本。

## 结果

- 旧实现离线回归先复现正常/异常退出残留；修复后 6 项进程回归、完整 347 tests 通过。
- `verify_ocsr_skill.py` 13/13、`agent_links.py check`、`audit.py check`、diff/eol 与新增路径隐私检查通过。
- 真实 OpenCode 2.2.1 / Win10 x64：正常退出 14 个、仅杀 launcher 后 13 个追踪进程全部消失；本轮清理后无追踪进程残留。其他会话和共享服务未终止。
- fresh reviewer 第一次 exit=1、无产物，未采信；第二次新会话报告 ACCEPT、无 P1/P2，11716 bytes，另以临时夹具测 ABI/非继承句柄和去掉绑定后的失败敏感性。源码之外的临时复查脚手架不作为已入库回归项。
- 采纳 P3 中的测试清理保护与真实原生 API 失败覆盖；保留既有 marker-based 错误契约，生产修复代码在独立复查后未再修改。
- 实际模型调用 4 次（正常预检、异常测试、两次 fresh review），没有超预算或自动模型重派。证据保留于本机 `.review/mcp-job-fix-20261031/`，不入库。
- 未在 Win11、x86、ARM64 或受限服务账号上实测；不承诺沙箱能力或外部服务代创建进程的归属。
