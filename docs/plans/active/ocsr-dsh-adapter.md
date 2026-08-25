# OCSR 接入 dsh 插件体系：dsh 适配层计划

状态：已完成（评审收敛通过，已落地 Phase 1）

## 目标

在保留 OCSR 作为“跨框架、可被任意 agent 框架消费的 SKILL + headless `opencode run` 派发后端”这一通用定位的前提下，补一条 **dsh 原生** 的适配路径：让该仓库能以 Cordis 插件 bundle 的形式被 dsh profile 安装、在任意工作目录下被 dsh 的技能系统发现并加载，并在后续演进中把派发入口提升为一等 dsh 工具。

**“dsh 原生”范围界定**：本计划把 Phase 1 限定为**技能级原生**（skill-level native）——通过 host 平面 `skills` 注册表上的 runtime provider 让仓库技能被 dsh 发现、安装并可用；一等派发工具（`lib/tool-ocsr.js` 的 `ocsr_dispatch`）属于 Phase 2，不在 Phase 1 的“可执行”范围内。Phase 1 验收不要求存在一等工具。这一范围划分（Phase 1 技能级原生、一等工具留待 Phase 2）经评审确认与用户意图一致。

一句话可验证标准（限定 Phase 1 的技能级原生）：在不改变仓库核心文件语义、不破坏其它框架消费方式的前提下，`dsh plugin --profile <p> add .` 或 `add <发布包>` 之后，任意工作目录下 `/skill ocsr` 能命中本仓库的 `ocsr` 技能，且 `resourceBase` 为 `{ kind: 'directory', path: <已安装包根目录> }`（SkillResourceBase 目录对象，不是裸路径字符串），`scripts/`、`config/`、`refs/` 相对引用可解析。（“模型调用 skill 工具”在此标准中为附加路径，还须满足下文 `tool-skill` 挂载条件；该条件不影响 provider 与 `resourceBase` 的成功）。

## 非目标与不变量

- 不把 OCSR 变成 dsh 专属：`SKILL.md` + `scripts/` + `config/` + `refs/` + `docs/` + `tests/` 仍是框架无关的核心，其它框架照常消费。
- 不把“dsh 原生”等同于“一等工具”：Phase 1 不等于新增一等派发工具；一等工具属 Phase 2，Phase 1 验收不以存在 `ocsr-tools` 为前提。
- 不复制派发语义到 JS：`scripts/ocsr_dispatch.py` 是编排的唯一实现；dsh 适配层只做“映射/包装”，不重新实现。
- 不改变 OCSR 的安全、预算、证据与失败停止规则；`--dir`、prompt 禁令、路径审计仍不得表述为安全沙箱。
- 不弱化“SKILL.md 是运行规则唯一事实源”：dsh 适配层不得硬编码技能描述/正文，`list()` 与 `get()` 必须读取同一份包根 `SKILL.md`（frontmatter + 正文）来构造返回对象，不得硬编码 `name`/`description`/`whenToUse`/`metadata`。
- 不引入以任务名/关键词枚举为核心的路由表。

## 目标形态

单一仓库、单一 npm 包，仓库根目录同时是“通用 SKILL”与“dsh 插件 bundle”的两张脸：

```text
ocsr/
  SKILL.md            # 通用 skill（唯一事实源；其它框架入口）
  scripts/, config/, refs/, docs/, tests/
  AGENTS.md CLAUDE.md GEMINI.md

  # ── dsh 适配面（语义独立，位于包内）──
  package.json        # type:module; dsh:{bundle:{patch:'./cordis.patch.yml'}}; exports; files(含 cordis.patch.yml); deps/peers
  cordis.patch.yml    # bundle patch：插入 ocsr-skill（Phase 1）/ ocsr-tools（Phase 2）行
  lib/index.js        # skill provider：list()/get() 每次读 SKILL.md（frontmatter + 正文），共用同一解析结果
  lib/tool-ocsr.js    # （Phase 2，可选）一等工具：包装 scripts/ocsr_dispatch.py
  refs/dsh-integration.md   # dsh 适配器专用事实源（安装/配置/版本锁定/演进）
```

**为什么是根级单包，而不是 `dsh/` 子包**：dsh profile 安装后 `files`/`exports` 以包根为锚，子包无法把仓库根级的 `SKILL.md`/`scripts/`/`refs/` 一并装进去；若要装就得引入拷贝/构建同步，反而破坏“唯一事实源”。根级单包让 `add .` 一句安装，同时根级 `SKILL.md` 对其它框架依旧可见。

## 关键机制与边界（dsh 侧事实）

- dsh 插件 = npm 包，`package.json` 带 `dsh: { bundle: { patch: './cordis.patch.yml' } }`（嵌套 JSON，不是 dotted key）；安装命令 `dsh plugin --profile <p> add <pkg>`（转发 pnpm，装完自动把包加入 `dsh.profile.bundles`）。
- `cordis.patch.yml` 是 patch 层表单：用 `- insert:` 插入行（`id`/`name`/`config`），`name` 为模块说明符，指向导出 Cordis 插件（`name`/`apply`/`inject`）的模块。
- 技能接入的正确缝是 **host 平面 `ctx.skills` 注册表上的 runtime provider**（照 `@deepseek-ai/dsh-skill-badge` 模式：`inject=['skills']`，`apply` 内 `ctx.skills.registerProvider((control) => ...)`——工厂形参即 `{ signal, invalidate }` 管理/失效控制对象，provider 闭包须持有它）**而不是**给 `skill-filesystem` 加 `customSkillDirs`。原因：`web` profile 把 host 端 `skill-filesystem` 行置 `disabled: true`（技能后端挪到 agent-preset），只有直接注册到仍留在 host 平面的 `skills` registry 才能全局生效。
- **`tool-skill` 挂载条件**：在 `web` profile 下，“模型调用 skill 工具”还要求当前激活的 agent preset 挂载 `tool-skill`。实测（当前 `@deepseek-ai/dsh` checkout 的 `config/agent-presets/*/agent.cordis.yml`）：`minimal` **不**挂载 `tool-skill`；`standard` 与 `code` preset **都**挂载（两者 `agent.cordis.yml` 均含 `skill-filesystem` 与 `tool-skill`）。OCSR provider 注册在 host 平面 `skills` registry 上只保证“技能目录/元数据存在且可达”，不保证任意 preset 都向模型暴露 skill 工具。该边界写入 `refs/dsh-integration.md`。
- **版本锁定**：本次 host 平面 runtime-provider 的判定以当前 `@deepseek-ai/dsh` checkout 实测为准；验证所用的 dsh 版本/install 在 `refs/dsh-integration.md` 记录并作版本锁定，避免宿主 API 变动破坏。
- 一等工具接入（Phase 2）：`inject=['tools']`，用 `@deepseek-ai/dsh-tools` 的 `defineTool` 调 `ctx.tools.register(...)`；工具内部是薄壳，把 schema 参数映射为 `python scripts/ocsr_dispatch.py ...`，结果归一化返回。

## 实施步骤（Phase 分层）

### Phase 1（最小闭环，本计划主体）

1. 前置基线：读 `docs/CURRENT.md`、`docs/STRUCTURE.md`、`docs/overview.md`、`docs/pitfalls.md` 与 `SKILL.md`；确认当前 dsh 版本与 profile（web/headless/tui）bundle 结构。记录当前用于验证的 `@deepseek-ai/dsh` checkout/版本与 profile，并在 `refs/dsh-integration.md` 作版本锁定。
2. 新增根级 `package.json`：`name`（建议 `@ocsr/dsh-ocsr`，可改）、`type: module`、`main`/`exports` 指向 `lib/index.js`。`files` 必须显式列出 `lib/`、`SKILL.md`、`scripts/`、`config/`、`refs/` **以及 `cordis.patch.yml`**（npm `files` 不会自动包含未列出的文件；bundle patch 配置未列出会在 `add <pkg>` 后丢失 bundle patch 文件）。声明 `dsh: { bundle: { patch: './cordis.patch.yml' } }`（嵌套 JSON，非 dotted key）；`peerDependencies` 锁 `@deepseek-ai/dsh-skill`、`@deepseek-ai/cordis`；`dependencies` 加入 `yaml`（供 frontmatter 解析）。
3. 新增 `cordis.patch.yml`：`- insert:` 一条 `id: ocsr-skill, name: '@ocsr/dsh-ocsr'` 行。
4. 新增 `lib/index.js`：导出 Cordis 插件，插件 `name` 取 `'ocsr-skill'`（与 `cordis.patch.yml` 的 `id` 一致，对齐 dsh 现有 `skill-badge` 模式）、`inject=['skills']`、`apply(ctx)`；`apply` 内 `ctx.skills.registerProvider((control) => { const provider = { name: 'ocsr', ... }; return provider; })`（注册工厂接收 `control` = `{ signal, invalidate }`，provider 闭包持有它以在 SKILL.md 变化时调用 `control.invalidate()`）。注意区分两层名字：provider 对象 `{ name: 'ocsr', ... }`，Cordis 插件导出名 `ocsr-skill`（参照 `@deepseek-ai/dsh-skill-badge`：其 provider.name = `'dsh-badge'`、插件 name = `'skill-badge'`，插件名与 provider 名不同）。provider 在 `list()` 时用 `yaml` 解析包根 `SKILL.md` 的 frontmatter（`name`/`description`/`whenToUse`/`user-invocable`/`disable-model-invocation`/`metadata`），在 `get()` 时重新读取同一份 `SKILL.md` 并按其正文生成 `content`（`content` 为剥离 `---` frontmatter 块后的正文，YAML 前置块不得进入 `content`）；不得把元数据/正文当作 apply 时的常量缓存。

   `list()` 与 `get()` 必须共用同一份 SKILL.md 解析来源构造返回对象（同一解析器、同一包根文件），不得硬编码 `name`/`description`/`whenToUse`/`metadata`：
   - `list()` 返回 candidate：`{ name, description, whenToUse?, invocation, provider, source, resourceBase, rank, locator, metadata? }`。其中 `resourceBase = { kind: 'directory', path: <包根目录> }`（SkillResourceBase 目录对象，不是裸路径字符串）、`rank = BUNDLED_SKILL_RANK`、`locator` 为该候选的 provider 私有句柄（如 `SKILL.md` 的解析路径）；frontmatter 存在 `whenToUse`/`metadata` 时随对象带入。
   - `get()` 返回 definition：`{ name, description, whenToUse?, invocation, provider, source, resourceBase, content, path?, metadata? }`。`content` 为剥离 `---` frontmatter 块后的 SKILL.md 正文（即 frontmatter 之后的部分，YAML 前置块不进入正文）；`resourceBase` 与 candidate 相同（`{ kind: 'directory', path: <包根目录> }`）；definition 契约在 `path` 字段存在时要求其为字符串，因此同时给 `path` 指向 `SKILL.md` 的解析路径。`@deepseek-ai/dsh-skill-badge` 的 `get()` 即把同一 `resourceBase` 对象返回给 definition，作为此处“definition 含 resourceBase”的参照。
   - provider 不实现也不调用 `validateCandidate`/`validateDefinition`：这两个函数是 `@deepseek-ai/dsh-skill` 的模块私有实现（未导出），由 `SkillRegistry` 在候选进入 `collect()`/`get()` 时自行校验。因此 provider 的职责只是返回符合契约的对象——`list()` 返回符合 `SkillCandidate` 契约、`get()` 返回符合 `SkillDefinition` 契约；字段约束（如 `provider` 与 `source` 为字符串、`provider` 为 provider 对象名 `'ocsr'`、`source` 如包路径/`file:` 来源、`description` 取自 frontmatter `description`）由注册表校验，provider 仅需遵守。
   - frontmatter → `invocation` 翻译：`user-invocable` → `invocation.userInvocable`；`disable-model-invocation` → 取反后写入 `invocation.modelInvocable`（即 `modelInvocable = !disableModelInvocation`）。缺省时给出与 `@deepseek-ai/dsh-skill-badge` 一致的合理默认。
   - 一致性：`list()`/`get()` 每次调用都读取 `SKILL.md`；当 provider 检测到 frontmatter/正文发生变化时调用 `ctx.skills.registerProvider()` 注入的 `control.invalidate()`（工厂形参 `control` 即注册时注入的同一 `{ signal, invalidate }` 对象，provider 闭包在创建时持有该句柄），使 dsh 目录重新收集。因此 `SKILL.md` 变更后 dsh 侧在**重新收集/重启后**一致（正文在每次 `get()` 即取最新），而清单（summary）需 re-collect 才更新；dsh 技能目录会缓存 catalog，非热更新，除非 provider 主动失效或重启。
5. 新增 `refs/dsh-integration.md`：记录 dsh 侧安装、配置（模型白名单覆盖方式）、与核心的边界、版本锁定（含当前 `@deepseek-ai/dsh` checkout）、`tool-skill` 挂载条件（`minimal` 不挂载；`standard` 与 `code` preset 均挂载）、Phase 2/3 演进方向与验证方式。
6. 文档接线：`docs/STRUCTURE.md` 登记 `refs/dsh-integration.md` 与入口；`docs/CURRENT.md` 记当前状态；`AGENTS.md` 增加一条硬约束“dsh 适配层只映射/包装，不得复制核心语义或技能正文”。随后运行 `python scripts/agent_links.py repair` 同步 `CLAUDE.md`/`GEMINI.md`。
7. 机械验证：
   - `dsh plugin --profile web add .`（或指向发布包）成功，`dsh --profile web --dump-config` 能看到 `ocsr-skill` 行。
   - 发布包完整性：运行 `npm pack --dry-run`（或 `npm pack --dry-run --json`）并核对打包清单至少包含 `cordis.patch.yml`、`SKILL.md`、`scripts/`、`config/`、`refs/` 及 `lib/index.js`；该检查验证 `add <发布包>` 路径可用，而非仅本地 `add .`。
   - 在**非仓库**的工作目录启动，`/skill ocsr`（或模型 skill 工具，后者须满足 `tool-skill` 挂载条件）能加载，`resourceBase` 为 `{ kind: 'directory', path: <已安装包根目录> }`（不是裸路径字符串），`scripts/`/`config/`/`refs/` 相对引用可解析。
   - 运行仓库既有验证：`python scripts/verify_ocsr_skill.py`、`python scripts/agent_links.py check`（`AGENTS.md` 改动后已用 `repair` 同步）、`python scripts/audit.py check`、`pytest tests/ -q`、`git diff --check`。
8. 治理评审：由于新增了对 Agent 行为有约束力的 dsh 适配机制，按仓库规则走独立 fresh-context 评审；重点审查“唯一事实源不重复”“通用性不破坏”“web profile 下技能可达”“dsh 适配层边界清晰”。

### Phase 2（可选，一等工具）

在 Phase 1 稳定后，新增 `lib/tool-ocsr.js`：用 `defineTool` 注册 `ocsr_dispatch`（后续可拆 `ocsr_run_spec`/`ocsr_selftest`），schema 参数（`worker`/`output-dir`/`output-pattern`/`watch`/`fork` 等）映射到 `python scripts/ocsr_dispatch.py ...`，返回归一化结果；**编排语义仍只在 python**。`cordis.patch.yml` 追加 `ocsr-tools` 行。本阶段才是“一等工具”意义上的 dsh 原生扩展。

### Phase 3（可选，深度原生）

将长派发通过 `ctx.jobs` 提升为 dsh 可跟踪 background job、把 worker 状态投影到 session。依赖 dsh jobs API，需先确认接口再动，不贸然耦合。本计划不将 Phase 3 纳入当前“可执行”范围，仅记录方向。

## 验收标准

- `dsh plugin --profile web add .` 一行安装成功；`--dump-config` 可见 `ocsr-skill` 行。
- 非仓库工作目录下，`/skill ocsr`（或模型 skill 工具，后者须满足 `tool-skill` 挂载条件）可加载，且 `resourceBase` 为 `{ kind: 'directory', path: <已安装包根目录> }`（SkillResourceBase 目录对象，不是裸路径字符串）。
- 发布包完整性：`npm pack --dry-run` 打包清单须包含 `cordis.patch.yml`、`SKILL.md`、`scripts/`、`config/`、`refs/`（及 `lib/index.js`），确保 `add <发布包>` 与 `add .` 两条路径均被验证。
- `SKILL.md` 仍是唯一事实源：dsh 适配层无硬编码的 `name`/`description`/正文副本；`list()` 与 `get()` 均读同一份 `SKILL.md` 解析结果。“改 `SKILL.md` 后 dsh 侧自动一致”定义为：provider 在 `list()`/`get()` 每次读取 `SKILL.md`，并在检测到变化时调用 `invalidate()` 使 dsh 技能目录重新收集——因此变更后 dsh 侧在**重新收集/重启后**一致，正文在每次 `get()` 即取最新；dsh 技能目录会缓存 catalog，非热更新，除非 provider 主动失效或重启。
- 仓库核心语义与其它框架消费方式不变：`SKILL.md` 在根级，`scripts/`/`config/`/`refs/` 相对引用未被挪动。
- 仓库既有验证脚本与测试与当前基线一致：`pytest tests/ -q` 结果等于基线（257 passed + 1 个已知环境相关失败；可复现基线出处 `docs/CURRENT.md` 2026-08-19 复验记录）。唯一失败项 `TestPidCaptureAndKill.test_kill_actually_terminates_process` 为 `taskkill` 对测试启动的 PowerShell 子进程返回 `Access denied`，与 dsh 适配无关；不得以“= 257 passed”替代完整基线描述。`refs/dsh-integration.md` 的安装/配置/边界说明与实现一致。
- 范围边界：Phase 1 为技能级原生，验收不含一等工具；`cordis.patch.yml` 在 Phase 1 仅含 `ocsr-skill` 行（`ocsr-tools` 属 Phase 2）。
- 独立 fresh reviewer 能仅凭计划与 `SKILL.md`、`refs/dsh-integration.md` 判断：目标形态、硬约束、边界、验证步骤是否自洽、可执行、不破坏通用性。

## 风险与裁决点

- **web profile 技能可达性**：这是最容易踩的坑——必须用 host 平面 runtime provider，而非 `customSkillDirs`。评审时应作为重点核查项。同时注意“模型调用 skill 工具”还受 agent preset 是否挂载 `tool-skill` 约束（实测 `minimal` 不挂载；`standard`/`code` 挂载），provider 本身只保证技能目录/元数据存在。
- **包名/scope 未定**：`@ocsr/dsh-ocsr` 仅是建议；需用户确认后再锁 package.json。
- **Phase 范围与“dsh 原生”定义**：本计划把 Phase 1 限定为技能级原生，一等工具归 Phase 2。若评审认为“dsh 原生”必须含一等工具，需要在范围上作明确取舍。
- **单包装入范围**：`files` 必须含 `scripts/`/`config/`/`refs/` 及 `cordis.patch.yml`，否则安装后技能内相对引用断裂、bundle patch 文件丢失。
- **不要过度耦合 dsh 版本**：peer 锁 rc 版本，避免宿主 API 变动破坏；验证所用 dsh 版本在 `refs/dsh-integration.md` 记录并锁定。

## 评审处置记录

- Round 1（本轮）：按评审意见修订，逐项处置：
  - 阻塞 1：`package.json` `files` 增加 `cordis.patch.yml`（并说明 npm `files` 不自动包含未列出文件）。
  - 阻塞 2：Phase 1 step 4 补全 candidate/definition 形状（两者均含 `name`/`description`/`invocation`/`provider`/`source`；candidate 另含 `resourceBase`/`rank`，definition 含 `content`/`path`），并写明 frontmatter → `invocation` 翻译；参照 `@deepseek-ai/dsh-skill-badge`。
  - 阻塞 3：明确 `list()` 与 `get()` 都读同一份 `SKILL.md` 解析结果；为“自动一致”定义更新/重启时机。
  - 阻塞 4：验收标准改为“与当前基线一致（257 passed；既有环境相关失败项不在本适配范围内）”，并注明 `TestPidCaptureAndKill.test_kill_actually_terminates_process` 为环境相关失败。
  - 建议 1：目标与验收边界明确“dsh 原生”= Phase 1 技能级原生；一等工具属 Phase 2。
  - 建议 2：Phase 1 step 6 增加 `python scripts/agent_links.py repair`，验证步骤保留 `agent_links.py check`。
  - 建议 3：step 4 明确导出 Cordis 插件 `name: 'ocsr-skill'`，与 `cordis.patch.yml` 行 id 一致。
  - 建议 4：关键机制与 refs 增加“web 下模型调用 skill 工具需 agent preset 挂载 `tool-skill`”边界说明。
  - 建议 5：记录验证所用 dsh 版本/install（当前 `@deepseek-ai/dsh` checkout）并在 refs 作版本锁定。
- Round 2（本轮）：在 Round 1 基础上继续收敛，逐项处置：
  - 阻塞 1：`get()` 补齐 `resourceBase`（与 candidate 相同的 `{ kind: 'directory', path: <包根目录> }`），并在 frontmatter 存在 `whenToUse`/`metadata` 时随 definition 返回；参照 `@deepseek-ai/dsh-skill-badge` 的 `get()` 返回同一 `resourceBase` 给 definition。
  - 阻塞 2：把 `resourceBase` 的形态统一写成 `SkillResourceBase` 目录对象 `{ kind: 'directory', path: ... }`，candidate、definition、目标形态与验收标准一致采用该对象形状，不再用裸路径字符串或“`resourceBase = 包根目录`”的模糊表述。
  - 建议 1：修正 `tool-skill` 边界——**只有 minimal preset 不挂载 `tool-skill`**；`standard` 与 `code` preset（含 `code/agent.cordis.yml`）均挂载，不再写成“`minimal`/`code` 可能不挂载”。
  - 建议 2：pytest 验收标准改为“等于基线（257 passed + 1 个已知环境相关失败）”，并给出可复现基线出处 `docs/CURRENT.md`（2026-08-19），不写成“= 257 passed”。
  - 建议 3：显式写明 provider 对象名 `'ocsr'`；说明插件导出名 `ocsr-skill` 与 provider 名 `ocsr` 不同（对齐 `dsh-skill-badge` 的 provider/plugin 命名差异）。
  - 建议 4：`dsh.bundle.patch` 统一写成嵌套 JSON `dsh: { bundle: { patch: './cordis.patch.yml' } }`，全文不再使用 dotted key。
  - 建议 5：调和“provider 读取 SKILL.md 一次”与“re-collect/restart 后自动一致”——改为 provider 在 `list()`/`get()` 每次读取 SKILL.md，变化时调用 `invalidate()`；`SKILL.md` 变更后 dsh 侧在重新收集/重启后一致，正文在每次 `get()` 即取最新。
- Round 3（本轮）：blank-slate 复查后修订，逐项处置：
  - 阻塞 1（BR-1）：移除“provider 必须通过 `validateCandidate`/`validateDefinition`”的表述——这两个函数为 `@deepseek-ai/dsh-skill` 模块私有（未导出），由 `SkillRegistry` 在 `collect()`/`get()` 内部调用。改为：provider 返回符合 `SkillCandidate`（`list()`）/`SkillDefinition`（`get()`）契约的对象，校验由注册表执行，provider 不调用也不导入。
  - 阻塞 2（BR-2）：把 `registerProvider(() => provider)` 改为 `registerProvider((control) => { const provider = { name: 'ocsr', ... }; return provider; })`；明确工厂接收 `control` 对象，provider 闭包持有该句柄，在解析的 SKILL.md 与其上次已知状态不同时调用 `control.invalidate()`。
  - 建议 1：显式写明 `get()` 的 `content` 必须剥离 `---` frontmatter 块（`SkillDefinition.content` = 移除元数据后的正文），YAML 前置块不得进入正文。
  - 建议 2：新增发布包检查（`npm pack --dry-run` 打包清单核对 `cordis.patch.yml`/`SKILL.md`/`scripts/`/`config/`/`refs`），确保 `add <发布包>` 路径被验证，而非仅本地 `add .`。
  - 建议 3：范围决策保持可见：Phase 1 = 技能级原生（可安装 skill provider），一等 `ocsr_dispatch` 工具为 Phase 2；该划分与用户意图一致，Round 3 予以确认。
- Round 4（本轮）：盲审#2 通过 → 落地 Phase 1。
- 状态：已完成（评审收敛通过，Phase 1 已落地）。

