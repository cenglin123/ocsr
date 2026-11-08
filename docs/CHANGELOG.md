# CHANGELOG

## 2026-11-08

### Board 性能：增量扫描与生命周期管理
#### 变更内容
- **增量扫描**：worker 目录以关键文件（worker-state/session-binding/start.marker/launcher.ps1/run.log）的 mtime+size 指纹缓存解析结果，内容未变不重读（进行中时长与 deadline 滞留判定仍每次重算）；dispatch-log.jsonl 记住偏移只解析新增完整行（不完整尾行留待下次，截断则全量重读）；缓存随扫描集淘汰。
- **目录遍历优化**：先按目录名纯字符串过滤再 is_dir/stat——修复大 TEMP（5 万+条目）下对全部条目逐个 stat 的瓶颈（实测 nt.stat 52,869 次→数百次；scan_workers 2.43s → 0.43s）。
- **单实例**：~/.ocsr/board.lock（pid+port），重复启动经进程命令行复核与 identity 识别后 KILL 旧实例重启；退出时清除锁文件。
- **空闲自动退出**：`--idle-timeout` 分钟无页面请求即退出（默认 30，0=禁用），孤儿面板不再常驻。
- 前后实测：同一组派发目录 /api/state 重建冷扫 529ms、无变化 warm 432–559ms（原 2.4–2.9s）；重复启动旧实例被 KILL 新实例接管；空闲 1 分钟超时自动退出且锁文件清除。
- 9 项新增离线测试（tests/test_board_incremental.py：指纹命中/失效、动态刷新、缓存淘汰、增量日志、截断重读、空闲退出、锁文件、单实例重启/忽略）；462 passed，verify 13/13、audit 通过。
- fresh 复查（deepseek/deepseek-flash）REVISE，8 项发现全部修复：缓存淘汰移至 build_state 统一执行（多根不再互清）、日志末行一致性校验防替换混合、单实例 KILL 移至新实例 bind 成功后且缺 port/identity 不符不动、缓存线程约束注释、文档可写契约补 board.lock；测试强化为打桩计数断言（消除假阳性）并补尾行/多根/守卫/空闲负向用例，470 passed（462+8）。报告存 .review/board-incremental-20261108/（gitignored），预算 2/2。
- 来源：handoff `C:\wt\handoff\20261108-ocsr-board-perf.md`（记忆底座会话转达）；计划 docs/plans/active/20261108-board-incremental-perf.md。

#### 迁移影响
- 面板进程生命周期完整：启动（命令/bat）→ 退出（按钮/关窗/空闲超时/同端口或锁文件重启）。--idle-timeout 默认 30 分钟，长期挂面板观察的会话需保持页面活动或调大。

### Board 使用性：退出按钮与双击启动脚本

#### 变更内容
- 工具栏新增「⏻ 退出」按钮：POST /api/shutdown（仅本机回环 + application/json，confirm 防误点），先回响应再异步关闭 server，优雅退出面板进程；页面显示重启指引。
- 仓库根新增 `启动面板.bat`：双击启动并自动开浏览器，关闭控制台窗口即退出进程。
- 使用模式明确：按需启动、不必常驻；无浏览器打开时零请求零 CPU；重复启动自动 KILL 旧实例同端口重启。
- 新增 ShutdownTest 端点测试（起 server → POST shutdown → serve_forever 退出）；453 passed，verify 13/13、audit 通过；退出按钮 playwright 实测（进程退出、端口关闭）。

#### 迁移影响
- 面板进程管理闭环：启动（命令/双击 bat）→ 退出（按钮/关窗/同端口重启）；无既有行为变更。

### instance_id 机械化获取（消除人工转录）
#### 变更内容
- `dispatch --watch` 落盘输出行附带 `session=<session_id>`（从 session-binding.json 轻量读取），上层编排可直接复制，不再人工读 JSON 转录。
- 新增只读子命令 `python scripts/ocsr_dispatch.py session-id --worker-dir <dir>`：输出 session_id，缺失/非法 fail-closed exit 2，供脚本化取用。
- 动机：2026-11-08 converge 流程三次人工转录错误导致归档 ledger-instance-conflict，且 instance_id 属 Archive Contract 不可修正字段。工具化消除该类失误（硬约束优先于纪律提醒）。
- 文档：refs/converge-integration.md 增加 instance_id 机械化获取规则；deployment.md 补 session-id 用法。
- 4 项合成测试（tests/test_session_id.py）；452 passed，verify 13/13、audit 通过。

#### 迁移影响
- dispatch stdout 落盘行新增 session= 字段（附加信息，不改变既有字段与退出码）；session-id 为新增只读子命令，不影响既有派发路径。

### Board converge 收敛与落地

#### 变更内容
- converge 收敛完成：评议（阻断需修复）→ 修复 → 复评（可执行）→ 盲审 1（阻断需修复）→ 修复 → 复评 → 盲审 2（可执行，零阻断）。收敛现场与轮次产物存 `.converge/active/ocsr-board/`。
- 本次收敛交付的实质修正：遥测归属区间上界（同 label 按 started_ts 划分互不重叠区间，旧 worker 不被下一次运行的终态行越界绑定）与 `reported:handoff_required` 终态值域（总期限交回的终态行参与绑定，其余 `reported:<status>` 中间行跳过）；/api/state 扫描缓存代次原子性（扫描期间配置写则放弃回填并重扫）；分组/归档/字节语义修正（拖放保存检查 resp.ok、空手动组可删除、归档计数与筛选同一口径）。
- 落地小修：返工按钮复制内容改为可执行命令模板（补全 `--worker` 与 `--output-dir`；原片段缺必填参数，粘贴后 argparse 必败），toast 注明需补全；遥测行与 worker-state 的非字符串/空 label 容错（`"label": []` 这类值曾使绑定按字典键抛 TypeError、/api/state 持续 500——`read_history` 跳过坏行，扫描侧以目录名兜底）；deployment.md 补遥测归属适用前提。
- 40 项面板测试（39+1，新增非字符串 label 全链路容错）；448 passed 全仓，verify 13/13、audit 通过。
- **归档未完成（诚实记录）**：finish 在 archive 步失败（ledger-instance-conflict——流程中三次 instance_id 转录错误致 spawn 身份重复；instance_id 属 Archive Contract 不可修正字段，经用户授权的 record-correction 亦被拒），实质收敛结论不受影响，未伪造归档成功状态，现场保留在 active/。

#### 迁移影响
- 面板行为与文档一致；无配置格式变更，board-groups.json 结构不变。同 label 多次运行的遥测归属在「后续运行不在同一扫描根内 / 目录已清理 / 超出 200 行尾窗」时不保证准确（末位 worker 无上界），deployment.md 已注明。

### Board 整体终审修复：遥测归属、缓存代次与分组生命周期

#### 变更内容
- 遥测归属修正：一次运行可写多条遥测（hierarchical_report 先写 reported 中间行再写终态行），绑定规则改为「启动后最早一条**终态行**（非 reported:）」，中间行的检查时刻时长与 log 大小不再被当成最终时长/产物字节；无终态行则不绑定。读取侧对非字符串 ts/outcome_detail 等外部输入做类型容错，不再因单条坏行导致 /api/state 500。
- 缓存正确性：_STATE_CACHE 增加代次 gen，扫描期间发生配置写则放弃回填并重扫，修复「配置写入立即使缓存失效」在并发下被旧扫描结果覆盖的竞态。
- 失败原因映射修正为 dispatcher 实际的 error:exit_code_N 形式（此前 error:exit_ 分支产生 exit=code_N 错位文案）。
- 分组生命周期：拖放保存检查 resp.ok（400 不再清空视图或假提示）；空手动组可在组标题「删除」清除分配；文档明确整组归档按工作目录持续生效（含后续新派发）。
- 其它：meta 轮询间隔动态显示；归档计数与已归档视图同一筛选口径；ACTIVE_STATES 死常量移除；POSIX 保留 allow_reuse_address（避免 TIME_WAIT 重启失败，Windows 仍禁用）；kill_pid 异常容忍；KILL 前二次核对目标进程命令行确为 ocsr_board.py（verify_board_process，防伪 identity 误杀）。
- 35 项面板离线测试（新增单次多行遥测归属、类型容错、代次竞态、空组删除实测）；443 passed，verify 13/13、audit 通过。整体终审（deepseek/deepseek-flash）REVISE，10 项发现全部修复并实测；报告存 .review/board-final-audit-20261108/（gitignored），调用预算 3/3（2 preflight + 1 派发；mimo 通道超时后切换 deepseek）。修补由主会话验收，未追加模型复评。

#### 迁移影响
- 面板行为与文档一致；无配置格式变更，board-groups.json 结构不变。

### Board：时间排序、列表视图、失败原因直显、归档工作流与扫描缓存

#### 变更内容
- 组内排序（启动时间倒序默认/正序/状态优先）与卡片/列表视图切换；列表含启动时间列，行内详情展开与拖动分组，详情展开跨轮询保持；排序/视图偏好存 localStorage。
- 失败卡片直显人性化原因（exit_0_no_artifact=进程正常退出但产物未落盘、exit_0_name_mismatch=文件名不符、非零退出码等）。
- 归档工作流：单卡/整组归档、批量「归档已结束」；「已归档」独立视图只看已归档项，单张/批量恢复。组归档被单张恢复时拆为逐卡归档（不静默整组恢复）；批量作用域含聚焦约束；归档计数按当前扫描结果口径；归档只写 localStorage，不删证据文件。
- 性能：/api/state 增加 10s TTL 扫描缓存（配置写入立即使缓存失效，fresh=1 强制重扫），前端自适应轮询（活跃 5s/空闲 15s）+ 手动刷新按钮。修复大 TEMP 目录（5 万+条目）与 Defender 实时扫描下单次全量扫描秒级、5s 轮询导致面板进程持续高 CPU 的问题：实测缓存命中响应 14–31ms（冷扫 2.4s）。
- fresh 复查（deepseek/deepseek-flash）REVISE，9 项发现全部修复并实测（详情跨轮询保持、聚焦批量不越界、组归档单张恢复、时间窗下界等）；报告存 .review/board-list-sort-20261107/（gitignored），调用预算 2/2。修补由主会话验收，未追加模型复评。
- 31 项面板离线测试（含缓存命中/失效行为）；439 passed，verify 13/13、audit 通过。

#### 迁移影响
- 归档状态存浏览器 localStorage（跨浏览器不共享）；面板数据有最长 10s 缓存延迟，可手动「⟳ 刷新」强制重扫。
- 目录扫描默认近 7 天窗口；更老记录随窗口/TEMP 清理退出主视图，归档不删除证据。

## 2026-11-07

### Board 交互增强：chips 直筛、时间筛选与文件夹管理

#### 变更内容
- 交互模式借鉴社区实现（opencode/Codex 的项目分组折叠与 show-more、Vibe Kanban 的 archive/搜索），不引入框架。
- 状态 chips 点击直筛（再点取消、选中高亮）；计数跟随其余筛选条件（facet 口径）。时间筛选（近 1h/今天/近 3 天/近 7 天/全部）按最近活动时间判定并含下界，作用于卡片与遥测历史。
- 文件夹组管理：折叠/展开、聚焦单组、整组隐藏（视图层）、组内默认 6 张 + 显示全部；状态持久化于 localStorage。
- 扫描根在线管理：/api/roots 增删自定义扫描根并持久化到 board-groups.json 的 roots 字段；默认 TEMP + CLI --work-dir + 自定义根叠加（--work-dir 为追加语义）；失效根以「已失效」列出且可移除。
- 独立 fresh 复查（deepseek/deepseek-flash）REVISE，13 项发现全部修复：显示已隐藏不再短路其余筛选、空手动组随整组隐藏消失、配置读-改-写加锁、跨根 worker key 加根哈希前缀隔离、失效根可管理、聚焦态折叠生效、时间窗下界、saveSet 容错、chips facet 计数、HTTP 端点契约测试（415/403/400/负 Content-Length/失效根往返）等。
- 30 项面板离线测试；438 passed（432+6），verify 13/13、audit 通过；chips/时间/折叠/聚焦/隐藏/show-more/roots 增删均 playwright 实测。评审报告存 .review/board-ux-20261107/（gitignored），调用预算 2/2。

#### 迁移影响
- worker key 新增扫描根哈希前缀（跨根同名 dispatch/label 不再共享状态）；此前版本保存的手动分组/隐藏记录需重新操作一次。
- 自定义扫描根写入 ~/.ocsr/board-groups.json 的 roots 字段（旧配置向后兼容）；--work-dir 由替换语义明确为追加。

### 新增 OCSR Board 只读网页面板

#### 变更内容
- 新增 scripts/ocsr_board.py + scripts/board/index.html：127.0.0.1 只读面板，扫描派发目录与 dispatch-log 遥测，展示 worker 状态/时长/session ID；支持 cwd 默认分组、拖动手动分组（~/.ocsr/board-groups.json 持久化）、状态/模型/关键字筛选、返工命令复制与视图层隐藏；不参与派发/看门狗/遥测链路，面板唯一可写文件为 board-groups.json。
- 端口策略：默认 8787；旧面板实例经 /api/identity 识别后 KILL 并同端口重启；外部程序占用顺序换 8788..8797 且不 kill 外部进程；Windows 禁用 SO_REUSEADDR（防止强行绑定已占用端口使探测失效）；探测与正式 bind 之间被抢占时自动重走端口选择。
- 状态诚实性：watcher 显式终态优先于退出码；进程退出未验收/越过 total_deadline 标灰色滞留，不冒充进行中；遥测 error/timeout（含 exit_0_no_artifact，worker-state 无 failed 值域）校正徽章；同 label 串行运行按「启动后最早一条」归属遥测，避免串到别的运行。
- 安全：Host 回环校验防 DNS rebinding；POST 仅收 application/json 并校验类型/长度/控制字符；前端全字段转义；轮询以请求序号防旧响应覆盖分组，指针按下卡片起暂停重渲染防拖动换牌。
- 20 项合成离线测试（tests/test_board.py，动态时间戳/动态端口，无环境耦合）；428 passed（408 基线 + 20 新增），verify 13/13、audit 通过；三端口场景与拖放/分组持久化经本机真实数据 playwright 实测。
- 独立 fresh 复查（deepseek/deepseek-flash）REVISE，12 项发现（状态推导、遥测归属、输入校验、前端竞态、测试环境耦合等）全部修复并复验；报告与冻结 diff 存 .review/ocsr-board-20261107/（gitignored）。调用预算 2/2（1 preflight + 1 派发）。

#### 迁移影响
- 面板为可选辅助工具，不启动即无影响；历史遥测/派发目录不因面板改变。
- 面板 dispatch-log 路径固定 ~/.ocsr/dispatch-log.jsonl（与 dispatcher 对齐，不随 OCSR_HOME 分叉）；--work-dir 对应 dispatcher 自定义工作目录，可重复指定。
- 非 watch 派发成功无遥测可配，卡片显示「已退出·未验收」是证据投影而非异常；真实失败会经遥测校正为红色失败。

### dsh 适配层迁移到 0.2.2-rc.1

#### 变更内容
- 适配目标从 dsh `0.1.1-rc.2` 升到 `0.2.2-rc.1`：包版本 `0.2.0`（`@ocsr/dsh-ocsr`），`engines.dsh ^0.2.2-rc.1`；peerDependencies 收敛为 `@deepseek-ai/cordis ^4.1.0`、`@deepseek-ai/dsh-skill ^0.2.2-rc.1`、`@deepseek-ai/dsh-tools ^0.2.2-rc.1`、`@deepseek-ai/schemastery ^3.19.0`，旧 jobs/jobs-local/tool-jobs/settings/session/session-projection peer 全部移除；dependencies 保留 `yaml ^2.8.1`；exports 收敛为 `.` 与 `./tool`。
- `cordis.patch.yml` 只剩 `ocsr-skill` 与 `ocsr-tools` 两个 entry（`ocsr-settings`、`ocsr-projection` 删除）。
- 模型只收窄门从 settings namespace 改为工具自身 Config：`lib/tool-ocsr.js` 新增 `Config = z.object({ allowedModels: z.array(z.string()).default(undefined) })`，由 `apply(ctx, config)` 读取；新增 `lib/model-gate.js`，按 `OCSR_ALLOWED_MODELS_PATH` > `OCSR_HOME/allowed-models.json` > `~/.ocsr/allowed-models.json` 与 driver 同优先级取基础名单并 fail-closed。语义：省略=完全交给 driver、显式空数组=全关、非空=取交集（只收窄、不放宽）。`.default(undefined)` 是承重的，否则 schemastery 会把省略字段补成 `[]`，让未配置的 profile 静默全关。
- `background:true` 按 0.2.2 jobs 契约重写：`run(job)` 内用 `job.append(...)` 写 registry 输出环（stderr 走 `channel:'stderr'`），返回 `{cancel, done}`，`done` 解析 `{status:'completed'|'killed'|'failed', detail?}`；owner 取 `exec.agent?.id`（SessionId），无 agent 时省略即 unowned。旧 `{cancel, done, readOutput}` 形态替换为 registry 输出环。
- 移除 settings namespace 与 `ocsr.jobs` session 投影两个平面：0.2.2 的 settings 服务改为 SettingsForms，不再有 register/get；projection 契约要求 zod `ZodType`（schemastery 无 `.parse`）且基础 bundle 默认挂载 `session-projection-cache`，OCSR 的内存投影按设计会自禁用。删除 `lib/settings-ocsr.js`、`lib/projection-ocsr.js`。
- 新增适配层回归：`tests/adapter/*.test.mjs`（Node 内置 `node:test`，11 项契约回归，对真实 dsh 0.2.2 运行时验安装版本门、真实 SkillRegistry 的 skill provider、bundle patch entry 与 package exports 对齐、tool 定义与输出 schema、Config 窄门语义、后台输出环与状态映射、`job_kill` 的 killed 映射、jobs 缺失时的 code 5）与 pytest 桥 `tests/test_dsh_adapter.py`（枚举测试文件逐个传给 node；缺 0.2.2 运行时或 Node 时 skip，`OCSR_REQUIRE_ADAPTER_TESTS=1` 可把 skip 变成失败；运行时自动探测，`DSH_RUNTIME_ROOT` 可固定运行时并关闭自动探测）。`package.json` 新增 `scripts.test:adapter`，`files` 改为显式 glob（`lib/*.js`、`SKILL.md`、`scripts/*.py`、`scripts/*.ps1`、`scripts/*.cs`、`refs/*.md`、`cordis.patch.yml`），使 `npm pack` 不再打入 `scripts/__pycache__/*.pyc`。
- 文档同步：`refs/dsh-integration.md`、`docs/CURRENT.md`、`docs/deployment.md` 按 0.2.2 契约重写/校正（preset 名单 `minimal/standard/ptc/cordis`、白名单唯一事实源、双 entry、已移除平面、验证命令）。
- 408 passed（含适配层 pytest 桥）；`verify_ocsr_skill.py` 13/13、`audit.py check`、适配层 Node 套件 11/11 与 `git diff --check` 通过。

#### 迁移影响
- 已装旧版 profile 需重新 `dsh plugin --profile <p> add .`（或升级到 0.2.0）才能拿到双 entry 与 0.2.2 jobs 契约；宿主低于 `^0.2.2-rc.1` 时安装预检会拒绝、加载期 bundle 被 skip，不要用 `allow-version` 豁免绕过。
- 旧 settings 里的 `dsh-ocsr` 白名单配置不再生效；需要收窄时给 `ocsr-tools` 的 `config.allowedModels` 配置（只能写 profile patch；非 volatile 字段不出现在设置表单）。省略该字段即完全由 driver 的 `~/.ocsr/allowed-models.json` 决定。
- 白名单仍只在用户本地 `~/.ocsr/allowed-models.json` 维护，包内没有 `config/`，重装不会带出模型池。
- 依赖 `ocsr.jobs` 投影的外部观察面消失；job 生命周期改从 jobs 服务自身读取，`job_output`/`job_kill` 不受影响。
- 适配层回归需要本机 0.2.2 运行时；无可探测运行时或 Node 的机器上 pytest 桥会 skip，不算失败。

### 插件市场收录素材与 npm 元数据

#### 变更内容
- 新增 `assets/icon-180.png`：由 OpenCode logo（MIT）派生——重新配色并把内部矩形拆成三根竖条表示并行 worker；署名与来源写在 `assets/README.md`，注明与 OpenCode 项目无从属关系。
- 新增 3 张市场截图 `assets/screenshot-{1,2,3}-*.png`（派发链路、模型门语义、`dsh --dump-config` 的两个 entry）与 `screenshots.json`（1–8 张，路径相对该文件）；截图是架构示意与真实命令输出，不是伪造的 GUI 画面。
- `package.json` 补齐 npm 元数据：`description`、`keywords`、`homepage`、`repository`、`bugs`、`author`、`publishConfig.access: public`。`repository` 指回本仓库，供插件市场自动关联 npm 包；`files` 仍不打包 `assets/` 与 `screenshots.json`（市场从 GitHub 读取）。
- 修正 CHANGELOG 中适配层 Node 套件数字（8/8 → 11/11）。

#### 迁移影响
- 无行为变更；安装方式与版本门不受影响。
- npm 元数据已写入 `package.json`（本次未发布到 npm）；`assets/` 与 `screenshots.json` 只留在 GitHub，不打进 npm 包。

### 包名改为 `dsh-ocsr`（为 npm 发布做准备；本次未发布）

#### 变更内容
- 包名从 `@ocsr/dsh-ocsr` 改为无 scope 的 **`dsh-ocsr`**，版本 0.2.1：为将来发布 npm 包消除组织/scope 权限门槛；当前未发布到 npm，安装以 GitHub 源为准（`dsh plugin --profile <p> add github:cenglin123/ocsr`）。
- `cordis.patch.yml` 两个 entry 的 `name` 同步改为 `dsh-ocsr` 与 `dsh-ocsr/tool`；`lib/*.js` 的 `@module` 文档标签、`refs/dsh-integration.md` 的安装/发布说明、`docs/CURRENT.md` 与 `assets/screenshot-3-composition.png` 同步更新。
- npm 元数据（description/keywords/homepage/repository/bugs/author/`publishConfig.access: public`）已就绪，`repository` 指回本仓库；将来发布时插件市场可自动关联包与仓库。

#### 迁移影响
- 以 `link:`/GitHub 源安装过旧包的 profile：bundle 名由 `@ocsr/dsh-ocsr` 变为 `dsh-ocsr`，需 `dsh plugin --profile <p> remove <旧名>` 后重新 `add`；0.2.0 未发布到 npm，不存在 npm 侧破坏性升级。
- 安装命令：当前用 `dsh plugin --profile <p> add github:cenglin123/ocsr`；npm 发布后可用 `add dsh-ocsr`。
- npm 发布暂停：本机 npm 账号注册不可用，发布留待日后处理；不影响 GitHub 源安装与市场收录。

## 2026-11-02

### 退役 AGENTS/CLAUDE/GEMINI 三文件同步体系

#### 变更内容
- AGENTS.md 成为唯一 Agent 入口；删除派生副本 CLAUDE.md、GEMINI.md 与同步脚本 scripts/agent_links.py。
- .githooks/pre-commit 移除三文件一致性检查（含内联 MD5 fallback），保留 python 检测、记忆结构检查与 OCSR 回归检查；治理变更提醒的匹配模式同步收窄。
- scripts/audit.py 移除 _check_sync 及其输出分支；README、SKILL.md、docs/deployment.md、docs/audit-checklist.md、refs/dsh-integration.md 的验证清单同步去除 agent_links 项。
- 3 位跨 family fresh reviewer（deepseek/deepseek-flash、xiaomi/mimo-v2.6-pro、zhipuai-coding-plan/glm-5.3-flash）独立复查一致判 REVISE：audit.py docstring 的 sync health 残留、_STATUS_GLYPHS 死标签、CURRENT.md 悬空指针，均已在本次变更内修复；报告存 .review/retire-agent-links-20261102/（gitignored）。
- 407 passed；verify_ocsr_skill 13/13、audit、pre-commit 冒烟、diff/eol 检查通过。

#### 迁移影响
- 修改协作规则只改 AGENTS.md，无同步步骤；宿主入口按各自约定读取 AGENTS.md。
- docs/CURRENT.md、docs/CHANGELOG.md 与 active plans 中的 agent_links 记录为历史验收口径，不再代表当前验证清单。

---

## 2026-11-01

### UTF-8 输出与超时诊断

#### 变更内容
- launcher 明确原生 stdout 的 UTF-8 解码，stderr 独立落盘；进程失败与 preflight 同时读取两类日志，保留真实退出码与唯一 session 绑定。
- JSONL 按 LF 分帧，显式报告坏 JSON、非法 UTF-8、非事件行与未完成尾行，不以替换字符吞掉损坏；Unicode 行分隔符不再切断字符串内事件。
- 新增只读 `diagnose --worker-dir` 与可选 `--session-db`；watcher 可显式传 `--diagnostic-session-db`。只查询已验证绑定的精确会话、本次启动后的 assistant 元数据，不输出推理或工具参数正文。数据库异常、未知结构明确降级，阶段名称区分消息步骤结束与产物验收。
- 到期终止/交回生成 `diagnostic.json` 并内嵌到 state/ledger，记录策略依据、最后观测来源与时效、日志质量、未知根因；侧车写盘失败显式记账，不宣称文件存在。诊断不参与续期、预算、恢复或完成裁决。
- 407 项离线回归通过，包括 CP936 下原生 UTF-8 正反对照、会话绑定、stderr/preflight、元数据隔离、阶段一致性与诊断写入失败降级。真实短调用 8 条事件无解析损坏；历史 MiMo 证据保持不变并能回查 write 参数生成状态。

#### 迁移影响
- 输出编码和 stderr 分离只作用于新生成的 launcher，旧日志不自动重编码。
- 数据库诊断默认关闭，仅适配已核对的 OpenCode v2 schema，使用 Python 标准库 sqlite3；查询失败不改变派发决策。遥测 schema 与 `stall` 分类保持兼容，超时不代表已证明服务端停滞。

## 2026-10-31

### Windows launcher 退出时清理所属进程树

#### 变更内容
- launcher 在启动 OpenCode 前创建并加入 `KILL_ON_JOB_CLOSE` Job，唯一非继承句柄由 launcher 持有，正常或异常退出均清理 Job 内后代；dispatcher 退出仍保留脱管 worker。
- 新增 `scripts/ocsr_launcher_job.cs`，生成 launcher 时嵌入源码，不依赖运行期间的技能目录。初始化/绑定失败沿既有 `error.log` 与 `exit=1` marker 契约阻止模型调用。
- 新增 6 项 Windows 离线回归，覆盖正常/异常退出、多层后代、兄弟进程隔离、脱管，以及 PowerShell/原生配置/绑定失败；完整回归 347 passed。真实 OpenCode/MCP 正常与异常退出无追踪进程残留，独立 fresh 复查 ACCEPT。

#### 迁移影响
- 只对新生成的 launcher 生效，不追溯清理旧 worker 或已有共享服务。
- worker 内启动的后台进程也随 launcher 结束；需要长期运行的服务须由独立宿主管理。受限宿主拒绝 Job 绑定时会明确失败，不退回无生命周期约束的启动方式。

## 2026-10-24

### ledger 增加 target_round 透传

#### 变更内容
- `dispatch --meta target_round=N` 识别 converge 主循环轮次，仅接受正整数；非法值在派发前报错并返回 1。
- 合法值以 int 写入 ledger 的 `launched` 与 `landed` 行，供下游轮次统计；`refs/converge-integration.md` 补充用法示例。

#### 迁移影响
- 无配置变更。未传 `target_round` 时账本行不新增字段，既有行为不变。

## 2026-10-18

### 续接前校验原工作目录（cwd）

#### 变更内容
- launcher 把 worker 的工作目录（dispatch 进程当前目录，绝对路径）写入 `session-binding.json` 的 `cwd`；接续派发时若原 `cwd` 可核对，新绑定沿用原目录（会话仍绑定在那里），否则记录当前 dispatch 目录。
- `--resume-from` 在启动副作用前校验原 `cwd`：目录不存在则拒绝，并提示在原路径重建（如 `git worktree add <路径>`）或新开 worker；旧派发无 `cwd` 记录时只提示「无法核对原工作目录，若已删除接续会失败」；当前 cwd 与原 `cwd` 不同时允许，但提示「opencode 会在原工作目录 <路径> 打开会话；请在返工提示词里写明要改的目录」。
- 文档同步：`refs/dispatch-patterns.md`「接续原会话（返工）」与 `docs/pitfalls.md`「opencode 会话绑定原工作目录」。

#### 迁移影响
- 旧派发目录没有 `cwd` 记录，续接不阻止但会提示；无需迁移。

### 返工可接续原会话（dispatch --resume-from）

#### 变更内容
- `dispatch` 新增 `--resume-from <原 worker 派发目录>`：worker 正常完成后按验收清单返工时，可接续原会话。只允许单个 worker；原派发模型须与本次一致（从原目录 `launcher.ps1` 核对，无法核对即拒绝）；`session-binding.json` 须有合法唯一 session ID；旧进程须确认已退出（PID 复用视为已退出，进程表不可读或身份不一致则拒绝），且进程表中不得有仍以该 PID 为父的存活后代（opencode 残留）。
- launcher 以 `opencode run <prompt> --format json --standalone --session <id> -m <model>` 启动，照常走看门狗、产物验收、ledger 与 telemetry；账本与遥测记录 `resumed_from`、`resume_session`、`resume_mode` 与 `rework_index`，账本另记 `attempt_index`，`worker-state.json` 同样记录模式与两种计数。
- `--resume-from` 按原目录状态自动区分两种模式：`worker-state.json` 为 `landed`，或无 `worker-state.json`/仅启动态且 `start.marker` 为 `exit=0` → **返工**，沿用原 `attempt_index`、`rework_index` + 1，上限 3 轮；非 0 退出、无 exit 记录或 `timed_out`/`stopped`/`failed` 等 → **故障恢复**，`attempt_index` + 1，三次总上限。返工模式拒绝 `--attempt-index`，恢复模式拒绝 `--rework-index`；原目录缺 `rework_index` 可用 `--rework-index` 显式指定（须大于原值且 ≤3）。
- 非 `--watch` 派发也会在启动时把模式与计数写入 `worker-state.json`，使失败重试与返工上限在非 watch 路径同样有据可依。
- 新会话仍写新的 `session-binding.json`；返回的 sessionID 与原 id 不同时照实记录并提示（回读与提示只在 `--watch` 路径执行）。
- 文档同步：`docs/overview.md` 第 2 条、`refs/dispatch-patterns.md`「接续原会话（返工）」、`SKILL.md` 入口指针、`docs/deployment.md` 升级复验清单。

#### 迁移影响
- 无配置变更。默认派发行为不变；仅在显式传 `--resume-from` 时启用续接。

### worker 改用私有服务器（opencode run --standalone）

#### 变更内容
- launcher 与 resume_argv 加 --standalone：默认连接的常驻后台服务为每个工作目录常驻 MCP 子进程且不随 run 释放，派发目录（尤其 git worktree）无法删除、进程逐次累积；首个 run 还会自行拉起常驻服务。测试断言 launcher 与续接参数含 --standalone。

#### 迁移影响
- 无配置变更；已残留的 MCP 进程需 opencode service restart 释放。

---

## 2026-09-22

- **模型白名单本地化**：`config/allowed-models.json` 排除 git（`.gitignore` + 解除跟踪），每台机器各自维护。文件缺失/为空 = 未配置：首次使用前询问用户启用哪些模型（`opencode models --verbose` 展示 → 征求 → 写入）；非空直接使用。非法格式仍 fail-closed。加载器支持 `OCSR_ALLOWED_MODELS_PATH` 覆盖配置路径（测试/多套配置）。npm 包不再携带 `config/`。
- 测试与用户配置解耦：全部改用合成白名单或临时配置注入（含子进程 CLI 测试），无 `config/allowed-models.json` 时也能全绿。
- 修复 dsh 工具面致命 bug：`lib/tool-ocsr.js` 曾用 `node` 执行 Python 驱动（`spawn(process.execPath, ...)`），前台与后台路径均不可用；改为 Python 解释器（`OCSR_PYTHON` 可覆盖）。
- 工具层错误码与 driver 退出码契约脱钩：模型门拒绝 = `4`、基础设施失败 = `5`（原先借用 `2`/`3` 会与「确定性失败/路径碰撞」混淆）；driver 退出码 `0/1/2/3` 透传不变。
- `agent_links.py repair` 覆盖派生文件不再要求 `--force`（与「只编辑 AGENTS.md」工作流一致；`--force` 保留为兼容 no-op）。
- 清理：删除已完成的 5 份 active 计划（历史在 Git）；修复 `20260810-deterministic-run-spec.md` 死链引用；`refs/dsh-integration.md` 真实用户路径改 `<user-home>` 占位；刷新陈旧测试基线数字；`audit.py` birth-record 去掉悬空候选；`projection-ocsr.js` schema 与 fold 对齐（可选字段）；tool schema 数值字段改 integer。

## 2026-09-11

- 默认白名单跟随 DeepSeek 更新：`deepseek/deepseek-v4-flash` → `deepseek/deepseek-flash`（V4.1 Flash，官方公告称旧 V4 Flash 已下线、仅暂时路由；2026-09-14 起 `deepseek-v4-pro` 也将被路由至该模型）。已按规则以 `opencode models --verbose` 核对本地池并 preflight 探测通道可用。
- 看门狗超时与 Session 恢复落地（计划 20260911-ocsr-watchdog-session-recovery，已执行）：检查期限与总期限分离（总期限 `timeout × (max_renewals+1)` 写入 `worker-state.json` 后不可变，续期默认 1 次）、worker 单向状态机、`artifact_seen` 与 `landed` 分离；launcher 以 `--format json` 流式解析顶层唯一 `sessionID` 写 `session-binding.json`（缺失/歧义只禁用续接，不改真实退出码）；恢复仅由上层以新 reserve/settle 显式发起，驱动器只产 `resume-material.json`（含 `old_process_stop_verified`、`requires_settle`）；并发 DB 锁从「延迟 30s 自动重试」改为 `recovery_required` 停止交回上层。新增 7 个遥测可选字段，`refs/dispatch-patterns.md` 同步模板与「检查期限、续期与总期限」「session 绑定与恢复材料」两节。
- allowlist 测试改为从 `ALLOWED_MODELS` 加载结果派生，不再在测试内复制模型清单（此前与 #7 合并的 4 模型配置矛盾致 3 例长期红）；`tests/test_audit.py` 临时目录先 `resolve()`，修复 Windows 8.3 短路径下 `relative_to` 假阳性（结构链接测试在本机长期误红）。

## 2026-09-06

- audit 工具词边界修复：drift 检查的子串匹配会把 "windows"（含 "ws"）误判为"文档提到 WebSocket"，自 #3 引入 package.json（manifest 出现）激活该检查起持续误报。改为 `\b` 词边界正则（`DRIFT_KEYWORD_RES` + `_mentions`），tests/test_audit.py 补 2 个回归用例；pisr 仓库同步修复。
- 模型选择规则收紧：默认白名单标注为作者本机模型池样例；每台机器首次使用前（及换机、通道异常归因后）先 `opencode models --verbose` 按本地池重配 `config/allowed-models.json` 再决定模型（SKILL.md、refs/model-defaults.md；与 pisr 同逻辑）。
- fresh 对抗评审补运行时验证合同（refs/failure-modes.md §2、SKILL.md 最小规范）：reviewer 以只读命令佐证时 prompt 须钉死禁止写入与修复（生成/评估分离），报告列明执行的命令与退出码；OCSR 无进程级工具白名单，命令只读性属事后审计面，需进程级硬约束的评审走 PISR。

## 2026-08-18

- 仓库重建为 public，历史起点。此前私有开发历史未随仓分发（脱敏摘要见 `DEVELOPMENT.md`）。
- watcher 失败语义分层：`_detect_name_mismatch` 区分「0 产物」与「产物命名与 pattern 不符」。
- 模型池移除 `deepseek/deepseek-v4-pro`（涨价），池收敛为 flash / mimo-v2.5 / mimo-v2.5-pro。
