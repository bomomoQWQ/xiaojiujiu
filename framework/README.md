# 小九九 · 外接测试框架（companion framework）

一个**外接**的测试框架：它把同一仓库里的 Runtime 跑起来、喂它输入、任意摆弄它的时间、把它的内部变量写成日志，
**并且一行都不改原程序**。

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

## 1. 四个能力

| 能力 | 在哪 | 说明 |
|---|---|---|
| **可控虚拟时钟** | `cf/clock.py`、`cf/control.py` | 运行中可 `set` / `advance` / `scale` / `freeze`，命令行驱动 |
| **OpenAI 兼容端点** | `cf/mock_openai.py` | 作为原程序「强语义理解」（`remote_api` provider）的**输入源**，响应可脚本化、可注错 |
| **跑马日志** | `cf/logbook.py` | 轮转文本日志 + 结构化 JSONL 轨迹，逐条心跳记录变量 |
| **组装与命令行** | `cf/harness.py`、`cf/cli.py` | 一键起全套，`cf` 命令控制 |

---

## 2. 快速开始

框架本身**只用标准库**，但它要启动原程序（需要 uvicorn/fastapi），所以用原程序的 venv 跑：

```bash
cd framework
# 用绝对路径：相对路径会让 Python 打两条 "Unexpected value in sys.prefix" 的
# RuntimeWarning（sys.prefix 与解释器路径对不上）。无害，但很吵。
PY="$(cd ../runtime && pwd)/.venv/bin/python"

# 终端 A：起一个 harness（前台，Ctrl-C 停）
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

## 6. 日志里有什么

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

## 7. 跑测试

```bash
cd framework
PY="$(cd ../runtime && pwd)/.venv/bin/python"
"$PY" -m pytest tests          # 107 passed
```

| 文件 | 覆盖 | 需要原程序 |
|---|---|---|
| `test_clock.py` | 时间解析、时钟算术、进程内重绑 | 否 |
| `test_logbook.py` | 双写、轮转、过滤、程序日志桥接 | 否 |
| `test_mock_openai.py` | 两种契约、grounding、脚本与注错、token 不入日志 | 否 |
| `test_harness_e2e.py` | 真程序端到端：接线、纪元、变量一致、故障降级、源码未改动 | **是** |
| `test_cli.py` | 子进程跑 `cf run`，再用客户端命令驱动它（时间、tick、say、refresh、backlog、tail、shutdown） | **是** |

后两个文件在原程序不可用时会自动 skip，所以只装框架也能跑前三个。

---

## 8. 目录

| 路径 | 说明 |
|---|---|
| `cf/clock.py` | 可控时钟 + `install_process_clock`（重绑 `utcnow` / `utc_now_iso`） |
| `cf/logbook.py` | 轮转日志、JSONL 轨迹、`LogBridge`（原程序日志桥接） |
| `cf/mock_openai.py` | OpenAI 兼容端点、grounded 默认回复、`MockScript` / `MockReply` 注错 |
| `cf/variables.py` | 变量采集（只走公开只读端点，失败不致命） |
| `cf/program.py` | 原程序 HTTP 客户端（说话 / tick / 内源回合 / 深刷新） |
| `cf/control.py` | 控制面（仅回环，非回环地址会被拒绝） |
| `cf/harness.py` | 组装：mock → 原程序 → uvicorn → 调度器 → 控制面 → 心跳 |
| `cf/cli.py` | `cf` 命令 |
| `tests/` | 102 个测试 |

---

## 9. 已知边界

* **不是黑盒**：为了完整接管时间，原程序被导入本进程（见 §4）。它仍然不被修改，但共享进程。
* **单进程单 harness**：同时跑两个 harness 需要各自的 venv 或进程，因为 `install_process_clock`
  会重绑整个进程的时钟。
* **控制面无鉴权**：只绑回环，且 `ControlServer` 会拒绝非回环地址（除非显式 `allow_remote=True`）。
  把它暴露到可路由地址等于把"把角色快进十年并给用户刷屏"的能力交出去。
* **`/cognition/refresh` 的信号名**是 HTTP 契约里的坑，见 §5。

---

## 10. 构建过程中发现的、属于原程序的现象

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
