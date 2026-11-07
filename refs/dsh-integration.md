# dsh 集成（适配器）事实源

dsh 适配层是 OCSR 的“技能级原生”接入面：让本仓库以一个 Cordis 插件 bundle 的形式被 dsh profile 安装，在任意工作目录下被 dsh 技能系统发现并加载。适配层只做映射/包装，不复制 OCSR 核心语义或技能正文；`SKILL.md` + `scripts/ocsr_dispatch.py` 仍是唯一事实源。

## 安装

- 本地：`dsh plugin --profile <p> add .`（在仓库根目录运行；`dsh` 会把相对路径锚定到当前目录）。
- 发布：npm 包名 `dsh-ocsr`（**尚未发布**，待 npm 账号可用时执行 `npm publish`）；当前安装用 GitHub 源 `dsh plugin --profile <p> add github:cenglin123/ocsr`。
- 安装后 `dsh.profile.bundles` 自动加入 `dsh-ocsr`；`package.json` 的 `dsh.bundle.patch` 指向 `cordis.patch.yml`，在 profile 层插入两个 entry：`ocsr-skill`（`dsh-ocsr`，技能 provider）与 `ocsr-tools`（`dsh-ocsr/tool`，派发工具）。
- **版本门（dsh 0.2.2）**：安装预检只读插件 manifest，不加载插件代码。`engines.dsh` 存在时它是唯一依据（本包的 `@deepseek-ai/dsh*` peer 因此不参与判定），否则逐个检查 dsh peer。不满足时安装命令拒绝并把不兼容项写进诊断；要强行安装须 `dsh plugin --profile <p> allow-version <name>@<version> --dsh-version <dsh-version> --accept-risk` 做精确版本豁免（等于自行承担崩溃/数据损坏风险，不建议）。已安装但不兼容的 bundle 在加载期被 skip，stderr 打印 `skipping profile bundle ...`，不会以旧契约静默运行。本包声明 `engines.dsh: ^0.2.2-rc.1`。

## 配置

- **模型白名单的唯一事实源是 driver 的用户本地文件**：`~/.ocsr/allowed-models.json`。路径优先级与 `scripts/ocsr_dispatch.py` 完全一致：`OCSR_ALLOWED_MODELS_PATH` > `OCSR_HOME/allowed-models.json` > `~/.ocsr/allowed-models.json`。该文件不入库、不随 npm 包分发（`package.json` 的 `files` 不含 `config/`），每台机器各自维护；缺失、空、非法 JSON、非 `provider/model` 或重复条目一律 fail-closed。
- **dsh 侧只有只收窄门**：`ocsr-tools` entry 的 `config.allowedModels`（`provider/model` 字符串数组，schemastery `z.object({ allowedModels: z.array(z.string()).default(undefined) })`）。宿主按插件 Config 校验后作为 `apply(ctx, config)` 第二参传入；在 profile patch 里给 `ocsr-tools` 写 `config`。该字段不是 `volatile()`，0.2.2 的设置表单只投影 volatile 字段，因此它不会出现在设置 UI，只能改 profile patch。三种语义：**省略（默认）= 完全交给 driver 的 `~/.ocsr/allowed-models.json`**；**显式空数组 = 全关**；**非空数组 = 与 driver 白名单取交集**，只能减少可选模型，绝不放宽 driver 规则。不在交集内或无法解析的 worker 由工具层以 `code 4` 返回。读取逻辑在 `lib/model-gate.js`，采用与 driver 相同的路径优先级。`.default(undefined)` 是承重的：schemastery 对省略的数组字段默认补 `[]`，去掉它会让所有未配置的 profile 静默全关。
- dsh 侧无需为技能加载额外配置；`skill-filesystem` 的 `customSkillDirs` 不是本接入路径。

## 边界

- **host 平面 runtime provider**：`lib/index.js` 在 `ctx.skills.registerProvider(...)` 注册，provider 对象名 `ocsr`（Cordis 插件名 `ocsr-skill`）。它只负责让技能目录/元数据在 host 平面 `skills` registry 存在且可达；`resourceBase` 为 `{ kind: 'directory', path: <包根目录> }`，`scripts/`、`refs/` 相对引用可解析。0.2.2 的 provider 契约（`registerProvider(control)`、candidate/definition、`BUNDLED_SKILL_RANK`、`control.invalidate()`）实测兼容，`lib/index.js` 未改。
- **`tool-skill` 挂载条件（重要）**：在 `web` profile 下，“模型调用 skill 工具”还要求当前激活的 agent preset 挂载 `tool-skill`。实测 dsh 0.2.2-rc.1（`@deepseek-ai/dsh-web-app` 的 `presets/*.patch.yml`）：preset 名单为 `minimal`、`standard`、`ptc`、`cordis`（旧 `code` 已移除，新增 `ptc`）；`minimal` **不**挂载 `tool-skill`，`standard`、`ptc`、`cordis` 均挂载（并含 `skill-filesystem`）。provider 只保证技能存在，不保证任意 preset 向模型暴露 skill 工具。
- **一等工具**：`lib/tool-ocsr.js` 用 `defineTool` 注册 `ocsr_dispatch`（`inject=['tools']`、`ctx.tools.register(...)`），把 schema 参数映射为 `python scripts/ocsr_dispatch.py dispatch ...`；编排语义只在 Python（薄壳）。`package.json` 的 exports 只暴露 `.` 与 `./tool`，对应上述两个 entry。
- **background 契约（0.2.2 jobs）**：`background:true` 时经 `ctx.get('jobs')` 取 jobs 服务并 `jobs.start({ kind, label, owner?, run })`；`run(job)` 内用 `job.append(...)` 写 registry 输出环（stderr 用 `{ channel: 'stderr' }`），返回 `{ cancel, done }`，`done` 解析 `{ status: 'completed' | 'killed' | 'failed', detail? }`；`job_output`/`job_kill` 读到的就是同一输出环。`owner` 取 `exec.agent?.id`（SessionId），无 agent 时省略即 unowned。jobs 服务未挂载时工具返回 `code 5`。
- **不改核心语义**：适配层只做映射/包装，不复制派发语义；`scripts/ocsr_dispatch.py` 是编排的唯一实现。

### 已移除平面及原因

- **`dsh-ocsr` settings namespace 已删除**：0.2.2 的 settings 服务改为 SettingsForms，不再提供 `register`/`get`，原「settings 里配 `allowedModels`」的接入方式不存在。只收窄门改由工具自己的 `Config.allowedModels` 承担（见上）。
- **`ocsr.jobs` session 投影已删除**：0.2.2 的 projection 契约要求 zod `ZodType`（schemastery 没有 `.parse`），且基础 bundle 默认挂载 `session-projection-cache`，OCSR 的“内存投影”按设计会自禁用；该 observation-only 平面不再有接入价值，job 生命周期直接从 jobs 服务自身读取。
- 对应文件 `lib/settings-ocsr.js`、`lib/projection-ocsr.js` 已删除；`cordis.patch.yml` 不再有 `ocsr-settings`、`ocsr-projection` entry。

## 版本锁定

- 实测基线：DeepSeek Harness desktop `0.2.2-rc.1`，其中 `@deepseek-ai/cordis` 4.1.0、`@deepseek-ai/schemastery` 3.19.0，`@deepseek-ai/dsh-skill`、`@deepseek-ai/dsh-tools`、`@deepseek-ai/dsh-jobs` 均为 0.2.2-rc.1（全局安装 `<user-home>/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh`）。
- `package.json`：`engines.dsh ^0.2.2-rc.1`；peerDependencies `@deepseek-ai/cordis ^4.1.0`、`@deepseek-ai/dsh-skill ^0.2.2-rc.1`、`@deepseek-ai/dsh-tools ^0.2.2-rc.1`、`@deepseek-ai/schemastery ^3.19.0`；dependencies `yaml ^2.8.1`。jobs/settings/session/projection 相关的旧 peer 已全部移除（jobs 只经 `ctx.get('jobs')` 取用，声明成 peer 也会误伤未挂载 jobs 的 profile）。
- 升级宿主时先复核这些接口：`skills.registerProvider(control)` 与 provider/candidate/definition 契约（含 `BUNDLED_SKILL_RANK`、`control.invalidate()`）；`defineTool` 的 parameters/output schema 与 `exec.agent?.id`；jobs 的 `JobSpec`/`JobHandle.append`/`JobHooks`（`cancel` 与 `done` 的 `completed|killed|failed` 状态）；`ctx.get('jobs')`；插件 `Config` 的 schemastery 校验；`dsh.bundle.patch` 的 entry 语法与 agent preset 名单/`tool-skill` 挂载。

## 工具层错误码

`ocsr_dispatch` 工具的结果 `code` 有两套来源，刻意不混用：

| 来源 | 取值 | 含义 |
|---|---|---|
| driver 子进程退出码（原样透传） | `0/1/2/3` | 见 [`dispatch-patterns.md`](dispatch-patterns.md) §退出码契约 |
| 工具层自判 | `4` | 模型门拒绝（不在 driver 白名单/Config 交集内、worker 串无法解析） |
| 工具层自判 | `5` | 无法执行 driver（解释器缺失、spawn 失败），或后台路径不可用（jobs 服务未挂载、`jobs.start` 注册失败） |

`background:true` 下 `code 5` 的触发条件：`ctx.get('jobs')` 为空（profile 未挂载 `@deepseek-ai/dsh-jobs` + `@deepseek-ai/dsh-tool-jobs`），或 `jobs.start(...)` 抛错（注册被拒/预检失败）。Python 解释器由 `OCSR_PYTHON` 环境变量指定（默认 `python`）。

## 验证

- `python scripts/verify_ocsr_skill.py`
- `python scripts/audit.py check`
- `python -m pytest tests/ -q`（含适配层 pytest 桥 `tests/test_dsh_adapter.py`；桥在本机缺 dsh 0.2.2 运行时或 Node 时 skip）
- `node --test tests/adapter/*.test.mjs`（等价 `npm run test:adapter`；Node 25 不接受目录参数，须传文件或 glob）。它对本机真实 dsh 0.2.2 运行时做 11 项契约回归：模块加载、安装版本门（用宿主 `evaluatePluginCompatibility` 验证 manifest 被接受、旧 `engines.dsh` 被拒绝）、真实 SkillRegistry 的 skill provider 注册/列表/加载、bundle patch entry 与 package exports 对齐、tool 定义编译与输出 schema、Config 只收窄、省略/显式空数组的 Config 语义、非法 worker 拒绝、后台 job 输出环与状态映射（含 turn abort 不杀后台 job）、`job_kill` 的 killed+原因、jobs 缺失时的 `code 5`。宿主运行时自动探测（npm 全局 `@deepseek-ai/dsh/node_modules` > `~/.dsh/profiles/node_modules` > 常见 Linux/macOS 全局路径）；设置 `DSH_RUNTIME_ROOT` 会**固定**运行时并关闭自动探测（值不可用即整组 skip，避免悄悄测到另一套安装）。找不到 0.2.2 运行时或 Python 解释器时整组 skip；需要把 skip 变成失败（CI）时设 `OCSR_REQUIRE_ADAPTER_TESTS=1`。
- `npm pack --dry-run`：`files` 只用显式 glob（`lib/*.js`、`SKILL.md`、`scripts/*.py`、`scripts/*.ps1`、`scripts/*.cs`、`refs/*.md`、`cordis.patch.yml`）。核对包含 `cordis.patch.yml`、`SKILL.md`、`scripts/`、`refs/`、`lib/`，**不含 `config/` 与 `__pycache__`**（后者曾被 `npm pack` 打进包）。
- `git diff --check`
- 非仓库目录 `/skill ocsr`（或模型 skill 工具，后者须 preset 挂载 `tool-skill`）可加载，`resourceBase` 为目录对象。
