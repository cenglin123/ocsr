# 20260922 · 模型白名单本地化 + 深度审计修复

状态：已执行（2026-09-22 全部落地并复验通过；PR 合并后按治理规则清理本计划）
来源：2026-09-22 综合深度审计；M1 设计由用户拍板，其余按审计修复方案执行。

## 背景

深度审计发现：dsh 工具面用 node 跑 Python 驱动（致命）、4 个测试硬编码模型 ID 随用户配置变红、默认白名单描述漂移 ×4、错误码撞车、`agent_links repair` 门槛与工作流矛盾、5 个已完成 plan 滞留 active/、计划引用死链、路径隐私违规、陈旧基准数字。

## 用户决定（M1 重设计）

`config/allowed-models.json` **排除 git**（名单经常变化，用户本地手工维护）：

- **文件缺失或为空数组 = 未配置**：首次使用时向用户展示 `opencode models --verbose` 结果并**询问**启用哪些模型，写入该文件后再派发。
- **非空 = 直接使用**，不再询问。
- 文件存在但格式非法（坏 JSON / 重复 / 首尾空白 / 非 `provider/model`）仍 fail-closed。

「询问用户」发生在 agent 对话层（SKILL.md 行为规则）；脚本层保持非交互，未配置时 fail-closed 并打印可执行指引——确定性归脚本、交互归 agent。

## 工作项

### P0-a · dsh 工具面 spawn 修复

- `lib/tool-ocsr.js`：`spawn(process.execPath, ...)` 改为 Python 解释器（`OCSR_PYTHON` 环境变量覆盖，默认 `python3`→`python` 探测）；注释同步。

### P0-b · 测试与用户配置解耦

- 测试不再依赖真实 `config/allowed-models.json`（gitignore 后新克隆/CI 无该文件）：
  - 4 个硬编码正例测试（`test_ocsr_dispatch.py` TestForbidBlockInjection ×2、`test_run_spec.py` TestCli ×2）注入合成白名单 / 派生自注入值。
  - `TestModelAllowlist` 中依赖真实加载结果的断言改为临时配置注入。
- 原则修正：CHANGELOG 2026-09-11 的「从 ALLOWED_MODELS 派生」在「配置为空合法」的新设计下不再充分，升级为「测试自带合成配置」。

### P1-a · 白名单加载语义（未配置态）

- `scripts/ocsr_dispatch.py`：缺失/空 → `ALLOWED_MODELS=frozenset()`、`DEFAULT_MODEL=None`；需要模型的命令（dispatch/selftest/preflight/run-dispatch-step）在未配置时 fail-closed，报「白名单未配置」+ 配置指引；非法配置仍硬错。
- `lib/settings-ocsr.js`：缺失/空对齐为「未配置」（回退 driver 语义）。
- `scripts/verify_ocsr_skill.py`：allowlist 检查接受未配置态（输出 unconfigured 提示）为 PASS。

### P1-b · SKILL.md 与文档同步（M1）

- SKILL.md §1：删「仓库默认仅含…」；写入未配置询问流与非空直用规则。
- `refs/model-defaults.md`：去钉死默认池；「默认池只含 MiMo」删除；角色分工表改为按角色选档规则 + 作者本机样例标注。
- `refs/dsh-integration.md`：白名单描述、npm 打包行为（`config/` 移出 `files`，包不再携带发布者名单）、settings 门仅 dsh 工具面生效的边界事实、tool 层错误码表。
- `docs/CURRENT.md` / `docs/CHANGELOG.md`：收尾同步。

### P1-c · `agent_links repair` 工作流矛盾（M3）

- 目标内容漂移不再要求 `--force`（CLAUDE.md/GEMINI.md 按设计就是派生文件，repair 覆盖即其语义）；打印变更摘要。`--force` 保留兼容。

### P1-d · tool 层错误码去撞车（M2）

- `lib/tool-ocsr.js` 工具层自判错误改用 `4`（工具层拒绝：settings 门/模型门）、`5`（无法启动 driver）；driver 子进程退出码原样透传（0/1/2/3 契约不变）；`dsh-integration.md` 登记。

### P2 · 清理

- 删除 5 个已完成 active plan（Git 保留历史；治理规则「过时文档删除，不保留考古副本」）；同步 `docs/CHANGELOG.md`、`docs/CURRENT.md`、`refs/run-spec.md`、`scripts/ocsr_run_spec.py` 中的 plan 路径引用（`20260810-deterministic-run-spec.md` 为从未入库的死链，改指 `refs/run-spec.md` 自身）。
- `scripts/audit.py` birth-record 候选去掉 `docs/plans/completed/` 悬空项。
- `refs/dsh-integration.md:25` 真实用户路径 → `<user-home>` 占位。
- 陈旧基准数字（`DEVELOPMENT.md`、`dsh-integration.md`）按最终测试结果刷新。
- `lib/projection-ocsr.js`：schema 必填与 fold 条件省略对齐（可选字段）。
- `lib/tool-ocsr.js`：timeout 类 schema 数值改 integer。

## 验收

- `python scripts/verify_ocsr_skill.py` 全过。
- `python scripts/agent_links.py check`、`python scripts/audit.py check` 全过。
- `pytest tests/ -q` 全绿，且**在无 config/allowed-models.json 的临时环境可全绿**（测试自包含）。
- `git diff --check`、无意外 CRLF。
- 手工核验：删/空 `config/allowed-models.json` 时命令报「未配置」而非崩溃；非法 JSON 仍硬错。

## 交接与风险

- 本地工作区原有 `config/allowed-models.json` 改动（mimo-v2.6）保留不入库；`git rm --cached` 只解除跟踪。
- SKILL.md 模型选择规则属行为规则重定义：按 AGENTS.md 治理条款，完成后应取得独立视角复查证据（fresh reviewer 只读问答）；需模型调用，派发前须向用户披露预算并获授权。
- 合并走特性分支 PR squash（不直接推 main）。
