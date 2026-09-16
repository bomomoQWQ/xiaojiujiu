# framework/ 与 scripts/ 是否整合：工程评估

把 `HANDOFF.md` §7.1「framework/ 与 scripts/ 的区别」与 backlog 第 2 项（`HANDOFF.md:201-202`）
提出的问题，落成一份可核对的答案：**要合并的到底是什么、合并会得到什么、会失去什么。**

**本文只记录从代码与既有产物里读出来的事实。读过的行号都标了出来；没能核实的项集中列在 §7，
不做"看起来差不多"的推断。**

| 项目 | 值 |
|---|---|
| 被评估对象 | `scripts/` 四个仿真 + `framework/`（`cf/` 与 `tests/`） |
| 评估方式 | 只读源码与配置；不运行仿真（需要 uvicorn/网络，耗时数分钟） |
| 断言数量来源 | `bb-run/report.json`、`res-run/report.json`（**既有产物，非本次运行**，生成于 2026-09-16T02:34Z）；关系递进取提交 `17f5ef3` 的提交信息；框架取本次 `pytest --collect-only` |
| 未运行的部分 | 四个 `scripts/*_simulation.py` 本体、`framework/tests` 的用例体（只收集不执行） |

---

## 0. 结论先行

**推荐 (b)：不合并两个套件，只把已经重复的"机械件"抽到一个共享模块里。**

主因有三条，都可在代码里核对：

1. **两者验证的不是同一件事，合并会减少覆盖而不是增加。** `scripts/` 是"固定剧本的发布门禁"
   （黑盒 13 阶段 77 项、韧性 18 阶段 335 项、关系递进 8 阶段 105 项），跑一次给一个是/否
   （`HANDOFF.md:287-288`）；`framework/` 是"可交互实验台"——起 harness，运行中拨时间、灌输入、
   看变量、给假端点注错（`framework/README.md:47-48`、`framework/README.md:110-138`）。
   把实验台并进发布门禁，等于让"这次改动属于被测物还是测试台"（`framework/README.md:256-257`）
   这个原本可回答的问题消失。
2. **两边各有对方结构性做不到的事**：`scripts/` 能用真实 Scheduler 的 gate 决定"它自己想开口"
   （`scripts/blackbox_user_simulation.py:1760-1814`），能在适配器传输层注入授权故障
   （`scripts/e2e_resilience_simulation.py:1209-1229`）；`framework/` 能把时间 `scale`/`freeze`、
   用真 `remote_api` mock 端点喂强语义（`framework/cf/mock_openai.py:366-424`）、接真主 LLM
   （`framework/cf/main_llm.py:133`）。这些能力合不进同一套驱动。
3. **但重复是真的、而且已经在漂移。** `free_port` 在三个脚本与框架里各有一份
   （`scripts/blackbox_user_simulation.py:562-566`、`scripts/relationship_progression_simulation.py:881-885`、
   `scripts/e2e_resilience_simulation.py:463-469`、`framework/cf/harness.py:843-849`）；
   插件配置字典是**照抄**的，框架源码里自己写着这行注释
   （`framework/cf/host.py:59-62`）；"源码未被改动"的核对，框架版只盯 `*.py`
   （`framework/cf/harness.py:862`），比黑盒版弱（`scripts/blackbox_user_simulation.py:3308` 盯
   `.py/.json/.jsonl/.yaml/.yml/.toml`，且把 `.pyc` 与"别的进程改的文件"分开处理，
   `scripts/blackbox_user_simulation.py:3296-3343`）。

**(c) 完全合并应当拒绝。** 没有任何一个具体行为需要靠"合并套件"才能被验证——重叠的部分两边
都已经在测，只是测法不同（见 §3），而不重叠的部分合并后反而会因为驱动方式不同而互相污染。

**(a) 完全不共享也站得住**，因为 §7.1 明确要求两者"互不依赖"，黑盒脚本也刻意重写而非 import
韧性脚本的管线（`scripts/blackbox_user_simulation.py:75-81`）。所以本文的 (b) 是**有限度的**：
只抽"没有行为语义的机械件"，不抽阶段驱动、断言器、假宿主。若维护者认为连这也破坏了独立性，
退到 (a) 是可接受的选择，代价见 §5 的成本栏。

---

## 1. 两个套件的对比表

### 1.1 汇总（题目要求的六列）

| | `framework/` | `scripts/` |
|---|---|---|
| **覆盖面** | 通用实验台：时间控制、强语义 mock、变量日志、真插件宿主、主 LLM 客户端、客户端配置/人格、OneBot 前端、TUI；271 个测试 | 四个固定剧本：黑盒用户旅程、并发/租约/重启韧性、关系递进两个月、记忆契约 |
| **到达 Runtime 的方式** | **进程内 import** Runtime（`framework/cf/harness.py:295-298`）+ 真 uvicorn（`:441-499`）+ 公开 HTTP；插件走真钩子（`framework/cf/host.py:299-393`） | 混合：黑盒/关系/韧性 = 真 uvicorn + HTTP；**记忆脚本 = `fastapi.testclient.TestClient`（无真服务器）**（`scripts/e2e_memory_simulation.py:195-199`） |
| **时间控制** | `ControllableClock` 支持 `set/advance/scale/freeze`，运行中经控制面 HTTP 驱动（`framework/cf/clock.py:185-359`、`framework/cf/control.py:50-178`） | `SimClock` 只有 `advance`（`scripts/blackbox_user_simulation.py:596-624`、`scripts/relationship_progression_simulation.py:997-1022`）；记忆脚本直接给 Runtime 传时间戳（`scripts/e2e_memory_simulation.py:223-237`） |
| **能否注入故障** | 能，但只在**强语义端点**：`MockScript`/`MockReply` 可 500、超时、坏 JSON、断连（`framework/cf/mock_openai.py:295-363`） | 能，且层面更多：harness 侧篡改投递文本/会话（`scripts/blackbox_user_simulation.py:1005-1056`）、适配器传输层授权失败（`scripts/e2e_resilience_simulation.py:1209-1229`）、真实租约过期（`:2943-3039`）、150+ 队列深度（`:2851-2934`）、并发上报竞态（`:3491-3605`） |
| **断言数量** | **271**（`pytest --collect-only`，本次核实；`framework/README.md:417` 写 227、`:451` 写 102、`HANDOFF.md:292` 写 102——三处均已过期） | 黑盒 **77**、韧性 **335**（两份 report.json）；关系递进 **105**（提交 `17f5ef3`）；记忆脚本 23 个静态 `V.check(` 调用点，**运行时总数未核实**（§7） |
| **独有能力** | 运行中拨时间；真 OpenAI 兼容 mock 端到端喂 `remote_api`；接真主 LLM/流式；变量 JSONL 轨迹；TUI 与 `cf` 子进程命令；人格价值观轴 | 真实 Scheduler 自主决定开口；真插件全链路 + 多会话隔离 + 重启恰好一次；适配器契约的逐字段核对；两个月关系递进 + 后台变量/事件日志 dump；记忆三类语义（工作集/线索召回/注入块）分层 |

### 1.2 逐产物（更细，避免"scripts/"被当成一个整体）

| 产物 | 真 uvicorn | 真 Scheduler | 真插件 | 强语义 provider | 断言数 | 独有覆盖 |
|---|---|---|---|---|---|---|
| `scripts/blackbox_user_simulation.py` | 是（`:1187-1235`） | 是（`:1223-1230`，脚本不调 `/endogenous`，`:26`） | 是（`:1480-1544`） | disabled（`:1319`） | 77（13 阶段） | 12 阶段用户旅程、多会话隔离、重启/重放恰好一次、记忆的用户可见面 |
| `scripts/e2e_resilience_simulation.py` | 是（`:701-761`） | 可选（`:730-751`）+ 显式 `/endogenous`（`:872-875`） | **否**，直接 import 适配器内核（`:168-194`） | 只验证选择/拒绝（`:3200-3292`） | 335（18 阶段） | 适配器契约逐项、授权故障、租约生命周期、队列 >150、并发上报 |
| `scripts/relationship_progression_simulation.py` | 是（`:1536-1632`） | 是（`:1572-1578`，`:28`） | 是（`:1781-1850`） | disabled（`:1660`） | 105（8 阶段） | 两个月关系递进、亲密/共同历史/追问扫描、后台 dump 与 `inspection.md`（`:3811-3905`） |
| `scripts/e2e_memory_simulation.py` | **否**（`TestClient`，`:195-199`） | 否（直接调 `endogenous_round`，`:229-237`） | 否 | disabled（`:211-212`） | 23 静态调用点（运行时未核实） | 记忆契约：准入/类型/修正/连续/召回/新鲜度/遗忘/来源溯源（`:340-635`） |
| `framework/` | 是（`framework/cf/harness.py:441-499`） | 是（`:501-517`），但测试里多为手动强制（`framework/tests/test_host.py:242-261`） | 是（`framework/cf/host.py:299-393`） | **真 HTTP mock**（`framework/cf/mock_openai.py:366-424`） | 271 | 见 §1.1 |

---

## 2. 重叠清单（同一行为被两边检查的地方）

每条都给出两侧的 `file:line`，并说明哪一侧更强、强在哪。**"更强"按证据的不可伪造性排序，
不按断言数量。**

### 2.1 真 uvicorn + 文件 SQLite（WAL）在进程内启动

| 侧 | 证据 |
|---|---|
| 黑盒 | `scripts/blackbox_user_simulation.py:1187-1235`（import uvicorn、`Runtime`、`create_app`、`Scheduler`）；WAL 在 `:1313-1316` |
| 韧性 | `scripts/e2e_resilience_simulation.py:701-761`；库路径 `:653` |
| 关系 | `scripts/relationship_progression_simulation.py:1536-1632` |
| 框架 | `framework/cf/harness.py:441-499`（uvicorn）+ `:349-386`（`load_config`/`resolve_paths`/`Runtime`） |

**哪边更强：平手，但用途不同。** 四者都是同一套真实部署形状。差别在"谁来写这段 wiring"：
三个脚本各自独立重写（黑盒在 `:75-81` 明确说是为了不被韧性脚本的假设绑架），框架又抄了一份。
这属于 §5 的"可共享"候选，而不属于"重复验证"。

### 2.2 进程内时钟重绑（`utcnow` / `utc_now_iso`）

| 侧 | 证据 |
|---|---|
| 黑盒 | `scripts/blackbox_user_simulation.py:627-659` |
| 关系 | `scripts/relationship_progression_simulation.py:1024-1056`（与黑盒逐行同构） |
| 框架 | `framework/cf/clock.py:362-404` |

**哪边更强：框架更强。** 框架版把前缀与属性做成参数（`framework/cf/clock.py:362-367`），
可重复调用并返回重绑计数（`:377-380`、`:402-404`），而且 `beat()` 每一拍都**重新执行一次**
（`framework/cf/harness.py:602`），覆盖"启动之后才 import 的模块会拿到真 `utcnow`"这个真实漏洞。
两个脚本版只在启动时调一次，且强制 `runtime_utility.utcnow = clock.now`
（`scripts/blackbox_user_simulation.py:657-658`）——黑盒在插件 import 后**再调一次**
（`:1500`）来补同一个洞，属于手工补救。这是"共享可以顺带修掉的已知差异"。

### 2.3 真插件加载 + AstrBot 三个钩子

| 侧 | 证据 |
|---|---|
| 黑盒 | `scripts/blackbox_user_simulation.py:1480-1544`（stubs 注入 sys.path `:1488-1490`、`import_module("...main")` `:1495`、`initialize()` `:1541`、钩子按名取 `:1542-1543`） |
| 关系 | `scripts/relationship_progression_simulation.py:1815-1850` |
| 框架 | `framework/cf/host.py:345-393`（同一顺序：stubs `:356-359`、import `:365-366`、`initialize` `:380`、钩子 `:381-382`） |
| 韧性 | **不做**这一步；它直接 import 适配器内核（`scripts/e2e_resilience_simulation.py:168-194`），并自己镜像 `AstrBotActionExecutor`（`:1100-1133`） |

**哪边更强：黑盒/关系/框架证明的是"插件真的被装起来了"，韧性证明的是"适配器契约逐字段正确"。**
韧性更强的地方是它能看到 lease 身份与强制拒绝（`:1185-1229`、`:1342-1358`），
这是真插件路径上拿不到的；黑盒/框架更强的地方是它们证明真钩子的注册与临时注入契约成立
（框架 `framework/tests/test_host.py:197-201`、`:283-292`；黑盒 `:1597-1600` 断言
`mark_as_temp`）。**两者不可互相替代。**

### 2.4 假平台（有地址簿、未注册会话投递失败）

| 侧 | 证据 |
|---|---|
| 黑盒 | `scripts/blackbox_user_simulation.py:763-806` |
| 关系 | `scripts/relationship_progression_simulation.py:1149-1177` |
| 韧性 | `scripts/e2e_resilience_simulation.py:1017-1043` |
| 框架 | `framework/cf/host.py:99-144`（未注册会话返回 `False`，`:129-144`） |

**哪边更强：平手。** 都是"未注册会话投递失败"这一条语义（韧性版见 `:1017-1043`）。
框架版多一个 `on_deliver` 回调给 TUI 实时打印（`framework/cf/host.py:142-143`），
但这属于呈现，不是验证强度。

### 2.5 运行插件用的配置字典

| 侧 | 证据 |
|---|---|
| 黑盒 | `scripts/blackbox_user_simulation.py:1517-1539` |
| 框架 | `framework/cf/host.py:59-82`，源码注释：`Copied from the shipped black-box simulation so both drive the plugin identically` |

**哪边更强：都不"强"，这是纯复制。** 框架版把 `context_timeout_ms` 2000 vs 500、
`context_prefetch` False vs True、`request_timeout_ms` 5000 vs 2000、`render_timeout_ms` 60000 vs 10000、
`queue_max_backoff_ms` 1000 vs 500 都改了值（对照 `:1517-1539` 与 `framework/cf/host.py:63-82`），
所以它已经**不是**"identically"。注释与实际不符，属于要共享时顺带修掉的偏差。

### 2.6 确定性宿主 LLM

| 侧 | 证据 |
|---|---|
| 黑盒 | `HostLLM` `:883-918`；`proactive_text_for` `:807-854`；`reply_text_for` `:855-873` |
| 关系 | `HostLLM` `:1231-1253`；`proactive_text_for` `:1178-1209`；`reply_text_for` `:1210-1221` |
| 框架 | `ScriptedMainLLM`（`framework/cf/main_llm.py:476`）、`_render_from_prompt`（`:569`）；真端点走 `OpenAICompatibleMainLLM`（`:133`） |

**哪边更强：框架更强，因为它两条路都有。** 黑盒/关系的替身只能证明"Runtime 递给主 LLM 的
话题是什么"（黑盒 `:60-67` 自述这条边界）；框架还能用**真主 LLM**（`framework/cf/main_llm.py:133`）
跑同一路径，并保留确定性替身做无端点回归。

### 2.7 "源码未被改动" 的核对

| 侧 | 证据 |
|---|---|
| 黑盒 | `_tree_files` `:3364-3393`；`Context.repo_sources_changed` `:3296-3314`（盯 6 种扩展名）；`.pyc` 单列一条检查 `:3168-3172` 与一条 note `:3316-3328`；仓库其他变动只作 note `:3330-3343` |
| 框架 | `_snapshot_sources` `framework/cf/harness.py:852-868`（只 `rglob("*.py")`）；`_compare_sources` `:871-884`；断言见 `framework/tests/test_harness_e2e.py:80-108` |

**哪边更强：黑盒明显更强。** 框架版只覆盖 `*.py`，且把 created/changed/deleted 合成一个
`untouched` 布尔（`:883`）；黑盒版覆盖配置/JSONL/YAML/TOML，并把"这次运行不可能产生的变动"
（`.pyc`、仓库其他文件）降级成 note 而不是假红——这正是 `HANDOFF.md:247-253` 记录的那次
"验证工具自己一条假红"教训的产物。**这是"重复代码已经在漂移"的最硬证据。**

### 2.8 事件幂等 / 重复投递

| 侧 | 证据 |
|---|---|
| 韧性 | `post_v1_event` `:833-845`；幂等与重放改写检查在阶段 2（`:1530-1713`，report.json 该阶段 19 项）；重复 report 收敛在阶段 6（report.json 7 项）与阶段 17 并发（15 项） |
| 框架 | `ProgramClient.say(event_id=...)`（`framework/cf/program.py:92-123`）；`test_say_with_a_fixed_event_id_is_idempotent`（`framework/tests/test_cli.py:312`） |
| 黑盒 | 阶段 10 replay（report.json 7 项），`:2666` 起 |

**哪边更强：韧性更强。** 它测到"重复报告**改了内容**也不会改写历史"以及并发上报只应用一次
（`:3491-3605`）；框架只测同一 event_id 第二次返回 `duplicate: true`。

### 2.9 主动消息的 lease → render → authorize → send → report

| 侧 | 证据 |
|---|---|
| 韧性 | 阶段 5 适配器契约（report.json 38 项），`AdapterHarness` `:1230-1358`，lease id 过期 `:1328-1340`，授权故障 `:1209-1229` |
| 黑盒 | 真插件 outbox 全链路，只按用户可见事实断言（`:4-17` 声明证据边界）；`:1724-1756` 等队列落定 |
| 框架 | `framework/tests/test_host.py:263-274`（消息真的到达平台）、`:276-282`（render 用的是 Runtime 的 prompt） |

**哪边更强：各强一半，见 §2.3。** 韧性可以断言 lease_id 身份与"无答复 != 拒绝"；
黑盒/框架只能断言"最终送达了、用的是 Runtime 的 prompt"。

### 2.10 "它自己决定开口" 与未结之事

| 侧 | 证据 |
|---|---|
| 黑盒 | `Story.step` `:1760-1814`：推进时钟后**等真实 Scheduler 的 rounds 增加**，gate 关闭才退回 `/tick` 且明说"不决策" |
| 关系 | 同构（`:1572-1578` 把 `endogenous_round` 交给 Scheduler；`:28` 声明不调 `/endogenous`） |
| 框架 | 真 Scheduler 也接了（`framework/cf/harness.py:501-517`），但 `test_the_runtime_decides_to_speak_on_its_own` **经控制面强制** `endogenous(force=True)`（`framework/tests/test_host.py:242-261`）；README 自述后台心跳会吃掉危险率窗口（`framework/README.md:463-466`） |
| 框架 | 未结之事：`test_a_dated_promise_becomes_an_unfinished_matter`（`framework/tests/test_host.py:229-240`） |
| 黑盒 | `phase_timed_matter` `:2098-2161`（report.json 5 项） |
| 关系 | acquaintance 阶段（`:2786` 起），report 5 项量级 |

**哪边更强：黑盒/关系更强，且是结构性的。** 它们证明的是"**没有人在驱动它**的时候它自己开口"；
框架那条测试的名字虽然写着 `on_its_own`，实际是控制面强制一轮（`:256`），
证明的是"强制一轮会得到 `hazard_triggered`"。这不是同一件事。

### 2.11 `remote_api` provider 的选择与降级

| 侧 | 证据 |
|---|---|
| 韧性 | `_LoopbackProbe` `:3149-3197` + `phase_provider_config` `:3200-3292`：证明"无 key 时 provider 自报不可用、**一个请求都不发**"（`:3281-3290`） |
| 框架 | `MockOpenAIServer` `framework/cf/mock_openai.py:366-424` + 接地回复 `:146`；`test_deep_refresh_goes_through_the_mock_and_applies`（`framework/tests/test_harness_e2e.py:121-137`）：证明 mock 被调用、suggestions 通过 grounding、至少一条操作落到状态 |
| 黑盒/关系 | provider 直接 disabled（`:1319`、`:1660`），**不覆盖真 remote_api 的 HTTP 契约** |

**哪边更强：两边互补，不能互相替代。** 韧性证明**拒绝路径**，框架证明**成功路径**。
`framework/README.md:213-214` 自述的 fail-open（连不上/超时/坏 JSON 一律降级）由框架的
`TestFaultInjection`（`framework/tests/test_harness_e2e.py:264-317`）覆盖。

### 2.12 明确**没有**重叠的行为

以下只在单侧出现，不属于重复，列出来是为了说明"合并后不会得到新的双保险"：

* 记忆契约八阶段（准入/类型/修正/连续/召回/新鲜度/遗忘/来源）：只在
  `scripts/e2e_memory_simulation.py:340-635`。
* 多会话隔离与串话：黑盒阶段 8（report.json 7 项），`scripts/blackbox_user_simulation.py:2411` 起。
* 关系递进的亲密/共同历史/追问扫描：`scripts/relationship_progression_simulation.py:2497-2562`。
* 后台变量 dump 与 `inspection.md`：`scripts/relationship_progression_simulation.py:3811-3905`。
* 运行中拨时间（`scale`/`freeze`）与子进程 `cf` 命令：`framework/cf/control.py:50-178`、
  `framework/tests/test_cli.py:169-232`。
* OneBot 11 前端：`framework/cf/onebot.py`、`framework/tests/test_onebot.py`（16 项）。
* `dead_code_inventory.py`（`:1-25`）与 `mutation_design_conformance.py`（`:1-30`）是源码分析/
  变异工具，两侧都没有对应物。

---

## 3. 结构性不可替代的能力

### 3.1 只有 `scripts/` 能做

1. **让真实 Scheduler 自己决定开口**，且不靠 `/endogenous` 强制。
   证据：`scripts/blackbox_user_simulation.py:1760-1814`（等 `scheduler.status()["rounds"]` 增加；
   gate 关闭时明确标注"不决策"），`:26` 与 `scripts/relationship_progression_simulation.py:28`
   都声明不调 `/endogenous`。框架做不到的原因写在 `framework/README.md:463-466`：
   每一拍 `lazy_tick` 会消费掉积分时间，手动步进 + 后台心跳会让 `delta_t≈0`。
2. **在适配器传输层注入故障并直接核对契约字段。**
   证据：`scripts/e2e_resilience_simulation.py:1185-1229`（记录 lease、只让 authorize 失败）、
   `:1342-1358`（显式构造 lease 请求）、`:2943-3039`（真实等租约过期）、
   `:2851-2934`（150+ 队列深度）、`:3491-3605`（并发上报）。框架只有强语义端点的注错
   （`framework/cf/mock_openai.py:295-363`）。
3. **用固定时间戳、完全可复现地跑记忆语义的八阶段契约。**
   证据：`scripts/e2e_memory_simulation.py:223-237`（时间戳显式传入，不依赖墙上时钟）、
   `:340-635`。
4. **把后台变量/事件日志 dump 成可人工审计的产物。**
   证据：`scripts/relationship_progression_simulation.py:3811-3905`（`backend/*.json` + `inspection.md`）。

### 3.2 只有 `framework/` 能做

1. **运行中从外部拨时间**：`set` / `advance` / `scale` / `freeze` / `unfreeze`。
   证据：`framework/cf/clock.py:251-322`、`framework/cf/control.py:50-178`、
   子进程驱动 `framework/tests/test_cli.py:169-232`。脚本的 `SimClock` 只有 `advance`
   （`scripts/blackbox_user_simulation.py:596-624`），且只能在脚本进程内调用。
2. **端到端喂真 `remote_api` 强语义**（接地回复 + grounding + fail-open 降级）。
   证据：`framework/cf/mock_openai.py:146`、`:366-424`；
   `framework/tests/test_harness_e2e.py:121-137`、`:264-317`。
3. **接真主 LLM（OpenAI 兼容，含 SSE 流式）并区分 reply/render 两条路。**
   证据：`framework/cf/main_llm.py:133`、`:584`；`framework/tests/test_main_llm.py`（32 项）。
4. **人格价值观轴只在创建运行时那一行写入，并把生效值记进 trace。**
   证据：`framework/cf/harness.py:388-439`、`framework/tests/test_host.py:449`。
5. **变量轨迹 JSONL + 心跳记录 + 原程序日志桥接。**
   证据：`framework/cf/harness.py:591-630`、`framework/cf/logbook.py`、
   `framework/README.md:365-408`。
6. **控制面只绑回环且拒绝非回环地址。**
   证据：`framework/cf/control.py:71-77`。

---

## 4. 可共享但不必合并的部分

下面每一项都满足"**有明确重复、且抽取不改变任何一侧的驱动方式**"。建议放在一个**中立位置**
（例如 `scripts/_sim_common.py` 或一个独立的 `simcommon/` 包），让两侧都依赖它，
**而不是让 `scripts/` 依赖 `framework/` 或反过来**——后者正是 §7.1 与
`scripts/blackbox_user_simulation.py:75-81` 想避免的。

| 候选 | 重复位置 | 抽取成本 | 收益 | 风险 |
|---|---|---|---|---|
| `free_port()` | 黑盒 `:562-566`、关系 `:881-885`、韧性 `:463-469`、框架 `framework/cf/harness.py:843-849` | 极低（4 处各 5 行） | 消掉 4 份逐字相同的实现 | 几乎为零 |
| `install_process_clock` + `SimClock` | 黑盒 `:596-659`、关系 `:997-1056`（逐行同构）、框架 `framework/cf/clock.py:185-404` | 中：需统一"只跳步"与"可 scale/freeze"两种接口 | 关系脚本可直接用黑盒的实现；框架可把"每拍重绑"这一修正回灌给脚本 | 时钟语义是各套件的核心假设，接口要设计得**兼容两种用法**，否则会逼一侧改驱动 |
| 源码未改动快照 | 黑盒 `:3364-3393`、`:3296-3347`；框架 `framework/cf/harness.py:852-884` | 中 | 把黑盒更强的版本（多扩展名 + `.pyc` 单列 + 降级 note）作为唯一实现，框架侧顺带升级 | 黑盒的 `OWNED_TREES`（`:3355-3361`）是黑盒专属的"我 import 了什么"清单，不能直接搬到框架 |
| 插件配置字典 | 黑盒 `:1517-1539`、框架 `framework/cf/host.py:59-82` | 低 | 消除"注释说 identically、值其实不同"（§2.5）的偏差 | 两套的调优取向不同（框架要长 render 超时给人看流式），共享时应是**基线 + 覆盖**而不是单一份 |
| `Reply` / `http_call` | 黑盒 `:665-752`、关系 `:1063-1137`、韧性 `:487-584`、框架 `framework/cf/program.py:32-200` | 中 | 框架的 `ProgramClient` 是唯一带类型化端点的版本，可作基线 | 脚本版本是"不抛异常、把错误记进 `Reply.error`"，框架版本是抛 `ProgramError`；两种错误策略都要保留 |
| `Verifier` / `Check` / `Section` | 黑盒 `:290-486`、韧性 `:214-376`、关系 `:619-799`、记忆 `:109-176` | 中 | 四份同构的报告器只留一份 | 各脚本的 report.json 形状已固化，抽取时不能改字段名 |
| `scrub_environment` / `PROVIDER_ENV_PATTERN` | 黑盒 `:569-580`、关系 `:888-895`；韧性用显式名单 `:3690-3708` | 低 | 三处一份 | 低 |
| `_short` / `_short_json`（诊断渲染） | 黑盒 `:528-548`、关系 `:841-861`、韧性 `:441-461`、记忆 `:317-320` | 低 | 消掉 4 份 | 低 |
| `Platform` 假平台 | 黑盒 `:763-806`、关系 `:1149-1177`、框架 `framework/cf/host.py:99-144`、韧性 `:1017-1043` | 中 | 一份 | 各版本的会话地址簿细节不同，属于"看起来一样、其实各自服务不同剧本"的部分，**建议只对齐语义、不强行合并** |

**不建议共享**（正是 §7.1 要保护的独立性）：

* 阶段驱动与剧本本体（`PHASES`、`Story.step`、`AdapterHarness` 的 phase 逻辑）——
  这些就是"两个套件验证不同东西"的载体。
* `Faults` 注错开关（黑盒 `:1005-1056` vs 关系 `:1254-1313` vs 框架的 `MockScript`）——
  故障的**层面**不同，合并会把"注错也能咬人"这件事变成耦合。
* 断言文案。黑盒的检查是中文/英文用户可见事实（`:387-404`），韧性的检查是契约字段
  （`:233-376`）；文案共享会让读者分不清"这是谁的证据"。

**成本/收益小结：** 上表前 4 项（`free_port`、时钟重绑、源码快照、插件配置）抽取的收益最大、
风险最小，其中"插件配置"与"源码快照"还能顺带**修掉两个已在漂移的偏差**（§2.5、§2.7）。
后 4 项（`Reply`、`Verifier`、`scrub`、`_short`）收益是"少维护几百行"，但要小心保留两侧
不同的错误策略与报告字段，属于可做可不做。

---

## 5. 如果要合并，需要先回答的问题

以下问题必须**先有书面答案**，否则合并后的产物会同时继承两套假设而无法判定归属。

1. **合并后谁是发布门禁？** 335 项固定验收（`scripts/e2e_resilience_simulation.py:3674`）与
   271 个框架测试（`framework/README.md:412-431`）的退出码语义不同：前者是"发布前必须全绿"，
   后者是"实验台可用"。并进一个入口后，一次实验失败是否等于一次发布失败？
2. **`cf` 包还继续"只用标准库、可独立 pip 安装"吗？** 现在 `framework/pyproject.toml`
   声明 `dependencies = []`，测试才借 runtime 的 venv。合并会不会把 `scripts/` 的
   argparse 单文件入口也塞进这个包？
3. **时间控制以谁为准？** `scripts/` 需要"可复现的整步跳"（`scripts/blackbox_user_simulation.py:1760-1814`），
   `framework/` 需要"外部可拨"（`framework/cf/clock.py:251-322`）。统一成一个时钟时，
   gate/rounds 的可观测性要不要保留？后台心跳要不要统一关闭策略（`framework/README.md:463-466`）？
4. **插件加载方式二选一还是并存？** 真插件（黑盒 `:1480-1544`、关系 `:1815-1850`、
   框架 `framework/cf/host.py:345-393`）与适配器内核直连（韧性 `:168-194`）覆盖不同层，
   合并后是一套 harness 支持两种模式，还是只留一种？
5. **注错入口归谁？** `scripts/` 的 `--fault`（黑盒 `:119-128`、关系 `:1254-1313`、
   记忆 `:40-47`）和框架的 `--script`（`framework/README.md:202-211`）是两个不同层面的开关。
   合并后是同一套 CLI 选项还是两个子命令？
6. **报告产物归谁？** `report.json` / `diagnostics.log`（黑盒 `:3430-3451`）与
   `trace.jsonl` / `framework.log`（`framework/README.md:365-372`）的消费方不同。
   合并后是否要求单一 schema？
7. **`scripts/` 内部的分工要不要先合并？** 黑盒与关系脚本本身就有大量逐行重复
   （`SimClock`、`install_process_clock`、`Platform`、`HostLLM`、`Reply`）。
   如果 `scripts/` 内部都没抽公共件，直接谈"与 framework 合并"会越过一个更该先做的小步骤。
8. **文档口径谁统一？** `framework/README.md:417`（227）、`:451`（102）、`HANDOFF.md:292`（102）
   与本次收集到的 271 不一致；黑盒 `HANDOFF.md:287` 写 70、既有 report.json 是 77。
   合并前需要一次"文档数字与代码对齐"的动作，否则合并后的文档立刻又是假的。
9. **谁在什么环境下跑"需要原程序"的那部分？** `framework/tests/conftest.py:19-34` 里
   `program_available()` 让框架测试可跳过；`scripts/` 则是缺依赖直接退 2
   （`scripts/blackbox_user_simulation.py:116-117`）。合并后 CI/本地无原程序时的行为要定清楚。
10. **合并后"这次失败是被测物的错还是测试台的错"怎么回答？** 现在这个问题由两个套件的
    边界回答（`framework/README.md:256-257`）；合并会抹掉这条边界。

---

## 6. 对两侧自我声明的核对

| 声明 | 出处 | 核对结果 |
|---|---|---|
| `framework/` "一行都不改原程序" | `framework/README.md:4` | **成立（就源码文件而言）**。`cf/` 下没有任何对 `runtime/src` 的写入；测试里的 `monkeypatch` 只用于 `CF_MAIN_LLM_*` 环境变量（`framework/tests/test_main_llm.py:123-127`、`framework/tests/test_config.py:306-308`）。运行结束时用前后快照核对（`framework/cf/harness.py:819-826`、`framework/tests/test_harness_e2e.py:80-108`）。 |
| `framework/` "不改代码、不碰私有对象" | `framework/README.md:179` | **部分成立，措辞过宽**。它 import 了 Runtime 的非公开入口 `create_app`/`Runtime`/`Scheduler`/`load_config`/`ValueProfile`（`framework/cf/harness.py:295-298`、`:351-352`、`:402`、`:448`、`:505`），重绑了模块属性 `utcnow`/`utc_now_iso`（`framework/cf/clock.py:392-401`），并调用 `runtime.lazy_tick` / `runtime.endogenous_round` / `runtime.close` / `runtime.semantic_provider`（`framework/cf/harness.py:605`、`:718`、`:816`、`:739`）。**没有**访问下划线私有属性。"不改代码"成立，"不碰私有对象"不成立，且 README §12（`:457-459`）自己承认"不是黑盒"。 |
| `framework/` 与 `scripts/` "两者互不依赖" | `framework/README.md:47-48`、`HANDOFF.md:301-302`(§7.1 精神) | **成立**。`cf/` 不 import `scripts/`，`scripts/` 不 import `cf/`（全文 grep 无交叉 import）。框架对插件的使用走自己的 `framework/cf/host.py`。 |
| `scripts/` "起一个真实 uvicorn" | `HANDOFF.md:98`、黑盒 `:21-23`、韧性 `:4-7`、关系 `:23-25` | **对黑盒/韧性/关系成立，对记忆脚本不成立**。记忆脚本用 `fastapi.testclient.TestClient`（`scripts/e2e_memory_simulation.py:195-199`）在进程内直接调 ASGI，没有真 socket。文档没有明确区分这一点。 |
| 黑盒"70/70" | `HANDOFF.md:287`、`:214` | **与既有产物不一致**。`bb-run/report.json` 的 `totals` 是 `{"passed": 77, "failed": 0}`。文档数字过期。 |
| 关系递进"105 项检查全绿" | 提交 `17f5ef3` 提交信息 | **未复跑核实**，但静态 `V.check(` 调用点 95 个（循环展开后可到 105），与提交信息自洽。 |
| 框架"335 项检查"（韧性） | `HANDOFF.md:287`、`framework/README.md:47` | **与既有产物一致**：`res-run/report.json` 的 `totals` 是 `{"passed": 335, "failed": 0}`。 |
| 框架测试规模"102" / "227" | `HANDOFF.md:292`、`framework/README.md:417`、`:451` | **不一致**。收集到 **271** 项（test_cli 23、test_clock 32、test_config 53、test_harness_e2e 19、test_host 25、test_logbook 12、test_main_llm 32、test_mock_openai 21、test_onebot 16、test_tui 38）；**父代理复核实跑为 `271 passed in 44.71s`**。三处文档数字都过期，已在本轮改掉。 |

---

## 7. 核实与未核实

**已核实（读代码/配置/既有产物）**

* 四个脚本的到达 Runtime 方式、时间控制方式、注错机制、断言数（黑盒 77、韧性 335 来自既有
  `report.json`；关系 105 来自提交信息；记忆 23 个静态调用点）。
* 框架测试 271 项：本次 `python -m pytest tests --collect-only -q`（**只收集，不执行**）。
* 框架是否 import/改写 Runtime、monkeypatch 用在哪、是否写 `runtime/src`。
* `framework/cf/*` 各模块职责与控制面回环限制。
* 两侧重复件的具体行号（§4 表）。

**未核实（明确说明，不猜）**

1. ~~**四个脚本本次都没有运行**~~（评估本身确实没跑，这是题目要求）。**父代理复核时补跑了黑盒与韧性**：
   黑盒 `77/77`、韧性 `335/335`，退出码都是 0（见 §9）。**仍未跑**：记忆质量与关系递进。
   原评估所依据的 `report.json` 生成于 2026-09-16T02:34Z。
2. **记忆脚本的运行时断言总数未核实。** 静态有 23 个 `V.check(` 调用点
   （`scripts/e2e_memory_simulation.py:340-635`），其中 `phase_kinds` 的循环
   （`:383-394`）会按 4 个种类展开，所以运行时大概率多于 23，但确切数没有 report.json 可读。
3. **关系递进脚本没有 report.json 产物**（工作区里只有 `bb-run/`、`res-run/`），
   105 这个数字只有一个提交信息作证，未独立复跑。
4. **"框架测试 271 项中，有多少会在原程序不可用时 skip"未逐项核实**；
   `framework/tests/conftest.py:19-34` 的 `program_available()` 机制存在，
   但本次环境有原程序，所以没有出现 skip 分支。
5. **`scripts/` 与 `framework/` 是否存在隐藏的运行时耦合未做动态验证**（例如
   `install_process_clock` 重绑是否会在同一进程里串到另一个套件）。这需要实际在
   同一进程先后跑两者才能观察，本次只做了静态检查（无交叉 import）。
6. **`framework/README.md:417` 的 227 与 §11 的 102 哪个是笔误未核实**——只确认两者都与
   本次收集的 271 不符。

---

## 8. 一句话回答题目

**不该合并套件，该共享机械件。** `framework/` 与 `scripts/` 是在同一被测物上做两件不同的事：
前者回答"改成这样会发生什么"，后者回答"发布前这些必须绿"。它们重叠的部分（真 uvicorn、
时钟重绑、真插件、假平台、源码未改动核对）已经在用不同强度各自验证，合并只会让"谁的证据"
变模糊；而它们各自独有的能力（运行中拨时间 vs 真实 Scheduler 自主决策、真 mock 强语义 vs
适配器传输层注错）都不是合并能产生的。真正值得做的，是把 `free_port`、时钟重绑、
源码快照、插件配置这四类**无行为语义的机械件**抽到中立位置——其中后两类还能顺带修掉
本报告 §2.5、§2.7 记录的两个已经发生的漂移。

---

## 9. 父代理独立复核（2026-09-16）

本报告的证据链由父代理逐条复核过。**结论未被推翻**，下面是复核记录，包括一处它比我先对的地方。

### 9.1 复核方法

不信报告里的话，只信自己读到的行与自己跑出来的数：对 §0 的三条最强证据与 §4 的共享清单，
逐条打开源码核对行号；另起两次仿真与一次完整框架测试套件。

### 9.2 实测补充（把报告里"未核实"的三项变成已核实）

| 项 | 报告 | 复核实测 |
|---|---|---|
| 框架测试 | `--collect-only` 得 271 项（未执行） | **`271 passed in 44.71s`**（`framework/` 下 `pytest tests`） |
| 黑盒 | 读既有 `report.json` 得 77 | **`77/77`，退出码 0** |
| 韧性 | 读既有 `report.json` 得 335 | **`335/335`，退出码 0** |

### 9.3 逐条复核结果（全部为"确认"）

1. **插件配置漂移**：`framework/cf/host.py` 与 `scripts/blackbox_user_simulation.py:1517-1539`
   逐键对比，确认 5 个值不同（`context_timeout_ms` 2000/500、`context_prefetch` **False/True**、
   `request_timeout_ms` 5000/2000、`render_timeout_ms` 60000/10000、`queue_max_backoff_ms` 1000/500）。
   `context_prefetch` 是**反的**，不只是数值不同。
   **补一条报告没写的区分**：`runtime_base_url` 在框架里**不是漂移**——框架在构造时注入
   （`framework/cf/host.py:376`），黑盒才把它放进同一个字典。所以"逐键不一致"里有 1 项是结构差异，
   真正要解释的是那 5 项。**没有任何地方记录这 5 项的理由**（`framework/README.md` 全文无关）。
2. **黑盒不调 `/endogenous`**：`:26` 自述 + `:1760-1814` 读 `scheduler.status()["rounds"]` 等它自增，确认。
3. **框架的"自主开口"测试是强制的**：`framework/tests/test_host.py:242-261`
   `test_the_runtime_decides_to_speak_on_its_own` 调 `live.endogenous({"force": True})`。
   但它的断言 `acted["reason"] == "hazard_triggered"` 仍然在测危险率本身——所以两侧是
   **互补而非重复**：框架测"给定一轮，危险率会不会触发"，黑盒测"调度器会不会自己醒来"。
4. **框架 README 自述心跳吃掉窗口**：`framework/README.md:463-466` 确认（`delta_t≈0`、行动概率归零）。
5. **框架时钟能力**：`framework/cf/clock.py` 确有 `set`(:251) / `advance`(:266) / `set_scale`(:281) /
   `freeze`(:305) / `scale`(:327)，确认强于黑盒的 `SimClock`（`blackbox:596-624`）。
6. **记忆脚本不起真服务器**：`scripts/e2e_memory_simulation.py` 的 `uvicorn` 出现次数为 **0**，
   用 `TestClient`（`:195-199`）；另三个脚本各 10 次。确认。
7. **框架源码快照更弱**：`framework/cf/harness.py:862` 只 `src.rglob("*.py")`；黑盒
   `:3296-3314` 盯 6 种扩展名、`.pyc` 单列一条检查与一条 note。确认。
8. **`free_port` ×4**：确认，且**报告比我先对**——我按 `def free_port` 搜只找到 3 处，
   差点把报告的"4 份"当成错误；第 4 份是 `framework/cf/harness.py:843` 的 `_free_port()`
   （带下划线，所以我的模式漏了）。报告 §4 表里已经正确引用了 `:843-849`。

### 9.4 复核顺带修掉的代码问题

- `framework/cf/host.py` 的那句 `Copied from the shipped black-box simulation so both drive the
  plugin identically` **是错的**（既与代码不符，也无法从代码验证），已改成列出 5 项真实差异、
  标出 1 项结构差异、并注明"理由没有任何地方记录"。
- 过期数字：`framework/README.md` 的 `227 passed` → `271 passed`、`102 个测试` → `271 个测试`；
  `HANDOFF.md` 的 `框架自带 102 个测试` → `271`、`黑盒 12 阶段 70 项` → `13 阶段 77 项`，
  并在状态表里补了 framework 一行。

### 9.5 仍未核实（保持报告的口径）

- 关系递进的 105 项：无 `report.json`，只有提交信息作证，**没跑**。
- 记忆脚本的运行时断言总数：静态 23 个 `V.check(` 调用点，`phase_kinds` 循环会展开，确切数未跑。
- 框架 271 项在原程序缺失时的 skip 数：本环境有原程序，skip 分支未触发。
- 两侧运行时耦合（`install_process_clock` 同进程先后跑是否串）：只做了静态无交叉 import 检查。
