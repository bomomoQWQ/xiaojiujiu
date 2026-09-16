# 小九九 · 外接测试框架（companion framework）

一个**外接**的测试框架：起一个**聊天窗口**，你和主 LLM 说话，Runtime 在后台记着、攒着、到点自己开口——
同时把它的内部变量写成日志，还能任意摆弄它的时间。**并且一行都不改原程序。**

```console
$ cf chat --start-time 2026-09-15T09:00:00Z
小九九 · 外接框架聊天窗口
  Runtime   : http://127.0.0.1:54739
  主 LLM    : deepseek-chat @ https://api.deepseek.com/v1
  会话      : webchat:FriendMessage:default

[09-15 09:00 ×1 | 心境 +0.00 | I/R/P 0.06/0.51/0.00 | 未决 0 | 候选 0 | 未结 0]
你> 我明天下午三点面试，结束了告诉你。
◆ 小九九(回复) 09-15 09:00
好，等你消息。别太紧张，正常发挥就行。

[…] 你走开了 8 小时（/advance 8h）[…]

◆ 小九九(主动) 09-15 17:00          ← 没人问它，它自己想起来的
面试怎么样？
```

聊天窗口里你打的话会**连同 Runtime 注入的背景块**一起送给主 LLM；
Runtime 那边同时在攒情绪、记未结之事、跑动机博弈，到点通过真实插件把消息渲染出来发给你。

框架住在主项目里，但与被测代码严格分开：

```
xiaojiujiu/                       ← 主项目
├── runtime/                      ← 被测的原程序（框架从不修改它）
│   └── src/companion_runtime/
├── framework/                    ← 本框架
│   ├── cf/                       ← 时钟 / mock 端点 / 日志 / 控制面 / 命令行
│   └── tests/
└── scripts/                      ← 原程序自带的两个仿真脚本

┌──────────────────────────┐      ┌────────────────────────────┐
│ SQLite + lazy_tick       │◀─────│ 可控虚拟时钟（重绑 utcnow）  │
│ SemanticProvider         │◀─────│ OpenAI 兼容 mock 端点        │
│ 公开 HTTP API            │◀─────│ 心跳 + 变量采集 + 日志        │
└──────────────────────────┘      └────────────────────────────┘
              ▲                                 │
              └──────── 只走 HTTP，不改代码 ──────┘
```

> 它和 `scripts/` 下的两个仿真脚本是**两回事**：那两个是为特定剧本写死的验收脚本（12 个阶段、335 项检查），
> 本框架是可交互、可复用、可外部调控时间的通用实验台。两者互不依赖。

---

## 1. 能力一览

| 能力 | 在哪 | 说明 |
|---|---|---|
| **聊天窗口（TUI）** | `cf/tui.py` | 行式 REPL + 实时状态栏；**流式回复**；主动消息在你打字时插进来 |
| **主 LLM 客户端** | `cf/main_llm.py` | 标准 OpenAI 兼容客户端（含 **SSE 流式**），接到宿主主 LLM 的接缝上 |
| **AstrBot 宿主模拟** | `cf/host.py` | 假平台 + 真插件（拿 AstrBot 桩加载），走真实的 observe→注入→生成→投递→回报 |
| **客户端配置文件** | `cf/config.py` | system prompt + 8 个价值观轴打包成**命名人格档案**，可切换；key 只从环境变量读 |
| **人格价值观参数** | `cf/harness.py`、`cf/cli.py` | 8 个轴，配置或 `--values` 覆盖；它们是编译进动力学的性格，不是提示词装饰 |
| **可控虚拟时钟** | `cf/clock.py`、`cf/control.py` | 运行中可 `set` / `advance` / `scale` / `freeze`，命令行驱动 |
| **OpenAI 兼容端点** | `cf/mock_openai.py` | 作为原程序「强语义理解」（`remote_api`）的输入源，可脚本化、可注错 |
| **跑马日志** | `cf/logbook.py` | 轮转文本日志 + 结构化 JSONL 轨迹，逐条心跳记录变量 |

---

## 2. 快速开始

框架本身**只用标准库**，但它要启动原程序（需要 uvicorn/fastapi），所以用原程序的 venv 跑：

```bash
cd framework
# 装进原程序的 venv（框架要用它的 uvicorn/fastapi）。装完 `cf` 就是普通命令，
# 可以在任何目录、对着任何配置文件跑。
PY="$(cd ../runtime && pwd)/.venv/bin/python"     # 绝对路径，避免 sys.prefix 噪音警告
"$PY" -m pip install -e .

# 接真模型：key 只走环境变量，不要写进任何文件
export CF_MAIN_LLM_API_KEY=<你的 key>
# base_url / model 写在配置文件里（见 §6），也可以用 CF_MAIN_LLM_BASE_URL / MODEL 覆盖

# 生成配置 → 开聊（推荐入口）
cf config init cf.toml
cf chat --config cf.toml

# 或者只要一个后台 harness（无界面，用别的命令驱动）
"$PY" -m cf run --run-dir runs/demo --start-time 2026-09-15T09:00:00Z

# 终端 B：控制它
"$PY" -m cf status   --run-dir runs/demo
"$PY" -m cf time advance 8h --run-dir runs/demo
"$PY" -m cf tick     --by 1h --run-dir runs/demo
"$PY" -m cf tail     --run-dir runs/demo --kind heartbeat
```

`cf run` 会把 runtime / control / mock 三个地址打进 trace，其它命令用 `--run-dir` 自己去找，
所以日常不需要手抄端口（也可以用 `--control http://127.0.0.1:PORT` 显式指定）。

---

## 3. 命令行

三组命令：**起 harness**、**拨时间**、**喂输入看结果**。

```
— 聊天（推荐） —
cf config init      写一份带注释的客户端配置模板（连 personas/*.md 一起）
cf config show      打印解析后的最终配置（--config 必填，--persona 可选）

cf chat             开一个聊天窗口：你和主 LLM 说话，Runtime 在后台工作
                    --config cf.toml              客户端配置（命令行参数优先于它）
                    --persona guarded             启用哪个命名人格档案
                    --llm-base-url / --llm-model  覆盖端点（默认读 CF_MAIN_LLM_*）
                    --system-prompt               角色设定（宿主人格，最高优先级）
                    --values user_care=0.9,...    覆盖人格价值观轴（见 §6）
                    --mock-semantics              强语义改用自带 mock，而不是主 LLM 端点

— 起 —
cf run              启动一个可外部调控的 harness（前台，Ctrl-C 停）

— 拨时间（打控制面，影响心跳） —
cf time set         --value 2026-09-15T09:00:00Z   跳到绝对时刻
cf time advance     --value 8h / 90s / 1h30m / 2d  推进一段时间
cf time scale       --value 60                     虚拟秒/真实秒（0 = 时间不走）
cf time freeze / unfreeze                          钉住 / 恢复
cf tick             --by 2h                        立刻跑一次心跳（可先推进）
cf endogenous                                      强制一次内源决策回合
cf status           --full                         查看时钟与端口
cf shutdown                                        让运行中的 harness 停止

— 喂输入 / 看认知 —
cf say "…"          --conversation c1 --at <ISO> --event-id <id>   让用户说一句话
cf refresh          --at <ISO> --major-event --force               请求深层认知刷新（这条才打到 mock）
cf backlog          --limit 20                                     看还没被理解的事件

— 看日志 —
cf tail             --kind heartbeat --json --follow --limit N     看结构化轨迹
```

`cf run` 会把 runtime / control / mock 三个地址打进 trace，其它命令用 `--run-dir` 自己去找，
所以日常不需要手抄端口（也可以用 `--control` / `--runtime` 显式指定）。

几个容易忽略但很有用的点：

* `cf say --event-id <固定id>` 让重复调用**幂等**：第二次是 `duplicate: True` 而不是多一条消息。
  测重投递时用它，不要靠"再发一次"。
* `cf refresh` 的信号（`--major-event` 等）会放在 body **顶层**发给 `/cognition/refresh` —— 见 §5 的坑。
* `cf tick --by 1h` 是"推进 + 心跳"一步到位，比 `cf time advance 1h` 再 `cf tick` 少一条命令。

`cf run` 的常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--run-dir` | `runs/<时间戳>` | 产物目录 |
| `--program-src` | `../runtime/src` | 原程序 src 目录（框架位于主项目内，所以只差一层） |
| `--base-dir` | `<run-dir>/program` | 原程序数据库/镜像的位置 |
| `--start-time` | 真实当前时间 | 虚拟世界起始时刻。**原程序的创建纪元就等于它** |
| `--time-scale` | `1.0` | 虚拟秒/真实秒 |
| `--step` | 无 | 每个心跳自动推进的时长，如 `1h` |
| `--heartbeat-interval` | `1.0` | 心跳间隔（真实秒） |
| `--no-mock` | 关 | 不接 mock，用程序默认的 `disabled` provider |
| `--script` | 无 | 给 mock 装脚本，见 §5 |

---

## 4. 时间是怎么被接管的（以及它的边界）

原程序**没有时钟抽象**：130 多处直接读 `companion_runtime.utility.utcnow`。所以外接框架只有两条路：

1. 调 `POST /tick` 并传时间戳——能推进持久状态，但**调度器自己的唤醒、`/schedule`、attempt 记账仍然走真实时间**；
2. 把原程序**导入本进程**，重绑每个已导入模块里的 `utcnow`——整个进程一起走虚拟时间。

框架走第 2 条，因为**只接管一半的时间比不接管更糟**：一个把 `lazy_tick` 拨快却把调度器留在真实时间的测试，
演不出"用户离开了 8 小时"。重绑的做法和原仓库自带的两个仿真脚本一致。

因此要如实说明两点：

* **框架是"进程内包装"，不是把原程序当黑盒子进程跑。** 这是为了完整接管时间付出的代价。
  除时间之外的一切（变量、输入、投递）都只走公开 HTTP，不改代码、不碰私有对象。
* **原程序的 Scheduler 用真实时间的 `asyncio.sleep`**（它按设计"不跑固定心跳"）。时间被放大后，
  虚拟时间会比调度器的真实等待跑得快，所以**框架的心跳是时间流逝的主要驱动**——
  每 `--heartbeat-interval` 真实秒推进一次时钟并 `lazy_tick`，然后把看到的变量记下来。

同时框架会在每次心跳前**重新执行一次重绑**：启动之后才被导入的模块会拿到真实的 `utcnow`，
不重绑就会有一半代码偷偷跑在墙上时间上。`cf status` 里的 `rebound_bindings` 就是这个数。

---

## 5. mock 端点：喂给原程序「强语义理解」

原程序的 `RemoteAPIProvider` 会往任意 OpenAI 兼容端点发两个不同的提示词，靠 system message 区分：

| 提示词 | 要回来的东西 |
|---|---|
| **深层刷新**（"深层认知整理器"） | 六个集合的 JSON：`reinterpretations`、`psychological_interpretation`、`candidate_intent_operations`、`memory_suggestions`、`unfinished_matter_suggestions`、`user_model_evidence_suggestions` |
| **状态解释**（"情绪解释器"） | 六个短字符串：`experience`、`focus`、`conflict`、`impulse`、`inhibition`、`expression`（每个 ≤120 字） |

默认回复是**有据可依**的：它从原程序刚发来的请求里读变量（未决事件 id、心境、压力、候选池），
据此作答。这点很重要——一个永远返回空对象的 mock 会让整条强语义链路"看起来接好了，其实什么都没发生"。
每一条 `reinterpretation` 都引用请求里真实存在的 `event_id`，否则会被原程序的 grounding 检查丢掉。

**注错**（注错是测试框架的本职，只能成功的假端点测不了失败路径）：

```bash
# 200 之后一律 500
cf run --script 500
# 先正常一次，然后连续两次超时，再返回坏 JSON
cf run --script grounded --script timeout:5 --script timeout:5 --script malformed
# 直接指定返回体
cf run --script 'json:{"reinterpretations":[]}'
```

原程序对失败是 **fail-open** 的：连不上/超时/JSON 畸形一律降级成"没有强语义"，绝不抛异常、
绝不影响 Runtime 存活。框架的测试里有一条专门断言这个性质。

### 写场景时的两个坑（都踩过，都不是原程序的 bug）

1. **触发信号要放在 body 顶层**，不是嵌在 `trigger_context` 里：

   ```jsonc
   // ✅ 对：端点是 payload["major_event"]
   {"now": "...", "major_event": true}
   // ❌ 错：trigger_context 是 Runtime.deep_refresh 的 **Python 参数名**，不是 HTTP 字段
   {"trigger_context": {"major_event": true}}
   ```

   嵌套写法会被**静默忽略**，于是一次"我明确说了有大事发生"的刷新变成 `not_needed`。
   框架的 `ProgramClient.refresh(major_event=True)` 已经帮你摊平了。

2. **深层刷新有最小间隔**（`deep_refresh_min_interval_seconds`，默认 3600s），它是**前置否决**：
   间隔不到就不刷新，无论理由多充分。所以间隔 1 小时整的连续刷新会被挡掉，写场景时要留够间隔。

---

## 6. 客户端配置与人格

### 6.0 安装

```bash
cd framework && "$(cd ../runtime && pwd)/.venv/bin/python" -m pip install -e .
```

装完 `cf` 是普通命令。不装也能用（`cd framework && python -m cf ...`），
但只有装过才能**在任意目录**、对着**任意位置**的配置文件跑——配置文件的用途正是
放在实验旁边，而不是放在框架目录里。

### 6.1 生成并查看

```bash
cf config init cf.toml          # 写一份带注释的模板，连 personas/*.md 一起
cf config show --config cf.toml # 看解析后的最终结果（含生效的提示词）
cf chat --config cf.toml
cf chat --config cf.toml --persona guarded     # 换人格
```

配置文件配的是**实验**（谁在演、演谁、时间从哪开始、产物写哪），
Runtime 自己的 `runtime.toml` 配的是**被测对象**。分开是为了让"这次改动属于被测物还是测试台"永远可回答。

优先级：**命令行 > 配置文件 > 内置默认**。价值观是**合并**不是替换，所以
`--values user_care=0.9` 只调这一个轴，不会把人格档案里其他七个轴丢掉。

### 6.2 命名人格档案

一个角色 = 一段 system prompt **加上**和它相配的价值观轴。两者分开配很容易打架——
写成话痨、参数却是克制型，结果就是"说暖话但不跟进"。所以打包成档案：

```toml
[persona]
active = "gentle"

[persona.profiles.gentle]
description = "温和、主动、在意对方"
system_prompt_file = "personas/gentle.md"   # 长提示词放文件里
[persona.profiles.gentle.values]
user_care = 0.95
emotional_expression = 0.80
boundary_respect = 0.60

[persona.profiles.guarded]
system_prompt = """..."""                   # 也可以直接内联
[persona.profiles.guarded.values]
boundary_respect = 0.96
conflict_directness = 0.15
```

* **配置文件里所有相对路径都相对配置文件本身解析**，不是当前工作目录
  （`system_prompt_file` / `program_src` / `plugin_root` / `run_dir` 都是）。
  一条规则，而且是唯一能在换目录后还成立的规则。
  `cf config init` 会按目标目录**算出**正确的相对路径，所以生成到哪一层都能直接用。
* **未被启用的档案缺提示词文件不会阻塞启动**，只有你真的切到它才报错。
  一个没写完的人格不该让你连好用的那个都用不了。
* 轴名打错会**列出全部可用轴**；数值超出 0..1 直接拒绝。

### 6.3 凭据

**key 绝不写进配置文件。** 只从 `[llm].api_key_env` 指定的环境变量读（默认
`CF_MAIN_LLM_API_KEY`）。文件里出现 `api_key` / `token` / `secret` 这类键会被**拒绝加载**，
报错只指出键的路径、绝不回显值。`max_tokens` 这种含 "token" 的普通键不会被误伤
（按词边界判断，不是按子串）。

---

## 7. 人格：8 个价值观轴

架构文档 §4.2 把人格定义为**编译进动力学的数值**，而不是提示词里的一段形容。Runtime 有 8 个轴：

| 轴 | 默认 | 它决定什么 | 被谁读 |
|---|---|---|---|
| `autonomy` | 0.72 | 自我推进的意愿 | `emotion.py` |
| `boundary_respect` | 0.88 | 对边界的敬畏，被拒绝时的收敛 | `boundaries.py`、`motivation.py` |
| `emotional_expression` | 0.46 | 情绪有多直接地写进话里 | `emotion.py` |
| `relationship_maintenance` | 0.79 | 长期沉默后主动靠近的倾向 | `emotion.py`、`memory.py`、`motivation.py` |
| `user_care` | 0.85 | 被对方的未结之事推动的强度 | `emotion.py`、`motivation.py` |
| `conflict_directness` | 0.41 | 把话挑明的倾向 | `motivation.py` |
| `stability_commitment` | 0.81 | 不被单次波动带偏 | `emotion.py`、`memory.py` |
| `curiosity` | 0.76 | 追问与了解的驱动 | `motivation.py` |

覆盖方式：

```bash
cf chat --values "user_care=0.98,emotional_expression=0.9,boundary_respect=0.35"
cf chat --values-file persona.json        # {"user_care": 0.98, ...}
```

**注意两点**：

1. **只在创建运行时那一行时写入。** 已经有数据库的 `--run-dir` 改这些不会生效，
   框架会在日志里明确警告（`[values] ... DB 已存在，本次覆盖不会生效`）。
   想看效果就换一个空的 `--run-dir`。
2. **不配置也是一个选择，但要看得见。** 每次运行都会把生效的 8 个值写进 trace
   （`values_configured`），所以"用了库里默认人格"是记录在案的，而不是隐形的。

改这些**真的有区别**（同场景、同种子，只换价值观）：

| 人格 | 候选效用 | 平均行动概率 |
|---|---|---|
| 默认（克制型） | 1.356 | 0.377 |
| 热烈主动型（`user_care=0.98` `emotional_expression=0.90` `boundary_respect=0.35`） | 1.513 | **0.463**（+23%） |

---

## 8. 聊天窗口的人机功效

诚实清单。**已解决**：

| 问题 | 处理 |
|---|---|
| 等待回复时屏幕完全不动（实测 ttfb 634–827ms，长回复更久） | **流式输出**：首字到达即开始打印；等不到首字时显示 `正在输入… 3.2s` 计时 |
| 状态栏每回合重复刷，把对话挤走 | **只在真的变了才重印**（比较整行，相同就跳过） |
| 主动消息重绘时把状态栏也带着重印一遍 | 重绘走同一个打印锁，且同样去重 |
| 非 TTY 下回复整条不打印（去重守卫误吞） | 守卫只在**片段真的显示过**时才抑制投递回调 |

**只对用户回合流式，主动消息渲染不流式**——后者是角色在后台组织一条你还没收到的消息，
让你看着它逐字写出来，比等一小会儿更糟。这条是设计选择，不是没做。

**还没做**：

* 多行输入（粘贴长文本 / 换行编辑）
* 历史跨会话持久化（只有 readline 的会话内历史）
* 全屏 curses 版本（当前是行式，状态栏跟着滚动而不是钉在底部）
* 回复中的 Markdown / 代码块着色

---

## 9. 日志里有什么

每次运行产出两个文件（都在 `--run-dir` 下）：

| 文件 | 用途 |
|---|---|
| `framework.log` | 人读的轮转文本日志（8 MiB × 5），一行一条 |
| `trace.jsonl` | 机器读的结构化轨迹，**一行一个 JSON 对象，不轮转** |

每条心跳记录里都带这些变量（`cf tick` 会直接打出来）：

```
mood_valence / mood_arousal / mood_stability     心境
impulse / restraint / pressure                   I / R / P 动力学
allow_proactive / contact_count_today            主动性与今日触达
cooldown_until / foreground_pause_until          冷却与前台上锁
unresolved                                       未决事件数（等强语义理解）
unfinished_open / candidates_active              未结之事 / 候选意图
boundaries_effective                             生效中的边界
outbox_pending / outbox_delivered / attempts_open 投递与尝试
next_wake_at / next_wake_in_s / next_wake_reasons 调度器的下一次唤醒与理由
quiet_hours                                      是否落在免打扰时段
```

除了这些标量，`detail` 字段里还存了每个只读端点的完整响应（`/state`、`/schedule`、
`/cognition/backlog`、`/unfinished`、`/candidates`、`/boundaries`、`/outbox`、`/attempts`、`/health`），
取不到时进 `probe_errors` 而不是让整轮失败。

事件种类（`cf tail --kind` 可过滤）：

| kind | 含义 |
|---|---|
| `harness_start` / `harness_ready` / `harness_stop` | 生命周期，含三个端口与源码未改动核对 |
| `heartbeat` | 一次心跳及其全部变量 |
| `control` | 一次外部时间操作 |
| `mock_openai_call` | 原程序打到 mock 的每一次请求与我们的回复 |
| `program_log` | **原程序自己的日志**（WARNING/INFO），桥接进同一个文件 |
| `endogenous` | 一次内源决策回合 |

> `program_log` 这一条值得单说：原程序内部那些"tick 被夹到纪元""候选操作被拒"的 warning
> 默认只闪一下 stderr 就没了，而这往往是某个数字走反的唯一解释。现在它们和变量在同一个文件里。

**Secret 纪律**：mock 只记录"请求带没带 bearer token"（`auth_present: true/false`），
**绝不记录 token 的值**——原程序对自己的 key 就是这个标准，框架照做。测试里有一条专门断言 token 不出现在 trace 中。

---

## 10. 跑测试

```bash
cd framework
PY="$(cd ../runtime && pwd)/.venv/bin/python"
"$PY" -m pytest tests          # 271 passed（约 45s）
```

| 文件 | 覆盖 | 需要原程序 |
|---|---|---|
| `test_clock.py` | 时间解析、时钟算术、进程内重绑 | 否 |
| `test_logbook.py` | 双写、轮转、过滤、程序日志桥接 | 否 |
| `test_mock_openai.py` | 两种契约、grounding、脚本与注错、token 不入日志 | 否 |
| `test_harness_e2e.py` | 真程序端到端：接线、纪元、变量一致、故障降级、源码未改动 | **是** |
| `test_cli.py` | 子进程跑 `cf run`，再用客户端命令驱动它（时间、tick、say、refresh、backlog、tail、shutdown） | **是** |
| `test_main_llm.py` | OpenAI 客户端：请求形状、密钥不入日志、失败降级、reply/render 分流 | 否 |
| `test_config.py` | 客户端配置：两种格式、凭据拒绝、命名人格、提示词文件、模板开箱可用 | 否 |
| `test_host.py` | 假平台、事件桩、AstrBot 三接口；端到端驱动**真插件**；价值观轴生效 | 部分 |

后两个文件在原程序不可用时会自动 skip，所以只装框架也能跑前三个。

---

## 11. 目录

| 路径 | 说明 |
|---|---|
| `cf/tui.py` | 聊天窗口：行式 REPL、实时状态栏、主动消息插入、斜杠命令 |
| `cf/host.py` | AstrBot 宿主模拟：假平台 + 真插件 + 真实钩子顺序 |
| `cf/main_llm.py` | 主 LLM：OpenAI 兼容客户端 + 无端点时的确定性替身 |
| `cf/config.py` | 客户端配置：命名人格档案、价值观轴、凭据拒绝 |
| `cf/clock.py` | 可控时钟 + `install_process_clock`（重绑 `utcnow` / `utc_now_iso`） |
| `cf/logbook.py` | 轮转日志、JSONL 轨迹、`LogBridge`（原程序日志桥接） |
| `cf/mock_openai.py` | OpenAI 兼容端点、grounded 默认回复、`MockScript` / `MockReply` 注错 |
| `cf/variables.py` | 变量采集（只走公开只读端点，失败不致命） |
| `cf/program.py` | 原程序 HTTP 客户端（说话 / tick / 内源回合 / 深刷新） |
| `cf/control.py` | 控制面（仅回环，非回环地址会被拒绝） |
| `cf/harness.py` | 组装：mock → 原程序 → uvicorn → 调度器 → 控制面 → 心跳 |
| `cf/cli.py` | `cf` 命令 |
| `tests/` | 271 个测试 |

---

## 12. 已知边界

* **不是黑盒**：为了完整接管时间，原程序被导入本进程（见 §4）。它仍然不被修改，但共享进程。
* **单进程单 harness**：同时跑两个 harness 需要各自的 venv 或进程，因为 `install_process_clock`
  会重绑整个进程的时钟。
* **控制面无鉴权**：只绑回环，且 `ControlServer` 会拒绝非回环地址（除非显式 `allow_remote=True`）。
  把它暴露到可路由地址等于把"把角色快进十年并给用户刷屏"的能力交出去。
* **`/cognition/refresh` 的信号名**是 HTTP 契约里的坑，见 §5。
* **后台心跳会吃掉危险率窗口。** 每一拍 `lazy_tick` 都会消费掉它积分的流逝时间，
  所以"手动步进 + 后台心跳"会让 `endogenous_round` 看到的 `delta_t≈0`，行动概率归零——
  症状看起来像"这个角色就是不想说话"。要手动驱动场景就把 `--heartbeat-interval` 设成 0
  （框架会记一条 `heartbeat_disabled`）。
* **危险率是逐拍抽样的**，一次跳 30 小时只是抽了一次。要演"离开两天"就分成小步走
  （黑盒仿真用约 1.4 小时/步），否则你会以为它不主动，其实只是样本太少。
* **价值观参数只在创建运行时那一行时写入**，见 §7。
* **用管道喂输入时场景会跑得比插件上报还快。** 插件的上报是排队的（最多 0.5 秒
  冲刷一次），人和它对话时感觉不到，但 `printf ... | cf chat` 会在几毫秒内把整段
  脚本灌进去。要脚本化跑剧情就加一点延迟（或用 `cf tick` / `cf say` 分步驱动）。

---

## 13. 构建过程中发现的、属于原程序的现象

框架不改原程序，但把它当被测对象时发现了两处值得记录的东西（详见 `cf/harness.py` 的注释）：

1. **`CR_SEMANTIC_PROVIDER` 是个死开关。** README §7.4 把它写成可用的 provider 开关，
   但 `providers.resolve_provider_name()` 优先读 `config.semantic.provider`，而 `load_config()`
   总会把这个字段填成默认的 `"disabled"`——于是环境变量永远轮不上，`load_config` 还会顺手打一条
   `Ignoring unknown environment override: CR_SEMANTIC_PROVIDER`。真正有效的是双下划线形式
   `CR_SEMANTIC__PROVIDER`（走 `CR_` + 点路径的通用覆盖），或者直接设置配置字段。
   框架选后者（不受拼写形式影响）。**未修改原程序，仅记录。**
2. **`build_provider` 的 `CR_SEMANTIC_BASE_URL` / `CR_SEMANTIC_MODEL` / `CR_SEMANTIC_API_KEY`
   是直接读环境变量的**，不经过 `load_config`，所以它们能用——但同时也会各自打一条
   "Ignoring unknown environment override" 的 warning。噪音，不是故障。
