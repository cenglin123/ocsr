# dsh 集成（适配器）事实源

dsh 适配层是 OCSR 的“技能级原生”接入面：让本仓库以一个 Cordis 插件 bundle 的形式被 dsh profile 安装，在任意工作目录下被 dsh 技能系统发现并加载。它不复制 OCSR 核心语义或技能正文；`SKILL.md` + `scripts/ocsr_dispatch.py` 仍是唯一事实源。

## 安装

- 本地：`dsh plugin --profile <p> add .`（在仓库根目录运行；`dsh` 会把相对路径锚定到当前目录）。
- 发布：`dsh plugin --profile <p> add @ocsr/dsh-ocsr`。

安装后 `dsh.profile.bundles` 自动加入 `@ocsr/dsh-ocsr`；`package.json` 的 `dsh.bundle.patch` 指向 `cordis.patch.yml`，在 profile 层插入 `ocsr-skill` 行（Phase 1 仅这一行；`ocsr-tools` 属 Phase 2）。

## 配置

- 模型白名单仍由 `config/allowed-models.json` 唯一决定（仓库默认 `xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro`）。该文件相对包根目录解析，dsh 安装后仍随包携带。
- dsh 侧无需为技能加载额外配置；`skill-filesystem` 的 `customSkillDirs` 不是本接入路径。若要覆盖模型白名单，直接编辑已安装包根的 `config/allowed-models.json`（注意重装会覆盖，需通过覆盖/分发维护差异化）。

## 边界

- **host 平面 runtime provider**：本适配在 `ctx.skills.registerProvider(...)` 注册，provider 对象名 `ocsr`（Cordis 插件名 `ocsr-skill`）。它只负责让技能目录/元数据在 host 平面 `skills` registry 存在且可达；`resourceBase` 为 `{ kind: 'directory', path: <包根目录> }`，`scripts/`、`config/`、`refs/` 相对引用可解析。
- **`tool-skill` 挂载条件（重要）**：在 `web` profile 下，“模型调用 skill 工具”还要求当前激活的 agent preset 挂载 `tool-skill`。实测（当前 `@deepseek-ai/dsh` 0.1.1-rc.2 checkout 的 `config/agent-presets/*/agent.cordis.yml`）：`minimal` **不**挂载 `tool-skill`；`standard` 与 `code` preset **都**挂载（两者均含 `skill-filesystem` 与 `tool-skill`）。provider 只保证技能存在，不保证任意 preset 向模型暴露 skill 工具。
- **不改核心语义**：适配层只做映射/包装，不复制派发语义；`scripts/ocsr_dispatch.py` 是编排的唯一实现。

## 版本锁定

- 本 Phase 1 以 `@deepseek-ai/dsh` `0.1.1-rc.2` 全局安装 checkout 实测验证（路径 `C:\\Users\\Administrator\\AppData\\Roaming\\npm\\node_modules\\@deepseek-ai\\dsh`）。宿主 API（`skills` registry、`registerProvider(control)`、candidate/definition 契约）以该版本为准；升级宿主时先复核这些接口再保留本适配。
- `package.json` peerDependencies 锁 `@deepseek-ai/dsh-skill ^0.1.1-rc.2`、`@deepseek-ai/cordis ^4.0.1`。

## Phase 2 / 3 方向

- Phase 2（一等工具）：`lib/tool-ocsr.js` 用 `defineTool` 注册 `ocsr_dispatch`，schema 参数映射为 `python scripts/ocsr_dispatch.py ...`；`cordis.patch.yml` 追加 `ocsr-tools` 行。编排语义仍在 python。
- Phase 3（深度原生）：通过 `ctx.jobs` 把长派发提升为 dsh 可跟踪 background job、把 worker 状态投影到 session；依赖 dsh jobs API，须先确认接口再动。

## 验证

- `python scripts/verify_ocsr_skill.py`
- `python scripts/agent_links.py check`（协议：先在 `AGENTS.md` 改动后运行 `repair` 同步 `CLAUDE.md`/`GEMINI.md`）
- `python scripts/audit.py check`
- `pytest tests/ -q`（基线：257 passed + 1 已知环境相关失败 `TestPidCaptureAndKill.test_kill_actually_terminates_process`，Windows `taskkill` 对测试启动的 PowerShell 子进程返回 `Access denied`）
- `git diff --check`
- 发布包完整性：`npm pack --dry-run`，核对包含 `cordis.patch.yml`、`SKILL.md`、`scripts/`、`config/`、`refs/`、`lib/index.js`。
- 非仓库目录 `/skill ocsr`（或模型 skill 工具，后者须 preset 挂载 `tool-skill`）可加载，`resourceBase` 为目录对象。
