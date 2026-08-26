# OCSR dsh 适配层 Phase 3：深度原生（ctx.jobs + settings 配置 + session projection）计划

状态：进行中（评审收敛，处置见「评审处置记录」）

## 目标

在保持“python 是编排唯一实现，dsh 层只做观测/编排/配置”这一不变量（Phase 1/2 已确立）的前提下，把 dsh 适配层从“技能级 + 一等工具”推进到“深度原生”，覆盖四点：

1. **ctx.jobs 集成**：让长时间 ocsr_dispatch 派发可作为 dsh 后台 job 跟踪，model 可经 job_output/job_list/job_kill 观察与终止，不再受“前台 timeout 小于单轮耗时”的约束。
2. **worker 状态投影（session projection）**：以**只读、in-memory-only / best-effort** 方式把 ocsr 后台 job 的可见状态变化投影进会话历史（session projection），供会话历史/UI 观察派发树；只观测、不编排，不把派发/调度/看门狗/ledger 语义复制进 JS。
3. **模型白名单 dsh 配置化**：允许经 dsh settings（dsh-ocsr namespace）覆盖模型白名单；因 driver 在 import 时读取固定 config/allowed-models.json，本覆盖只允许收窄（交集），不得扩展。
4. **薄壳原则**：以上全部为“生命周期编排 + 只读观测 + 配置门”，不把派发语义搬进 JS；scripts/ocsr_dispatch.py 仍是编排唯一实现。

> 说明：第 2 点「worker 状态投影（session projection）」**恢复为范围点**，不再降级为显式非目标。恢复路径为“in-memory-only / best-effort 只读投影单元”：观察 ctx.jobs 生命周期并把 job 状态变化合成进 session-projection fold；不修改 SKILL.md / scripts/ocsr_dispatch.py，也不在 JS 中复制派发语义。

一句话可验证标准：Phase 1/2 功能不回退，且 ocsr_dispatch 在 background 为 true 时注册 dsh 后台 job 并可被 job_output/job_kill 操作；SKILL.md 与 scripts/ocsr_dispatch.py 未被修改；模型白名单可被 dsh settings 收窄（未配置时不影响现有行为，显式空数组才等于主动关闭）；session projection 在 registry/jobs/sessions 可用时注册只读、in-memory-only 投影单元、在任一不可用时降级不炸。

## 非目标与不变量

- 不修改 SKILL.md 与 scripts/ocsr_dispatch.py（保持框架无关、单事实源）。
- 不把编排/工人调度/看门狗逻辑复制到 JS；JS 只做“启动进程、暴露句柄、观察状态、配置门、只读投影”。
- 白名单覆盖不得突破驱动自身 ALLOWED_MODELS；只做交集过滤，扩展需用户在 python 侧显式加许可（不在本计划范围，除非用户决定给 driver 加 env/arg 缝）。
- 不把 --dir/forbid-paths/路径审计描述成安全沙箱。
- **session projection 仅为只读、in-memory-only / best-effort 观测**：不把 job 状态写回 session log（不 append 合成事件），不注册“永不更新/注册即失败”的投影单元，不因 registry/jobs/sessions 不可用而让插件加载失败；投影只是把观察到的 job 可见状态映射进会话历史视图，绝不承担编排语义。apply(state,event) 是纯函数，不具备 ctx/jobs 访问，因此**不得**在 apply 内部重新读取 jobs 或“refold”；“降级到下一真实事件再 refold”只能由拥有 ctx/jobs 访问的插件层（投影合成层）承担，且须按本计划设定合成事件的 seq/timestamp。
- 依赖 dsh jobs/settings/sessions/sessionProjections 接口的当前版本（@deepseek-ai/dsh 0.1.1-rc.2）；若接口不可用则降级不炸（fail-closed 于“返回可诊断错误/跳过注册”，不 fail-open）。

## 关键 dsh 事实（已实测/源码核对）

- ctx.jobs（@deepseek-ai/dsh-jobs + dsh-jobs-local）：jobs.start({ kind, label, owner?, run })，run 返回 { cancel, done, readOutput }，返回 jobId；生产者模式同 dsh-tool-bash（lib/index.js 约 414 行）。model 侧经 dsh-tool-jobs 暴露 job_output/job_list/job_kill。
- ctx.jobs 的可用性通常是“服务已注册但未挂载消费者”的动态态：jobs.start 在 owner 无 served controller、maxConcurrentJobsPerOwner 超限、kind/label 为空等情况下会抛出，不能把“ctx.get('jobs') 取到”等同于“一定能 start 成功”。
- ctx.jobs 变更观察：jobs.onJobsChanged(listener) 以 **owner 粒度**通知可见集合移动（注册、stopping 转变、settle、owner 移除、服务清空），listener 参数为 owner（Agent）或 undefined（无主 job）。jobs.list(caller) 返回当前可见 job 快照数组（id/kind/label/status/detail?/startedAt/finishedAt/ownerSession）。
- ctx.sessionProjections（@deepseek-ai/dsh-session-projection）：register(definition) 契约字段为 { key, stateSchema, init(), apply(state,event), wire?:{viewSchema,view}, stateVersion }；registry 只对已提交的 session/event 做 eager drive；apply 对无关事件须返回同一引用（Same-reference gate）；wire.view 为同步纯函数；state 必须为 plain JSON 且由 stateSchema 校验；stateVersion 为非负整数。
- ctx.get('sessions')（@deepseek-ai/dsh-session）：**宿主 session-store 服务**（SessionStore，提供 `get(id)` 等只读访问，把 owner/Agent 解析为对应 session）。投影层用它做 `ctx.get('sessions')?.get(owner.id)` 的 owner→session 解析；无主 job 或查不到 session 则跳过该次（best-effort）。对投影单元而言它是注册的**必需**服务之一（sessionProjections + jobs + sessions 三者齐全才注册）；缺任一即不注册。**不得使用属性访问器 `ctx.sessions`**——不可达时会 throw，一律经 `ctx.get('sessions')` 判空后使用。
- ctx.get('settings')（@deepseek-ai/dsh-settings）：settings.register(ns, schema, { base }) 注册 namespace；解析顺序为 schema 默认值 → 注册方 composition base（entry-config）→ 用户文档 section。**注意**：`ctx.settings` 属性访问器仅在服务可达时可作为简写，服务不可达时会 throw——插件一律经 `ctx.get('settings')` 判空后使用 `settings.get(...)`/`settings.register(...)`。因此本计划把“驱动白名单文件（driverBaseModels）”“settings 的 priority base 层”两组 base 语义严格区分。
- 白名单：scripts/ocsr_dispatch.py:144 的 ALLOWED_MODELS_PATH = Path(__file__).resolve().parents[1] / config / allowed-models.json，import 时一次加载（_CONFIGURED_MODELS/ALLOWED_MODELS）；无 env/arg 覆盖缝。故 dsh settings 只能做“收窄校验/前置门”。**JS 侧由 lib/settings-ocsr.js 的 loadDriverBaseModels() 读取同一 config/allowed-models.json 作为 driverBaseModels**；读不到/非法时 fail-closed（返回空集，与 driver import 对缺失/非法文件 fail 的行为一致，只是 JS 侧不 throw、返回可诊断空集）。
- --worker 格式（scripts/ocsr_dispatch.py:946-954）：PROMPT_PATH|MODEL|LABEL，按 | **最多切两次**（Python w.split("|", 2)，maxsplit=2），取中段为 MODEL。前置门**只做这段格式映射**（提取 model 字段用于交集校验），不复制 driver 的派发/调度语义；解析不通过时 fail-closed。

## 实施设计

### 3.1 ctx.jobs 集成（lib/tool-ocsr.js）

- 工具 schema 增加 background（boolean，默认 false）；**worker 描述修正**：把当前的“a prompt file path, an opencode -m model id, or a LABEL=|... label pattern”改为与 driver 一致的 PROMPT_PATH|MODEL|LABEL 三段 | 分隔格式。worker 为 required:true，因此模型门对每次调用都必然执行。
- **inject 保持现状**：ocsr-tools 的 Cordis inject 仍为 ['tools']，**不加入** jobs/settings/sessions/sessionProjections。jobs/settings/sessions/sessionProjections 均属可选服务，改为在 execute/apply 里用 ctx.get('jobs') / ctx.get('settings') 运行时获取；取不到即降级为可诊断 ok:false，绝不因 profile 缺服务而使整个插件加载失败（web/内置 profile 不受影响）。**不得使用属性访问器 `ctx.jobs`/`ctx.settings`**——Cordis 只在服务可解析时暴露属性，不可达时会 throw，一律经 `ctx.get(...)` 判空后使用。
- execute(args, exec)：
  - **顶部设置门**：在进入前台/后台分支之前，先从 ./settings-ocsr.js 导入并使用 loadDriverBaseModels()（读取同一 config/allowed-models.json，见 3.3）、parseWorkerModel(args.worker) 与 effectiveAllowedModels(driverBaseModels, settingsAllowedModels)。driverBaseModels 为 loadDriverBaseModels() 的返回值（不再由 tool-ocsr 自行读文件）；settingsAllowedModels 为仅在 settings 服务可用时经 `const settings = ctx.get('settings'); if (!settings) { settingsAllowedModels = undefined（回退 driverBaseModels）; } else { settingsAllowedModels = settings.get('dsh-ocsr')?.allowedModels; }` 取得；未配置为 undefined。**不得使用属性访问器 `ctx.settings`**（Cordis 语义下不可达服务会 throw），必须经 `ctx.get('settings')` 判空后调用 `settings.get(...)`。worker 为 required:true，模型门总是执行；模型不可解析（fail-closed）或不在 effective 集合内 → 返回 { ok:false, kind: args.background ? 'background' : 'foreground', jobId: null, code:3, stderr } 可诊断，**不启动进程、不注册 job**。门对前台与后台两条路径同样生效。**故障 code 定义**（全计划统一）：`1`=background jobs 不可用，`2`=jobs.start 抛错，`3`=模型门拒绝/不可解析（任一非零成功码即非零，不依赖具体值也可；此处钉住以便可诊断）。
  - 若 args.background 为 true：
    - const jobs = ctx.get('jobs')；若不可用 → 返回 { ok:false, kind:'background', jobId:null, code:1, stderr:'background jobs unavailable: load @deepseek-ai/dsh-jobs + dsh-tool-jobs' }（不抛、可诊断）。
    - jobs.start({ kind:'ocsr-dispatch', label: worker+output_dir 摘要, owner: exec.agent, run: () => buildBackgroundSpec(...) }) **整体包 try/catch**：start 前/中抛错（owner 无 served controller、maxConcurrentJobsPerOwner、kind/label 为空等）→ { ok:false, kind:'background', jobId:null, code:2, stderr:'background job start failed: ' + err.message }，同样可诊断、不 throw 到插件层。
- **buildBackgroundSpec**：封装进程句柄；jobs.start 的 run 必须返回 `{ cancel, done, readOutput }`（与 dsh-tool-bash lib/index.js 约 414 行的生产者模式一致；cancel 在请求取消时触发，done 在进程退出后 resolve 为 `{ status, detail?, output? }`）。
    - **readOutput() 契约（钉住）**：每次调用**排空（drain）**子进程 stdout/stderr 到内部累积缓冲，并返回自上次读取以来的**增量（delta）**；调用方（dsh-tool-jobs / model 侧 job_output）自行累加 delta 即得**全量（full）**。即 readOutput 不是“返回全量”的只读查询，而是“累积+排空”的拉取接口；“delta vs full”由调用方区分。
    - 成功时返回 { ok:true, kind:'background', jobId }（model 用 job_output/job_kill 继续）。
  - 若非 background → 维持现前台路径（runDriver 返回归一化结果），并令前台返回也统一携带 kind:'foreground'、jobId:null，以便 renderResult 统一按 kind 分支。
- 取消/exec.signal 仍生效；jobs.start 的 run.cancel 与 done 要保证“进程退出后 resolve”，避免 dangling。
- **output.schema 扩展**：现有 additionalProperties:false 且仅允许 ok/command/code/stdout/stderr 的 schema 必须改为同时允许 kind（string，enum [foreground,background]）与 jobId（string|null），并令 command/code/stdout/stderr 为可选；否则 background 返回（尤其 background 失败 { ok:false, kind:'background', jobId:null, code, stderr }）会被 schema 拒绝。
- **renderResult 增加统一的 background 分支**：renderResult 先按 kind === 'background' 分支（**包括失败**——因为所有 background 返回都携带 kind:'background' 与 jobId）。成功渲染 `[background job] jobId=<id>（kind=ocsr-dispatch）`；失败渲染 `[background job] failed: <stderr>`（jobId 为 null 时可省略 id）。前台与一般结果维持前台渲染，但**仅当 command !== undefined 才打印 command: ...**，以避免无 command 时输出 command: undefined。由于 background 失败不再缺 kind/jobId，它不会再落入前台分支、也不会再打印 command: undefined。
- 验证注记：以**目标 profile 实际挂载了 dsh-tool-jobs** 为前提才能让 model 侧 job_output/job_list/job_kill 可见；仅注册 local job（dsh-jobs-local）而不挂 dsh-tool-jobs 时，model 侧工具不可见。应把“挂载 dsh-tool-jobs”作为验收/联调环境前置并写入验证步骤。

### 3.2 session projection（lib/projection-ocsr.js）

- **决策**：采用 **in-memory-only / best-effort** 只读投影单元。不做高保真复制，也不把 ocsr 派发语义搬进 JS；本单元只是“观察 ctx.jobs 生命周期并把可见 job 状态变化折叠进 session-projection”，只记录当前存活进程内可见状态、不承诺跨重启持久性。
- **插件**：name='ocsr-projection'；inject: []（可选服务，运行时 ctx.get('sessionProjections')/ctx.get('jobs')/ctx.get('sessions')；任一不可用时跳过注册并记录可诊断 warning，不炸插件）。本单元仅记录存活进程内可见状态，不声明跨重启持久性。
- **register(definition) 契约字段**（按 @deepseek-ai/dsh-session-projection 0.1.1-rc.2）：
  - key: 'ocsr.jobs'（声明进 SessionProjectionMap / SessionProjectionStateMap 的 client key）。
  - stateSchema: schemastery 校验 { byId: record(string→jobSnapshot), order: array(string) }；值必须是 plain JSON。
- **jobSnapshot 具体字段**：`{ id: string, kind: string, status: string, label: string, owner?: string | null, detail?: string, startedAt?: number | string, finishedAt?: number | string }`。其中 owner 为内部状态字段（来自 jobs.list(caller) 的可见集合，取 ownerSession/owner id）；**不包含 outputRef**（该字段从不来自 jobs.list 快照，故不声明）；**wire.view 只暴露子集**（见下），内部不承诺向 view 透传 owner。
  - init(): 返回 { byId: {}, order: [] }。
  - apply(state, event): **同步**；仅当 event.type === 'ocsr/jobs:update' 时折叠，否则返回同一引用（Same-reference gate）。合成事件的 payload.jobs 携带**完整**可见 job 快照数组（whole-value 规则）：按 id 覆盖/新增 byId，删除不在列表的 id，重算 order；返回新的 plain-JSON state。**apply 是纯函数、无 ctx/jobs 访问**，因此内部**不得**读取 jobs；若合成事件到来时状态已在 byId 中，仅需按 payload 全量覆盖即可。
- wire（client-visible）：viewSchema = schemastery { jobs: array({ id, kind, label, status, detail?, startedAt?, finishedAt? }) }（**子集**，不含 owner，且因 jobSnapshot 不含 outputRef 故亦无 outputRef）；view(state) = { jobs: state.order.map((id) => state.byId[id]) }（同步、只读、纯映射，返回前从 jobSnapshot 中挑出子集字段，避免把内部 owner 泄漏到客户端视图）。
  - stateVersion: 1（非负整数；未来改字段或折叠语义需 bump）。
- **如何把 job 生命周期合成进 apply**：
  - 先在插件层 `const jobs = ctx.get('jobs')`；不可用则跳过本轮（best-effort，与插件整体降级一致）。随后用该已取得引用（而非属性访问器 `ctx.jobs`）调用 `jobs.onJobsChanged(owner)`；onJobsChanged 是 owner 粒度（注册、stopping、settle、owner 移除、服务清空都会触发）。
  - 每个变更以 owner（Agent）解析对应 session：ctx.get('sessions')?.get(owner.id)；无主 job 或查不到 session 则跳过该次（best-effort）。
  - 从已取得的 `jobs` 引用调用 `jobs.list(owner)` 取当前可见 job 快照数组（jobs 已在上文判空守卫，无需再 `ctx.get('jobs')?.list(owner)` 回退），**在插件层（合成层）构造合成事件**，并显式设定 seq/timestamp（registry.drive 依赖 event.seq）：
    - 事件 shape：`{ type:'ocsr/jobs:update', seq, timestamp, payload: { jobs: <整个可见集合，按 jobSnapshot 字段归一> } }`。
    - **seq**：以该 session 当前已提交事件的序列推导，避免与真实事件冲突。优先取 `seq = Math.max(1, (session.events?.length ? Number(session.events[session.events.length-1]?.seq ?? session.events.length - 1) + 1 : 1))`；**合成 seq 下限为 1（不产生 seq=0）**，与插件层 per-session 单调计数器（记录 lastDrivenSeq，每次驱动 +1，且保证 > 0）的 >0 保证一致，避免与真实事件首号冲突。若 session.events 不可靠，则在插件层维护该 per-session 单调计数器（每次驱动 +1 且 > 0）。合成事件**不 append 到 session log**，因此这一 seq 只用于 drive 排序/去重，不持久化。
- **seq 边界（in-memory-only / best-effort）**：registry.drive 使用 event.seq，可能把幻影水位（最后一个已提交事件之后的一号）折进 cell.observedSeq。**registry.drive 的跨单元副作用**：drive 会推进**每个已注册投影单元**的 cell.observedSeq（即便该单元 apply 返回同一 state 引用）；因此其它单元必须遵守 Same-reference gate（无关事件返回同一引用），避免被本合成事件误判为已观测。同时 `snapshot(session).asOfSeq` 仍为 `session.seq - 1`（真实日志水位），而 `snapshot(session).state` 的值已反映合成 seq 的状态——该读不一致（asOfSeq 与 state 非严格对齐）属已接受的 minor 不一致。**本投影是 in-memory-only**：合成事件仅经 registry.drive 折叠、不写入 session 日志；若宿主挂载投影缓存（如 `@deepseek-ai/dsh-session-projection-cache`），该缓存可能在 turn/end 对 observedSeq 打点，并在冷读 self-heal 时做全量重读而重置全部合成状态，导致投影跨重启丢失——这是本 in-memory-only / best-effort 单元**已接受的降级**。实现不得声明任何跨重启持久性。
    - **timestamp**：`new Date().toISOString()`（或 Date.now()）。
- **合成事件送入投影 fold（只走 registry 公开 drive）**：**仅**调用投影 registry 的公开驱动路径 `registry.drive(session, event)` 直接折叠——该路径只折叠 projection 并通知 change feed，**绝不**触碰 session-store / 持久化 bus。**明确禁止**用 `ctx.emit('session/event', session, event)` 送合成事件：dsh 的 session store / persistence 订阅同一 bus，且其信封检查（type/seq/time/data）不拒绝未知但合法的类型，会把 ocsr/jobs:update append 进 session log，违反“合成事件不 append / 不持久化”不变量。若该公开 drive seam 抛错/未定义/未挂载 → 视为 **best-effort 的 in-memory 部分提交**：drive 内部可能在抛错前已把 cell 更新为本次合成状态，插件层不回滚、不伪造 refold，记录 warning 并丢弃本次更新；默认行为是 drive 失败即丢弃该次更新。不得把 degrade 描述为“由 apply 在下一真实事件重读 jobs 并 refold”——apply 是纯函数、无 ctx/jobs 访问。若确需在下一真实事件时补读，只能由插件层在下一真实事件的驱动回调里重新 jobs.list 并合成新事件（按上述 seq/timestamp 规则构造），列为可选增强，不承诺必做。
  - 为避免污染持久化 session log：合成事件**不调用 session.append**，仅经 `registry.drive` 做投影驱动；标记专用类型 ocsr/jobs:update，其余 session-projection 订阅方/持久化方忽略。由于不经 session/event bus，持久化方不会看到该事件。
- **投影缓存守卫（明确不承诺持久性）**：在装配期做**确定性探测**——通过 `ctx.get('sessionProjectionCache')`（存在即宿主挂载了会持久化 projection 的缓存，如 `@deepseek-ai/dsh-session-projection-cache`）判定，不依赖 definition/`stateVersion`/checkpoint/cold-read 等启发式；注册投影单元本身仍以 `ctx.get('sessionProjections')` 可用为前提。若缓存存在且会把幻影 watermark 持久化：默认**跳过注册（降级为 no-projection）**并记录 warning；若实现选择仍注册，则必须接受“turn/end 打点幻影 observedSeq + 冷读全量重读重置合成状态 + 重启丢失”这一降级，且**绝不声明任何持久性**。两者都不得宣称投影跨重启存活。
- **可诊断/降级**：
  - 无 sessionProjections / jobs / sessions（任一缺失）→ 跳过注册并 logger.warn，其余 ocsr 功能不受影响。
  - register 抛错（重复 key、stateVersion 冲突等）→ catch + warn，不冒泡。
  - onJobsChanged 回调内异常 → 由 jobs registry 自带 containment 兜底；本插件再 wrap 一次并 warn。
  - 合成事件驱动失败 → 视为 **best-effort 的 in-memory 部分提交**：drive 抛错前可能已更新 cell，插件层不回滚、记录 warning、丢弃本次更新（因为 apply 纯函数无法重读 jobs，不伪造“已 refold”）。
- **诚实边界**：该投影是**只读观测**，不产生/修改任何 ocsr 派发语义；它把 job 可见状态“映射”进会话历史视图。任何调度/看门狗/错误切换/ledger 语义仍只在 scripts/ocsr_dispatch.py。

### 3.3 settings 白名单门（lib/settings-ocsr.js，独立插件 ocsr-settings）

- **决策**：选择**独立**的 ocsr-settings 插件，**移除**“可选 merge into tool-ocsr.js”分支。单一拓扑：settings 插件负责“注册 dsh-ocsr namespace + 暴露谓词”，tool 插件负责“调用谓词门”；职责分离，避免步骤 4 与步骤 5/9/验收的矛盾。
- **lib/settings-ocsr.js 导出**：
  1. **Cordis 插件**：name='ocsr-settings'，inject: []（运行时 ctx.get('settings')；缺 settings 时跳过注册并记 warning，不炸插件）。apply(ctx) 中先 `const settings = ctx.get('settings'); if (!settings) { logger.warn('ocsr-settings: settings unavailable, skip registration'); return; }`；随后 `settings.register('dsh-ocsr', schema, { base: entry })`；**不得使用属性访问器 `ctx.settings`**（不可达时 throw），必须经 `ctx.get('settings')` 判空后调用 `settings.register(...)`；schema（schemastery）：{ allowedModels: array(string).optional() }（**不 default([])**）。**`entry` 的来源 = ocsr-settings 插件自身的 composition 配置对象**：即该插件 `apply` 时传入的 config 参数（plugin 在 composition 中被解析出的 entry 配置对象，通常为 `{}`）。它作为 dsh-settings 的 priority base 层贡献；它**不是** driverBaseModels 的 config/allowed-models.json；两者 base 语义必须分离。
  2. **loadDriverBaseModels()**：读取 config/allowed-models.json，与 driver 相同的 ALLOWED_MODELS_PATH 语义（相对仓库根 `config/allowed-models.json`，等价于 scripts/ocsr_dispatch.py:144 的 parents[1]）。文件缺失（OSError）、JSON 解析失败、数组为空、包含非 `provider/model` 字符串、或存在重复项 → 返回 `[]` 并 logger.warn（fail-closed，与 driver import 对缺失/非法文件 fail 的行为一致，只是 JS 侧不 throw、以空集可诊断）。这是 **driverBaseModels 的唯一 JS 出口**，tool-ocsr 不再自行读文件。
  3. parseWorkerModel(worker)：与 driver 一致解析 PROMPT_PATH|MODEL|LABEL。即按 | **最多切两次**（Python w.split("|", 2)，maxsplit=2），取中段为 MODEL；JS 用两次 indexOf/slice 做等价实现。段数不足或中段为空 → 返回 null（fail-closed）。
  4. effectiveAllowedModels(driverBaseModels, userOverride)：
     - driverBaseModels：**由调用方传入**（生产环境应传 loadDriverBaseModels() 的返回值；测试可注入桩值）。读取/解析失败 → fail-closed（返回空集，拒绝放行）。
     - userOverride：由调用方（tool 顶部模型门）在确认 settings 服务可用后经 `settings.get('dsh-ocsr')?.allowedModels` 取得；settings 服务不可用时视为 undefined（回退 driverBaseModels）。resolved 值已折叠 schema 默认、settings priority base、用户 section；缺席/undefined → 返回 driverBaseModels（不额外收窄，零回退）。
     - 配置为非空数组 → intersection(driverBaseModels, userOverride)，只能收窄。
     - 配置为空数组 → 返回空集（视为“显式关闭”），前置门拒绝所有 dispatch。
- **接线**：lib/tool-ocsr.js import { loadDriverBaseModels, effectiveAllowedModels, parseWorkerModel } from './settings-ocsr.js'，在 execute 顶部先 `const driverBaseModels = loadDriverBaseModels()` 再做模型门（前台与后台都走）；取 settingsAllowedModels 前先 `const settings = ctx.get('settings'); if (!settings) { settingsAllowedModels = undefined; } else { settingsAllowedModels = settings.get('dsh-ocsr')?.allowedModels; }`（不得用 `ctx.settings` 属性访问器）；不通过即返回 { ok:false, kind: args.background ? 'background' : 'foreground', jobId: null, code:3, stderr } 可诊断，不启动进程、不注册 job。
- **两个 base 语义分离**：driverBaseModels（驱动白名单文件，config/allowed-models.json，与 driver 同源，经 loadDriverBaseModels() 读取）≠ settings 解析的 priority base 层（注册方 entry-config，即 ocsr-settings 插件的 `entry`）。两者不可互换。
- 前置门规则：worker 在工具 schema 中为 **required:true**，因此每次调用都会进入模型门，不存在“worker 未提供 → 不介入”的空路由。worker 提供但**不可解析** → fail-closed；解析出 model 但不在 effectiveAllowedModels 内 → fail-closed。
- 明示：这仅是“格式映射 + 交集校验”的**最小映射**，不复制 ocsr_dispatch 的语义/调度逻辑；任何语义变更仍只在 python 侧。

### 3.4 薄壳原则

- 以上所有新增均为“启动句柄 / 只读观察 / 配置门 / 只读投影”，无任何调度、看门狗、错误切换、ledger 语义复制；这些仍归 ocsr_dispatch.py。session projection 只做只读观测，绝不成为第二条编排路径。

## 实施步骤

1. 前置基线：复核当前 @deepseek-ai/dsh 0.1.1-rc.2 的 jobs/settings/sessions/sessionProjections 接口（本计划已列）；读取 lib/tool-ocsr.js、package.json、cordis.patch.yml、refs/dsh-integration.md 现状。
2. lib/tool-ocsr.js：schema 增加 background，并**修正 worker 描述**（PROMPT_PATH|MODEL|LABEL）；**inject 保持 ['tools']**，运行时用 ctx.get('jobs')/ctx.get('settings') 取可选服务（不得使用 ctx.jobs/ctx.settings 属性访问器；settings 不可用时模型门以 driverBaseModels 回退）；从 settings-ocsr 导入 loadDriverBaseModels/effectiveAllowedModels/parseWorkerModel，在 execute 顶部对前台/后台统一做模型门；新增 buildBackgroundSpec，把 jobs.start 的 run 封装成进程句柄（run 返回 { cancel, done, readOutput }）；jobs.start 整体 try/catch；**所有 background 返回（含失败）携带 kind:'background' 与 jobId（null|string）**；扩展 output.schema（kind/jobId，command/code/stdout/stderr 可选）；renderResult 统一按 kind==='background' 分支（含失败）且不打印 command: undefined；前台/后台/禁用三态均有可诊断返回。
3. 新增 lib/settings-ocsr.js（独立 ocsr-settings Cordis 插件，inject: []；apply 内先 `const settings = ctx.get('settings'); if (!settings) { skip注册+logger.warn; return; }` 再 `settings.register('dsh-ocsr', schema, { base: entry })`），**并 export** loadDriverBaseModels()、effectiveAllowedModels(driverBaseModels, userOverride) 与 parseWorkerModel(worker)。**不做“可选并入 tool-ocsr.js”分支**（单一拓扑）。loadDriverBaseModels() 为 driverBaseModels 的唯一 JS 出口。
4. 新增 lib/projection-ocsr.js（ocsr-projection 插件，inject: []；运行时可选 ctx.get('sessionProjections')/ctx.get('jobs')/ctx.get('sessions')；注册 ocsr.jobs 投影单元，订阅 jobs.onJobsChanged，合成 ocsr/jobs:update whole-value 事件并驱动 fold；**sessionProjections/jobs/sessions 任一不可用则跳过注册、记 warning、不炸**）。jobSnapshot 字段与 wire.view 子集按 3.2 定义；合成事件的 seq/timestamp 由插件层设定，且只经 registry 公开 drive 送入、不得经 session/event bus。
5. cordis.patch.yml：追加 ocsr-settings 与 ocsr-projection 两行（ocsr-skill、ocsr-tools 已存在；顺序为 ocsr-skill/ocsr-tools/ocsr-settings/ocsr-projection）。
6. package.json：files 已含 lib，无需改；peerDependencies 增加 @deepseek-ai/dsh-jobs、@deepseek-ai/dsh-jobs-local（实际 registry 实现）、@deepseek-ai/dsh-tool-jobs、@deepseek-ai/dsh-settings、@deepseek-ai/dsh-session（宿主 session-store，即 `ctx.get('sessions')`）、@deepseek-ai/dsh-session-projection（均 ^0.1.1-rc.2），并用 peerDependenciesMeta 把 dsh-jobs/dsh-jobs-local/dsh-settings/dsh-session/dsh-session-projection（以及 dsh-tool-jobs）标为 optional（不因 profile 缺这些服务而破坏安装）；exports 增加 ./settings 与 ./projection（如独立模块）。
7. refs/dsh-integration.md：Phase 3 更新为“已实现（jobs 后台化 + settings 白名单门 + session projection 只读投影）”，注明“白名单只收窄不扩展”“job 可观察/终止需挂载 dsh-tool-jobs”“projection 为只读/in-memory-only / best-effort，registry/jobs/sessions 任一不可用或宿主挂载投影缓存时降级”“driverBaseModels ≠ settings priority base”与版本耦合。
8. 文档接线：docs/CURRENT.md 记录；如需在 docs/STRUCTURE.md 登记新的 refs 说明（若新增 refs）。AGENTS.md 硬约束维持“只映射/包装”。
9. 机械验证：node --check 全部 lib/*.js；仓库验证 verify_ocsr_skill.py、agent_links.py check、git diff --check；临时 profile dsh plugin --profile ocsr-p3 add . 后 --dump-config 出现 ocsr-skill/ocsr-tools/ocsr-settings/ocsr-projection 四行；进程内 smoke：stub defineTool + 假 jobs/settings/sessionProjections/sessions 验证 background 分支调用 jobs.start（含 start 抛错→ok:false branch）、settings 未配置回退 driverBaseModels / 显式空关闭 / 交集门（settings 判空经 ctx.get('settings')，不触发 ctx.settings 属性访问器）、--worker 解析一致性与不可解析 fail-closed、session projection 在 registry+jobs+sessions **三者均存在**时注册 ocsr.jobs 单元并 refold 合成事件（含验证合成事件带 seq/timestamp），**并断言三者任一缺失时插件加载不炸且注册为跳过**；真实 job 联调（可选，需模型消耗）后台派发→job_output 观察→job_kill 终止，并确认目标 profile 已挂载 dsh-tool-jobs。

## 验收标准

- 未修改 SKILL.md、scripts/ocsr_dispatch.py；git diff --stat 仅含适配层文件与文档。
- ocsr_dispatch 在 background 为 true 时返回 { kind:'background', jobId } 且（在挂载 dsh-tool-jobs 的目标 profile 中）job 可被 job_list/job_kill 观察/终止（进程内 smoke + 可选联调）。
- settings dsh-ocsr.allowedModels 能被读取并作为交集门；未配置时回退 driverBaseModels（对现有行为无回退），显式空数组时主动关闭；driver ALLOWED_MODELS 之外的模型无法放行；--worker 不可解析 fail-closed。**实现须经 `ctx.get('settings')` 判空后调用 `settings.get(...)`（全库不得使用 `ctx.settings` 属性访问器）；settings 服务不可用时模型门回退 driverBaseModels、ocsr-settings 跳过注册并 warn。**
- **session projection**：
  - registry + jobs + sessions 可用时，ocsr.jobs 投影单元注册成功并提供只读 wire.view（viewSchema/view），且 ocsr/jobs:update 合成事件会把新的 job 可见集合折叠进状态；jobSnapshot 按 3.2 字段定义，wire.view 只暴露子集。
  - 合成事件的 seq/timestamp 由插件层构造；apply 为纯函数，不重新读取 jobs；onJobsChanged 驱动失败时视为 **best-effort 的 in-memory 部分提交**（drive 抛错前可能已更新 cell，插件层不回滚、不伪造 refold、丢弃该次更新），不承诺在 apply 内 refold。
  - 合成事件**只经 registry 公开 drive(session, event)** 送入，**禁止**用 ctx.emit('session/event', ...) 走 session/event bus；否则会被持久化方 append 进 session log。
  - 合成事件仅经 registry.drive 折叠、不写入 session 日志。**投影缓存守卫**：默认行为（与 §3.2 一致）是在确定性探测到 `ctx.get('sessionProjectionCache')`（宿主挂载会持久化投影的缓存）时**跳过注册（no-projection）**；仅当实现选择“仍注册”的非默认分支时，才适用“turn/end 打点幻影 observedSeq + 冷读全量重读重置 + 重启丢失”这一降级（此时该 best-effort 投影可能被缓存冷读时的全量重读重置，属已接受降级）；本投影不声明跨重启持久性。
  - registry / jobs / sessions 任一不可用（或 register 抛错）时，ocsr-projection 跳过注册、记录 warning、不崩溃，ocsr-tools 继续正常工作。
  - 投影为只读、in-memory-only / best-effort、observation-only：不 append 会话日志、不复制任何派发语义、不声明跨重启持久性。
- **render 修正**：background 失败返回 { ok:false, kind:'background', jobId:null, code, stderr }（带 kind/jobId）时，renderResult 按 kind==='background' 分支渲染且不输出 command: undefined。
- 仓库既有验证（verify/agent_links/diff --check）通过；node --check 通过。
- 在临时 profile 的 --dump-config 中四行（ocsr-skill/ocsr-tools/ocsr-settings/ocsr-projection）均存在，且不破坏 web/内置 profile（无 jobs/settings/sessions/sessionProjections 时 ocsr-tools 仍可加载并降级）。

## 风险与裁决点

- 白名单只能收窄：driver 无 env 覆盖缝，settings 覆盖不能扩展。若用户要“dsh 可扩展白名单”，需给 driver 加最小 env/arg 缝（触及核心，需单独评审）；本计划默认不做。
- jobs/settings/sessions/sessionProjections 均为可选依赖的版本耦合：契约以 0.1.1-rc.2 为准；升级宿主前需复核。缺少 jobs/settings/sessions/sessionProjections 时 ocsr-tools 必须能加载并把对应功能降级为可诊断 ok:false（projection 则跳过注册）。
- job 生命周期：run.cancel/done 必须保证进程退出后 resolve；exec.signal 取消要同时 kill 子进程、避免僵尸。
- **job 可观察/终止的前提依赖**：dsh-tool-jobs 必须实际挂载在目标 profile；未挂载则本地 job 仍注册但 model 侧无操作工具。联调环境须先验证该挂载。
- **jobs.start 抛错面**：owner 无 served controller、maxConcurrentJobsPerOwner、空 kind/label 等需捕获并返回可诊断 ok:false，不能冒泡导致插件失败。
- 不炸 web 组合：web profile 把部分工具/设置挪到 preset 之后，注册失败时应 fail-closed 于可诊断错误而非静默放行。
- --worker 前置门是**格式映射的最小复制**，仅用于提取 model；解析失败必须 fail-closed。不要把该门扩大为对 driver 的语义副本。
- **session projection（in-memory-only / best-effort）**：
  - 合成事件不进入持久化 session log，因此投影只对“注册后观察到的 job 变更”生效，无法从历史日志重放；这是有意的降级，不承诺时延/持久性。
  - **sessions 是注册必需**：投影还需宿主 `ctx.get('sessions')`（session-store）把 owner 解析成 session；sessionProjections / jobs / sessions 任一缺失都导致跳过注册（不注册 ocsr.jobs 单元），但 ocsr-tools 仍可正常工作。
  - **合成事件禁止经 session/event bus**：不得用 `ctx.emit('session/event', session, event)` 送投影事件，必须直接调 registry 的公开 `drive(session, event)`；否则会被 session store/持久化方 append 进 session log。
  - **合成 seq 是 in-memory-only 水位**：registry.drive 使用 event.seq，可能折进 cell.observedSeq（幻影水位，最后一个已提交事件之后的一号）。本投影明确不持久化：若宿主挂载投影缓存，turn/end 打点与冷读全量重读可能重置合成状态，属已接受降级；不得声明跨重启持久性。
  - **投影缓存守卫**：宿主若挂载会持久化投影的缓存（如 `@deepseek-ai/dsh-session-projection-cache`），应经 `ctx.get('sessionProjectionCache')` 做确定性探测，探测到即默认跳过注册（no-projection）；若仍注册，须接受“幻影 observedSeq 打点 + 冷读全量重读重置 + 重启丢失”，不声明持久性。
  - **驱动 seam 不可靠时**：onJobsChanged 驱动失败 → 视为 **best-effort 的 in-memory 部分提交**：drive 内部可能在抛错前已把 cell 更新，插件层不回滚、记录 warning、丢弃本次更新；不得描述为“由 apply 在下个真实事件重读 jobs 并 refold”（apply 纯函数无 ctx/jobs）。若确需补读，只能由插件层在下一真实事件驱动回调里重读 jobs 并合成事件，且合成事件须带 seq/timestamp。
  - stateVersion 需在改变投影字段/折叠语义时 bump，否则持久化 cache 可能复用旧版状态。
  - 不把投影当作第二条编排路径；任何调度/看门狗/错误切换语义仍只在 scripts/ocsr_dispatch.py。

## 评审处置记录

- Round 1 处置（对应本次修改）：
  - B1（架构性）：修订 3.1/3.2/3.3 与实施步骤 2-4；jobs/settings/sessionProjections 不再写入 cordis inject 硬依赖。ocsr-tools 的 inject 保持 ['tools']，jobs/settings 改为运行时 ctx.get('jobs')/ctx.get('settings') 降级获取；使 profile 缺服务时插件仍可加载，保护 Phase 1/2 与 web profile 不回退。
  - B2（实现细节）：3.1 的 output.schema 增加 kind/jobId，并把 command/code/stdout/stderr 改为可选；renderResult 增加 background 分支。
  - B3（实现细节）：3.3 的 allowedModels 由 default([]) 改为 optional()；明确区分“未配置（回退 driver ALLOWED_MODELS，不改变现有门）”与“显式空数组（主动关闭）”，消除对当前行为的回退。
  - B4（结构）：session projection 从“四点”降级为显式非目标；删除其验收标准，取消 lib/projection-ocsr.js 与 ocsr-projection 注册，不引入 sessionProjections 依赖；在非目标、3.2、实施步骤、验收、风险中同步一致。
  - 附加收敛：
    - 白名单前置门按 scripts/ocsr_dispatch.py:954 解析 --worker（PROMPT_PATH|MODEL|LABEL，按 | 取中段）；不可解析时 fail-closed；并明示“仅是格式映射，非语义复制”。
    - 增加“job 可观察/终止需目标 profile 实际挂载 dsh-tool-jobs”的验证注记（3.1、实施步骤 9、风险）。
    - ctx.jobs.start 包 try/catch（owner 无 served controller、maxConcurrentJobsPerOwner、空 kind/label 可抛），失败返回 ok:false 可诊断。

- **Round 2 处置（本次修改）**：
  - **B1c（概念性，恢复）**：把 Round 1 降级为“非目标”的 session projection **恢复为范围点 #2**，采用“只读、best-effort 投影单元”替代路径；重写 3.2 为完整 register(definition) 契约（key/stateSchema/init/apply(state,event)/wire{viewSchema,view}/stateVersion），并说明如何把 ctx.jobs.onJobsChanged 生命周期事件合成成 ocsr/jobs:update 事件送入 apply；重新加入投影验收标准（只读、best-effort、registry 不可用降级不炸）与 lib/projection-ocsr.js + ocsr-projection 行。全程不修改 SKILL.md / scripts/ocsr_dispatch.py，不把派发语义复制进 JS，honest 标注 observation-only。
  - **B2（结构，收敛）**：settings 门分解矛盾剔除。选定**独立 ocsr-settings 插件**（移除旧 3.3（settings）/步骤 4 的「可选 merge into tool-ocsr.js」分支）；明确 wiring：lib/settings-ocsr.js 同时导出 ocsr-settings Cordis 插件（注册 dsh-ocsr namespace）与 effectiveAllowedModels(driverBaseModels, userOverride) + parseWorkerModel(worker) 谓词；lib/tool-ocsr.js 导入谓词并在 execute 顶部对**前台与后台**统一调用门。使步骤 5/9 与验收标准与该单一拓扑一致。
  - **附加收敛**：
    - --worker 解析采用与 driver 一致的 split('|', 2)（Python maxsplit=2，最多切两次），取中段为 MODEL；JS 用两次 indexOf/slice 等价实现。
    - 明确 JS 读取同一 config/allowed-models.json 作为 driverBaseModels；并把 driverBaseModels（驱动白名单文件）与 dsh-settings priority base 层（settings 解析的注册方 entry-config）两组 base 语义分离命名，消除歧义。
    - background 失败返回 { ok:false, code, stderr }（无 command/kind）增加 render 分支，避免打印 command: undefined。
    - 工具 schema 的 worker 描述修正为 PROMPT_PATH|MODEL|LABEL 三段格式。

- **Round 3 处置（本次修改）**：
  - **B3-1（结构）driverBaseModels source/export 不一致**：3.1 曾称“settings-ocsr 读取同一 config/allowed-models.json 作为 driverBaseModels”，但 3.3 的导出面只有 parseWorkerModel(worker) 与 effectiveAllowedModels(driverBaseModels, userOverride)，未导出任何 driverBaseModels 加载器/常量，导致 tool-ocsr 只能自行读文件，破坏“settings-ocsr 是白名单读取唯一出口”的约束。**修复**：lib/settings-ocsr.js 新增并导出 `loadDriverBaseModels()`；它按 driver 的 ALLOWED_MODELS_PATH 语义读取 config/allowed-models.json（OSError/JSONDecodeError/空数组/非法 item/重复项 → 返回 [] 并 warn，fail-closed）。3.1 改为在 execute 顶部 `const driverBaseModels = loadDriverBaseModels()` 后代入 effectiveAllowedModels；3.3 的导出列表、接线、实施步骤 3 与验收同步。
  - **B3-2（实现）background 失败渲染分支不可达**：旧 3.1 说 background 失败返回 `{ ok:false, code, stderr }`（无 command/kind），而 renderResult 的 background 分支只在 kind==='background' 时触发，导致该失败永远落入前台分支并可能打印 command: undefined。**修复**：所有 background 返回（含失败）统一携带 `kind:'background'` 与 `jobId`（失败为 null；jobs 不可用 / start 抛错均如此）；前台返回亦统一带 `kind:'foreground'`、jobId:null；renderResult 按 kind==='background' 统一分支（含失败），command 仅在 !== undefined 时打印。
  - **B3-3（结构）投影 degrade 路径不可实现**：旧 3.2 说“若 drive seam 不可靠，在下一个真实 session/event 驱动时由 apply 重新读取 jobs 并 refold”，但 register(definition) 的 apply(state,event) 是纯函数、无 ctx/jobs 访问，无法重新读取 jobs。**修复**：把 degrade 改为“onJobsChanged 驱动失败 → **丢弃该次更新并保持上一状态（best-effort）**”；同时明确合成事件的 seq/timestamp 由插件层在构造事件时设置（seq 取 session 已提交事件序列 +1 或插件层 per-session 单调计数器，timestamp 为 ISO/Date.now()）；可选增强是“下一真实事件时由插件层重读 jobs 并合成事件”，但明确不是 apply 内部行为。非目标、风险、验收同步更新。
  - **附加收敛**：
    - 3.3：明确 settings register 的 `{ base: entry }` 中 `entry` 的来源 = ocsr-settings 插件所载 composition 的 entry 配置（priority base 层），与 driverBaseModels 的 config/allowed-models.json 严格区分。
    - 3.2：定义 jobSnapshot 具体字段（`{ id, kind, status, label, outputRef?, owner?, detail?, startedAt?, finishedAt? }`）与 wire.view 子集（不含 outputRef/owner）。
    - 实施步骤 6：peerDependencies 增加 `@deepseek-ai/dsh-jobs-local`（实际 registry 实现），与 dsh-jobs/dsh-tool-jobs 一并列出，并在 peerDependenciesMeta 中标为 optional。

- **Round 4 处置（本次修改）**：
  - **B4-1（结构，BLOCKING）投影驱动 seam 不得经 session/event bus**：3.2 曾把“调用 registry drive 路径”与“等价地经 ctx.emit('session/event', session, event)”并列为等价 seam。实际 dsh 的 session store / persistence 订阅同一 bus，且信封检查（type/seq/time/data）不拒绝未知但合法的类型，因此经 ctx.emit 送出的 ocsr/jobs:update 会被 append 进 session log，违反“合成事件不 append / 不持久化”不变量。**修复**：3.2 改为只允许直接调用投影 registry 的公开 `drive(session, event)`（只折叠 projection + 通知 change feed，不触碰 session-store/持久化 bus），并明确禁止 ctx.emit('session/event', ...) 用于本目的；drive 抛错时保留“丢弃该次更新、保持上一状态（best-effort）”的降级。
  - **附加收敛**：
    - 3.1：worker 为 required:true，模型门总是在每次调用执行——统一 3.1“when worker provided”与 3.3“worker 未提供→no-op”表述，移除后者的空路由；补一行 buildBackgroundSpec 的 run 返回 `{ cancel, done, readOutput }`（镜像 dsh-tool-bash lib/index.js ~414）。
    - 3.2：明确合成事件 seq 由 registry.drive 使用、可能流入 cell.observedSeq / 检查点；强调该观测单元为 best-effort 且**不持久化**（或约束合成 seq 不写入幻影 observed 检查点）。
    - 清理：移除正文部分所有内联评审轮次/问题标签，仅保留结构化“评审处置记录”节。
- **Round 5 处置（本次修改，blank-slate 复核）**：
  - **B5-1（BR-1，BLOCKING）投影不持久化 → in-memory-only / best-effort 显式化**：原 3.2/验收把投影描述为“best-effort 且不持久化”，但 `registry.drive` 会把合成事件 seq 折进 `cell.observedSeq`（幻影水位，最后一个已提交事件之后的一号）；若宿主挂载 `@deepseek-ai/dsh-session-projection-cache`，其 checkpoint() 会在 turn/end 持久化该幻影 observedSeq，冷读 self-heal 又会全量重读而重置合成状态，导致“本投影不持久化”与实际可被缓存持久化相矛盾。**修复**：把 `ocsr-projection` 显式定义为 **in-memory-only / best-effort**，只记录存活进程内的 job 状态、不承诺跨重启持久性；删除“不把幻影 seq 写进 observed 检查点”验收表述，替换为“合成事件仅经 registry.drive 折叠、不写入 session 日志；若宿主挂载投影缓存，该 best-effort 投影可能被缓存冷读时全量重读重置，属已接受降级”；在 3.2 与风险增加投影缓存守卫：探测到会持久化 watermark 的缓存时默认跳过注册（no-projection）或注册并接受 transient watermark + 重启丢失，但绝不声明持久性。
  - **B5-2（实现）jobSnapshot 移除 outputRef**：`outputRef` 从不来自 jobs.list 快照，从 3.2 的 jobSnapshot 字段定义中移除；wire.view 相应不再提及 outputRef（此处置覆盖 Round 3 对 jobSnapshot 的旧字段描述）。
  - **B5-3（实现）3.3 `entry` 语义**：明确 `entry` = ocsr-settings 插件自身 `apply` 传入的 composition 配置对象（config 参数），通常 `{}`，与 driverBaseModels 的 config/allowed-models.json 严格分离。
  - **B5-4（实现）done 结果形状**：3.1/buildBackgroundSpec 明确 `done` 在进程退出后 resolve 为 `{ status, detail?, output? }`。
  - **B5-5（实现）drive 失败降级**：把“drive 失败 → 丢弃更新”明确为 best-effort 的 in-memory 部分提交（drive 抛错前可能已更新 cell），插件层不回滚、不伪造 refold。
  - **B5-6（清理）**：移除状态头中的内联 Round 引用（仅保留结构化“评审处置记录”）。

- **Round 6 处置（本次修改，blank-slate 复核 BR-2）**：
  - **BR-2（结构，BLOCKING）dsh settings 访问不得用属性访问器 `ctx.settings`**：计划在 3.1 用 `ctx.settings.get(...)`、3.3 用 `ctx.settings.register(...)`，但 dsh Cordis 语义下 `ctx.settings` 属性访问器在服务不可达时 throw，破坏“settings 缺失时优雅降级”（模型门回退 driverBaseModels / ocsr-settings 跳过注册+warn）的约束。**修复**：3.1 模型门与 3.3 ocsr-settings.apply 均改为 `const settings = ctx.get('settings'); if (!settings) { <fallback/skip+warn> }` 后调用 `settings.get(...)` / `settings.register(...)`；同步 3.1 inject 注记、实施步骤 2/3 与验收/冒烟。
  - 附加收敛：
    - 3.2 / 风险：补 registry.drive 的**跨单元副作用**——drive 会推进**每个**已注册投影单元的 cell.observedSeq（即便 apply 返回同一引用），其它单元必须遵守 Same-reference gate；并注明 `snapshot(session).asOfSeq` 保持 `session.seq - 1`（真实日志水位）而 state 值已反映合成 seq 的 minor 读不一致。
    - 3.1：钉住生产者 readOutput 契约——buildBackgroundSpec 的 run 返回 `{ cancel, done, readOutput }`；readOutput 每次调用排空子进程 stdout/stderr、返回**增量（delta）**，调用方自行累加得**全量（full）**。
    - 3.2 / 风险 / 验收：投影缓存守卫改用**确定性探测** `ctx.get('sessionProjectionCache')`（存在即挂载会持久化投影的缓存），不再依赖 definition/stateVersion/checkpoint 启发式。

- **Round 7 处置（本次修改）**：
  - **B7-1（结构，BLOCKING）§3.2 不得用属性访问器 `ctx.jobs.list(owner)`**：§3.2 曾用 `ctx.jobs.list(owner)` 从可选 Cordis 服务取可见 job，属与 BR-2 同类的属性访问器缺陷（服务不可达时 throw，破坏“jobs 缺失时优雅降级”）。**修复**：§3.2 改为一律先用 `const jobs = ctx.get('jobs')` 取得引用（不可用则跳过本轮，best-effort），再以该引用调用 `jobs.onJobsChanged(owner)` 与 `jobs.list(owner)`（或判空等价地 `ctx.get('jobs')?.list(owner)`），与 §3.1 的 `const jobs = ctx.get('jobs')` 模式一致。
  - 附加收敛：
    - 全计划设计正文扫除 `ctx.jobs`/`ctx.settings` 属性访问器用法：规范代码一律改用已取得引用或判空后的 `ctx.get('jobs')`/`ctx.get('settings')`；剩余的历史“评审处置记录”条目仅用于记述既往缺陷与修复，保持原样不作为现行用法。
    - 关键 dsh 事实：统一命名，把一处独用的 `driverBaseAllowedModels` 改为 `driverBaseModels`，与正文其余表述一致。

- **Round 8 处置（本次修改）**：
  - **B8-1（结构，BLOCKING）`sessions` 未传播到依赖/验收/验证清单且注册前置不一致**：投影单元依赖 `ctx.get('sessions')`（owner→session 映射），但 `sessions` 未列入关键 dsh 事实、版本耦合依赖说明、步骤 6 peerDependencies 与验收/冒烟清单；且注册前置不一致——§3.2 已写“sessionProjections/jobs/sessions 任一不可用时跳过注册”，而验收/步骤仅以 registry+jobs 为准，步骤 9 smoke 也只 stub 了 jobs/settings/sessionProjections（未 stub sessions）。**修复**：
    - 关键 dsh 事实新增 `ctx.get('sessions')` 条目（@deepseek-ai/dsh-session，宿主 session-store 服务；投影用它做 owner→session 解析；是注册必需三服务之一；不得用 `ctx.sessions` 属性访问器）。
    - 版本耦合依赖说明、步骤 1/4/6/7、非目标与一句话可验证标准、§3.1 inject 注记、验收与风险清单同步把 `sessions` 纳入；步骤 6 peerDependencies 增加 `@deepseek-ai/dsh-session`（^0.1.1-rc.2）并在 peerDependenciesMeta 标 optional。
    - 注册前置统一为：**仅当 sessionProjections + jobs + sessions 三者均可用时注册**；验收改为“registry + jobs + sessions 可用时，ocsr.jobs 投影单元注册成功”；降解/风险行改为“任一不可用（或 register 抛错）时跳过注册”。
    - 步骤 9 smoke 增加 stub 假 `sessions` 服务，并断言三者均存在才注册、三者任一缺失时插件不炸且注册为跳过。
  - 附加收敛：
    - 投影缓存守卫：明确 §3.2 默认行为是“探测到会持久化投影的缓存即跳过注册（no-projection）”；验收同步——冷读全量重读重置仅适用于非默认“仍注册”分支。
    - §3.2：简化冗余的 `ctx.get('jobs')?.list(owner)` 回退——jobs 此前已判空守卫，直接以已取得引用 `jobs.list(owner)` 取值。
    - 模型门故障 `code` 钉值：1=background jobs 不可用、2=jobs.start 抛错、3=模型门拒绝/不可解析（任一非零也可，此处钉住便于诊断）。
    - §3.2 seq：合成 seq 下限为 1（**不产生 seq=0**），与插件层 per-session 单调计数器 >0 的保证一致，避免与真实事件首号冲突。
