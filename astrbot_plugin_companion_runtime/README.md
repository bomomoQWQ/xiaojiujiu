# astrbot_plugin_companion_runtime

「小九九 · 内源主动型长期陪伴 AI Runtime」的 **宿主侧薄适配插件**。

> **它不是一个能单独用的插件。** 情绪、记忆、用户模型、候选意图、动机博弈与行动决策全部在
> 主程序 **小九九 Runtime** 里：<https://github.com/bomomoQWQ/xiaojiujiu>。
> 只装本插件而不跑 Runtime 时，AstrBot 的行为与未安装时**完全一致**——那是 fail-open 设计，
> 不是故障。

## 安装

**第一步：先把 Runtime 跑起来**（主仓库里有 `Dockerfile` 与 `docker-compose.yml`）：

```bash
git clone https://github.com/bomomoQWQ/xiaojiujiu.git
cd xiaojiujiu
docker compose up -d --build       # runtime + astrbot 一起起，插件已挂载好
curl http://127.0.0.1:8787/health  # 应当返回 200
```

AstrBot 已经用别的方式部署时，只跑 Runtime 一个容器：

```bash
docker run -d --name xiaojiujiu -p 127.0.0.1:8787:8787 \
  -v xiaojiujiu-data:/data xiaojiujiu
```

**第二步：安装本插件**。在 AstrBot WebUI 的「插件市场」搜索 `companion_runtime` 安装；
或手动克隆到 AstrBot 的插件目录：

```bash
git clone https://github.com/bomomoQWQ/astrbot_plugin_companion_runtime.git \
  AstrBot/data/plugins/astrbot_plugin_companion_runtime
```

**第三步：把插件指向 Runtime**。在插件配置里设置 `runtime_base_url`：

| 部署方式 | `runtime_base_url` |
|---|---|
| 两个容器都在 compose 网络里 | `http://runtime:8787` |
| 同主机分别部署（插件默认值） | `http://127.0.0.1:8787` |

多会话部署还要把 Runtime 的 `conversation_id` 设成会话的 `unified_msg_origin`
（如 `aiocqhttp:FriendMessage:10001`，可用环境变量 `CR_CONVERSATION_ID`），否则主动消息
无法投递回形成该意图的会话。细节见主仓库 README 与 `runtime/README.md`。

## 这个插件做什么

它不属于 AstrBot 本体，也不包含任何认知逻辑：情绪、记忆、用户模型、候选意图、动机博弈、
行动决策全部由 **Runtime** 负责；本插件只做四件机械的事：

1. **监听并异步上报**：把用户消息与机器人实际发出的消息写入 Runtime 的不可变事件日志；
2. **临时注入上下文**：在 `on_llm_request` 中，于**严格短超时**内取回 Runtime 当前上下文，
   以 `TextPart.mark_as_temp()` 注入本轮请求（永不进入永久对话历史）；
3. **带租约消费 outbox**：轮询 Runtime 的行动队列，执行 `render`（调用当前聊天模型渲染主动消息）与
   `send`（**经 Runtime 现场授权后**才真正发送）；
4. **fail-open**：Runtime 不可用时，AstrBot 的行为与未安装该插件时完全一致。

> 架构依据：`内源主动型长期陪伴AI_Runtime_完整架构设计.md`
> （尤其 §2.6 隐藏上下文只做临时注入、§59.1 Prompt 优先级、§60 入口屏障、§62 协议层、
> §68 行动尝试状态机、§69 `committed != sent`、§80 临时注入格式、§86.10 隐藏心理上下文不进入永久历史）。
>
> **架构补丁：** `PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`
> （即时演出层与持久认知层分离；注入块改为「背景」；本地生成式模型路线已被整体放弃）。
> 本 README 已与 v0.2 对齐，见 §0（认知前提）、§4.2（注入块语义）、§4.7（健康字段）、
> §4.8（状态命令现状）。

---

## 0. v0.2 认知前提（读本文档前请先读这一节）

以下四条是插件侧必须同步的**认知**（不是代码）。插件的行为与协议 v1 契约在 v0.2 下**没有变化**，
变化的是「注入块到底是什么」以及「Runtime 接不接模型」。

### 0.1 两层时间模型

补丁 v0.2 把系统正式拆成两个时间尺度：

```text
【即时演出层】 —— 由宿主主 LLM 完成
当前用户原话 + 最近对话上下文 + 宿主人格 + 已有 Runtime 状态
        ↓
主 LLM
        ↓
本轮即时反应（可见回复）

【持久认知层】 —— 由 Runtime 完成
本轮交互沉淀
        ↓
记忆 / 情绪余波 / 用户模型 / 未尽之事
        ↓
主动动力 / 候选意图 / 后验重解释
        ↓
影响未来轮次
```

原则：**主 LLM 管「现在这一刻怎么活」；Runtime 管「活过以后留下什么」。**

对本插件的直接含义：

- 插件**不参与**即时演出的语义计算。插件只把 Runtime 的文本作为**临时内容**交给主 LLM，
  本轮怎么反应由主 LLM 自己根据当前语境完成。
- 插件**不负责**持久认知，也不因为「本轮表现出了某种情绪」而写任何 Runtime 状态；
  写入永远只发生在 Runtime 内部（规则 / 统计 / 低频深层刷新 → Reducer）。
- 因此，「主 LLM 当场表现」与「Runtime 持久状态」**不是同一权限**，插件两者都不改写。

### 0.2 Runtime 默认不接任何模型，插件也不需要知道 provider 是什么

- **本地生成式模型路线已被整体放弃**（补丁 §16、§17、§29 的最终形态）：Runtime 里没有
  任何本地权重、本地推理进程或本地端点，对应的实现、语法约束文件、训练与实验脚本
  也已从工程中移除或归档。插件**不假设任何模型存在**：既不探测、也不要求权重或 warmup，
  配置里也没有任何对应开关。
- Runtime 的 `semantic_provider` 只有两种取值：`disabled`（**默认，标准配置**）与
  `remote_api`（可选的低频强语义）。已退役的本地名字（`local_cpu` / `local_gpu` 等）
  会被 Runtime 显式警告后按 `disabled` 处理 —— 插件侧不需要、也无法感知这个差别。
- `disabled` 下系统功能完整：显式事件由粗粒度规则结算，模糊事件记为 `unresolved`，
  心理上下文退回确定性模板。此时只是「想得没那么深」，没有任何缺件。
- 强语义能力（`remote_api`）是可选的**加速器**，只用于低频深层认知刷新，不用于每轮即时演出。
  弱 VPS 上不需要常驻生成模型权重与推理进程；核心常驻只剩 Bot Runtime、数据库、
  轻量任务队列与检索。
- **插件对 `disabled` 与 `remote_api` 的行为完全一致，而且不需要知道当前是哪一种**：
  上报、注入、outbox 消费三条路径都不读 provider 字段（状态命令只是把它显示出来，见 §4.8）。

### 0.3 未解释的事件是正常状态，不是错误

- Runtime 允许对模糊事件（例如「算了，也没什么。」）保持 `semantic_status = unresolved`，
  只记录原始事件与 `potential_relevance`，**不硬猜**。
- `unresolved` 积压会累积，也可能在几小时后被一次低频深层刷新重新解释
  （生成 `reappraisal_event`）。这是设计中的「允许错过当下，但不能丢失原始证据」。
- **因此 `unresolved` 计数上升不是故障信号**，不需要告警、不需要在本插件侧做任何补偿动作。

### 0.4 插件不需要新钩子

粗粒度语义结算、`unresolved` 积压、低频深层认知刷新**全部发生在 Runtime sidecar 内部**。
对宿主侧而言协议与接缝都没有变化：插件继续只有「上报 / 注入 / 租约消费」三件事，
不需要新增钩子、定时任务或事件类型。

---

## 1. 职责与非职责

| 是本职 | 不是本职（Runtime 负责） |
| --- | --- |
| 传递原始事件、会话标识、消息正文 | 事件评价、语义解释、推断与事实分离 |
| 在超时预算内取回并注入上下文文本 | 决定本轮该注入什么心理状态 |
| 原样包装注入文本（不解释、不排序、不改写） | 决定注入块在 Prompt 里的优先级语义 |
| 调用 AstrBot 当前 provider 做一次渲染 | 决定说什么、为什么说 |
| 在授权通过后发送消息并回报结果 | 决定何时主动、是否沉默、边界判定 |
| 有界重试与租约心跳 | 状态持久化、单写者 Reducer、版本与 APPLY/REBASE/DISCARD |

插件**不读取、不保存、不转发** AstrBot 的 provider API key：渲染一律通过
`Context.llm_generate()` 走宿主既有配置。插件自身不含任何凭据（见 §8）。

**v0.2 补充：注入是「背景」，不是「本轮该有的情绪」。** 插件对 Runtime 文本只做包装
（加标签）与长度截断，**绝不改写、绝不重排、绝不为它参与优先级排序**。优先级语义由 Runtime
在文本内部自带的使用说明声明，由主 LLM 遵守（见 §4.2）。

---

## 2. 安装

```text
AstrBot/
└── data/plugins/
    └── astrbot_plugin_companion_runtime/     ← 本目录整体放入
        ├── main.py
        ├── metadata.yaml
        ├── _conf_schema.json
        ├── requirements.txt
        └── companion_runtime/
```

- 依赖：仅使用标准库与 `aiohttp`，而 `aiohttp>=3.11.18` 已是 AstrBot 的硬依赖，
  因此 `requirements.txt` 保持为空（避免触发无意义的 pip 检查）。
- 元数据：`metadata.yaml` 声明 `astrbot_version: ">=4.28,<5"`，不满足时 AstrBot 会拒绝加载。
- 安装后在 WebUI 插件页配置 `runtime_base_url`（并可选 `runtime_token`），然后重载插件。

---

## 3. 配置

配置定义见 `_conf_schema.json`，完整项与默认值：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关闭后不监听、不注入、不消费 outbox |
| `runtime_base_url` | `http://127.0.0.1:8787` | Runtime HTTP 根地址（与 Runtime 自身默认端口一致） |
| `runtime_token` | `""`（secret） | `Authorization: Bearer <token>`；可用环境变量 `COMPANION_RUNTIME_TOKEN` 代替，避免把密钥写进配置文件 |
| `adapter_id` | `default` | 多个 AstrBot 实例接入同一 Runtime 时必须不同 |
| `request_timeout_ms` | `1500` | 普通请求超时 |
| `context_timeout_ms` | `400` | **注入路径硬超时**，范围 50–2000，超出强制截断 |
| `context_cache_ttl_ms` | `30000` | 缓存有效期；命中即零等待 |
| `context_cache_max_sessions` | `64` | 缓存会话数上限（LRU） |
| `context_prefetch` | `true` | 收到消息即后台预热缓存 |
| `observe_mode` | `wake` | `wake` / `all`，见下 |
| `report_assistant_messages` | `true` | 上报机器人实际发出的纯文本 |
| `inject_enabled` | `true` | 关则只上报不注入（注入内容为「背景」，见 §4.2） |
| `inject_max_chars` | `2000` | 注入文本长度上限，`0` 为不限（截断是插件**唯一**对注入文本的加工） |
| `outbox_enabled` | `true` | 关则 Runtime 无法主动联系用户 |
| `outbox_poll_interval_ms` | `1000` | 轮询间隔，失败指数退避至最长 30s |
| `outbox_max_actions_per_poll` | `2` | 单轮租约上限 |
| `outbox_lease_ttl_ms` | `30000` | 租约时长，长任务会自动续租 |
| `outbox_max_concurrency` | `1` | 行动并发（弱 VPS 建议 1） |
| `render_timeout_ms` | `60000` | 渲染超时 |
| `send_timeout_ms` | `20000` | 发送超时 |
| `queue_max_items` | `256` | 本地重试队列容量（写满丢弃最旧） |
| `queue_max_attempts` | `6` | 单条最大重试次数 |
| `queue_max_age_ms` | `600000` | 单条最长滞留时间 |
| `queue_base_backoff_ms` / `queue_max_backoff_ms` | `500` / `30000` | 退避区间（带随机抖动） |
| `queue_send_timeout_ms` | `10000` | 队列单次投递超时 |
| `debug` | `false` | 把超时/失败细节升级为 warning 级日志 |

> **v0.2：配置里没有、也不会有任何模型相关的开关。** Runtime 默认不接任何模型（§0.2），
> 插件不探测模型进程、不管理权重、不做 warmup，也没有「语义 provider」相关配置项：
> 这些完全属于 Runtime 侧（`semantic_provider` 见 §4.7）。插件对 `disabled` 与
> `remote_api` 两种 Runtime 配置的行为**完全一致**。

### `observe_mode` 请务必确认后再改

- `wake`（默认，推荐）：只上报 **AstrBot 本身就会处理** 的消息（私聊、被 @、唤醒词、回复机器人）。
  实现方式是自定义过滤器判定 `event.is_at_or_wake_command`，该字段只由真实的唤醒条件置位，
  因此本插件**永远不会**把一条普通群消息变成唤醒事件；handler 内部还会再校验一次同样的条件
  （双保险：即使宿主的过滤器语义变化，`wake` 模式下也不会上报非唤醒消息）。
  另外注意：AstrBot 只有在 `is_at_or_wake_command=True` 时才会调用 LLM，因此即便过滤器完全失效，
  最坏后果也只是这类消息继续流经后续管线阶段，而不会让机器人回复它们。
- `all`：上报全部消息。代价是：AstrBot 会把这类消息标记为已唤醒（`is_wake=True`）并让它们继续
  流经限流、内容安全等后续管线阶段。这会改变宿主行为，仅在你清楚后果时开启。
- 无论哪种模式，**只有适配器真正在运行时**这个开关才可能为真：插件被关闭、启动失败（3 次后放弃）
  或已终止时都会把它复位为假，避免用一个已经不存在的适配器去放宽宿主管线（详见 §7）。

### 隐私：什么数据会离开宿主机

会上报：会话标识（`unified_msg_origin`）、平台名、消息类型、发送者 id/昵称、机器人 id、群 id、
消息 id、消息纯文本、时间戳。**不会**上报：AstrBot 的 API key、系统提示词、完整历史上下文、
图片/音频二进制（仅上报消息文本）。`runtime_token` 只放在请求头，从不写日志、从不出现在 `/companion_runtime` 输出中。

---

## 4. Runtime HTTP 协议（v1）

所有请求均为 `POST`，`Content-Type: application/json`，可选头 `Authorization: Bearer <token>`。
每个请求体都带 `protocol_version`；响应非 2xx 视为失败（观测类请求 fail-open，发送类授权 fail-closed）。

约定：

- `event_id` 由插件生成（`evt_<uuid4>`），**幂等键**：Runtime 收到重复 `event_id` 应直接 2xx 忽略。
- `(action_id, attempt_id)` 标识一次行动尝试：同一 attempt 重复租约只重放上次结果，不会二次执行；
  Runtime 想重试必须给出新的 `attempt_id`。
- 时间戳为 ISO-8601 UTC（毫秒精度，`Z` 结尾）。

### 4.1 `POST /v1/events` — 追加原始事件

```json
{
  "protocol_version": "1",
  "adapter_id": "default",
  "sent_at": "2026-01-01T12:00:00.000Z",
  "events": [
    {
      "event_id": "evt_9f1c…",
      "kind": "user_message",
      "session": "webchat:FriendMessage:user-1",
      "text": "今晚可能不来了",
      "occurred_at": "2026-01-01T12:00:00.000Z",
      "platform": "webchat",
      "message_type": "private",
      "sender_id": "user-1",
      "sender_name": "User",
      "self_id": "bot-1",
      "group_id": "",
      "message_id": "msg-1",
      "wake": true,
      "preempts_proactive": true
    }
  ]
}
```

- `kind`：`user_message` | `assistant_message`。
- `message_type`：`private` | `group` | `other`。AstrBot 的 `MessageType` 枚举值是平台消息类
  （`FriendMessage` / `GroupMessage` / `OtherMessage`），插件把这三种映射成上面的会话范围词；
  遇到未知取值时原样小写透传，便于排查而不是丢掉信息。
- `preempts_proactive=true` 由设计 §60 入口屏障定义：用户消息一到，Runtime 应立即暂停新的内源主动派发。
- 响应：任意 2xx。正文可忽略。

### 4.2 `POST /v1/context` — 取回当前注入上下文

请求：

```json
{"protocol_version": "1", "adapter_id": "default", "session": "webchat:FriendMessage:user-1",
 "trigger": "llm_request", "platform": "webchat", "last_event_id": "evt_9f1c…"}
```

响应（二选一）：

```json
{"context": {"text": "【使用说明】…【进入本轮前的长期状态（背景）】\n克制，想联系…",
 "version": "182", "ttl_ms": 30000}}
```

```json
{"context": {"version": "182", "sections": {"进入本轮前的长期状态（背景）": "…",
  "当前工作局势": "…", "当前最终意图": "…", "必要记忆": "…", "表达边界": "…"}}}
```

- **时延即体验**：本接口位于 `on_llm_request` 内，硬上限 2s、默认 400ms；超时即放弃注入（不报错、不改写请求）。
- 插件只做包装（`<companion_runtime_context version="…">…</companion_runtime_context>`）与长度截断，
  不解释语义；`sections` 会被拼成 `【段落】` 形式。
- 空响应 / 204 / 空对象：视为“本轮无上下文”，不注入。
- `trigger` 为 `llm_request`（前台请求路径）或 `message`（后台预热）。预热失败无副作用。

#### 注入块的内容是「背景」，不是「本轮该有的情绪」（v0.2）

补丁 v0.2 之前，心理段落试图告诉主 LLM「当前这句话应该产生什么情绪」；**现在不再是**。
Runtime 注入块描述的是：

> **角色进入本轮之前的长期状态**（长期心境底色、必要记忆、表达边界、当前工作局势等），
> 以及「你从哪来」，而不是「你现在该怎么反应」。

Runtime 会把这段使用说明和优先级顺序一并写进注入文本内部，插件**原样透传**。优先级顺序是：

```text
宿主最高层设定 / 安全约束
  > 当前用户原话
  > 当前确定事实
  > 显式边界
  > Runtime 持久心理状态
  > 心理解释缓存
  > 主 LLM 自然发挥
```

其中最关键的一条：**当前用户原话与当前事实高于 Runtime 的旧心理缓存。**
如果用户这句话和注入块里写的长期状态冲突，以当前用户原话为准；本轮即时反应由主 LLM
自己根据当前语境完成。

插件在这件事上的立场（也是不许越界的地方）：

- 插件**始终只把 Runtime 的文本作为临时内容注入**（`TextPart.mark_as_temp()`），
  它不会进入永久对话历史，也不是系统提示词。
- 插件**绝不改写**注入文本：不删段落、不调顺序、不加自己的「建议情绪」、不做优先级判断。
- 插件**绝不参与排序**：优先级由 Runtime 在文本内声明，由主 LLM 遵守；
  插件既不是排序方，也不懂得排序语义（它只认 `text` / `sections` 两种形状）。
- 「即时反应 ≠ 持久状态写入」：主 LLM 当场表现出的情绪**不会**被插件回写成 Runtime 状态。
  持久结算完全由 Runtime 内部完成。

### 4.3 `POST /v1/outbox/lease` — 租约行动

```json
{"protocol_version": "1", "adapter_id": "default",
 "capabilities": ["render", "send"], "max_actions": 2, "lease_ttl_ms": 30000}
```

```json
{"actions": [
  {"action_id": "act_1", "attempt_id": "att_7", "action_type": "send",
   "lease_id": "lease_5f…", "session": "webchat:FriendMessage:user-1",
   "lease_ttl_ms": 30000, "deadline_at": "2026-01-01T12:00:30.000Z",
   "payload": {"text": "在忙吗？"}}
]}
```

- `action_type=render` 的 payload：`{"prompt": "<Runtime 组合好的完整提示词>", "system_prompt": "可选", "max_chars": 0}`
  （插件**不会**再拼装语义，只把 prompt 交给当前 provider）。
- `action_type=send` 的 payload：`{"text": "<已渲染、待授权的正文>"}`。
- 未知 `action_type` 会被跳过并以 `skipped` 回报；缺 `session` 同样 `skipped`。
- 建议：`deadline_at` 到达后应允许其他适配器重新租约；无行动时返回 `{"actions": []}` 即可。

### 4.4 `POST /v1/outbox/{action_id}/heartbeat` — 续租

```json
{"protocol_version": "1", "adapter_id": "default", "lease_id": "lease_5f…", "extend_ms": 30000}
```

响应 `{"extended": true}`（或任意 2xx）。长时间 `render` 期间插件自动按 `lease_ttl/3`（最短 1s）续租。

- **续租从租约到手的那一刻开始**，包括排在并发闸门（`outbox_max_concurrency`）后面等待的整批行动：
  否则等待期间租约就会到期，Runtime 会把同一个行动再派给别的 worker，同一条主动消息就可能发两遍。
- **`extended=false` 是「租约已不属于你」，不是可重试的故障**：插件立即停止该行动（渲染中的直接取消），
  **不回报任何结果**（交由 Runtime 的租约到期恢复重派，见 §4.5），并且**不把这次续租计为成功**
  （状态命令里的 `heartbeats` 只统计被确认的续租）。网络错误、超时等瞬时故障不算失去租约：
  工作继续，但同样不计入成功。

### 4.5 `POST /v1/actions/{action_id}/authorize` — 发送前的现场授权

```json
{"protocol_version": "1", "adapter_id": "default", "lease_id": "lease_5f…",
 "session": "webchat:FriendMessage:user-1", "attempt_id": "att_7",
 "text_preview": "在忙吗？", "text_sha256": "…"}
```

```json
{"authorization": {"authorized": true, "reason": "", "text": "改口后的措辞"}}
```

- 语义：这是不可逆动作前的**最后一道闸门**；Runtime 应在此完成并发重协调
  （KEEP / MERGE / RERENDER / RESOLVED / ABORT）。`authorized=false` 时插件**不发送**，
  以 `rejected` 回报；`text` 非空则替换正文（RERENDER）。
- **超时、网络错误、响应不可解析一律 fail-closed：不发**。这是本插件唯一 fail-closed 的地方。
- 「没问到」和「被拒绝」必须分得开，而且**没问到时不写任何结果**：
  - `authorized=false`（Runtime 明确否决，含边界拦截、`unknown_action`、`lease_expired` 等）
    → `rejected`；
  - 请求没送达 / 响应里没有结论 → **不发，也不回报任何状态**，把这一行交回 Runtime 自己的
    租约到期恢复路径（见下）。原因：Runtime 对**任何**非 `ok` 的 send 结果都按终态处理
    （`mark_delivered(success=False)` → `nack(terminal=True)`），而一条只是**租约到期**的行会被
    `reclaim_expired` 收回成 `pending` 并重新派发。因此插件在这里保持沉默，是唯一能让「瞬时故障」
    保持可重试的形态；报 `rejected` 或 `failed` 都等于替 Runtime 关掉一行它从未评判过的行动。
  - 恢复代价（Runtime 侧契约）：最多等一个租约时长（`outbox.lease_seconds`，默认 45s）后由
    tick（最短间隔 5s）回收并重新租约；行内 `attempts` 未达 `max_attempts`（默认 3）时才会重派，
    用尽后仍会落 `failed` 终态。重复执行不会造成重复发送：这条消息从未发出。
  - 因同理，**租约被 Runtime 收回时（续租返回 `extended=false`）插件也停止工作且不回报**：
    Runtime 已经自己记录了这个决定，而一条来自失去租约方的终态结果会关掉它打算重派的行。
    唯一例外是这份租约丢失时投递**已经开始**（Runtime 已授权、消息可能已在路上）：那就等它落地
    并照实回报 —— 此时沉默会让 Runtime 重派一条可能已送达的消息（见 §7.1）。

### 4.6 `POST /v1/outbox/{action_id}/result` — 回报结果

```json
{"protocol_version": "1", "adapter_id": "default", "action_id": "act_1", "lease_id": "lease_5f…",
 "action_type": "send", "status": "ok", "attempt_id": "att_7",
 "session": "webchat:FriendMessage:user-1", "reported_at": "2026-01-01T12:00:01.000Z",
 "result": {"sent": true, "chars": 4, "authorized": true}}
```

| `status` | 含义 |
| --- | --- |
| `ok` | `render`：`result.text` 为渲染结果；`send`：`result.sent == true` 表示确实已投递 |
| `failed` | 执行失败（`error` 为单行原因：渲染/发送超时、异常、平台未找到、`delivery_failed`） |
| `rejected` | Runtime 明确否决，未发送（`error` 含原因，如 `aborted_by_user_message`） |
| `skipped` | 无法执行（`missing_session`、`unsupported_action_type:…`） |

> **不是所有情况都会回报。** 两种情况下插件刻意不写结果，把行留给 Runtime 的租约到期恢复：
> ① 发送授权没问到结论（§4.5）；② 续租被拒、租约已被 Runtime 收回且尚未开始投递（§4.4）。
> 两者都没有产生任何投递，所以重派不会造成重复发送。它们计入状态命令里的 `deferred`。
> 反过来，只要投递已经开始，插件一定回报真实结果（成功或失败）——那是防止重复发送的唯一手段。

> 行动 id 总是出现在请求路径中；仅结果回报的请求体额外附带 `action_id`，便于 Runtime 实现直接对齐。

- 设计 §69：`committed != sent`。只有 `status=ok` 且 `result.sent=true` 才算真正发出。
- 回报是幂等的；失败时进入本地有界重试队列，最终由租约到期兜底。
- 建议 Runtime 对 `rejected` 与 `skipped` 也落一条 `action_attempt` 终态，避免反复派发。
- 由插件主动留空的那两种情形（§4.5 / §4.4）不经过本地队列：它们不是「回报失败」，而是
  「不回报」，由 Runtime 自己的 `reclaim_expired` → `pending` → 重新租约来完成重试。

### 4.7 `GET /health` — 健康、语义 provider 与结算积压（v0.2）

补丁 v0.2 之前，「Runtime 是否装了本地模型」是运维必看项；本地模型路线已整体放弃，
可选的只剩远端 `remote_api`，因此健康检查改为回答两个问题：**有没有可用的语义 provider**、
**持久层故意留下多少未解释事件**。

```json
{
  "status": "ok",
  "runtime_version": "0.1.0",
  "semantic_provider": {
    "provider": "disabled",
    "available": false,
    "enabled": false,
    "reason": "disabled"
  },
  "semantics": {"by_status": {"unresolved": 12, "settled": 87}, "by_relevance": {"medium": 12}, "unresolved": 12}
}
```

> 上面是标准部署（`disabled`）的形态。配置 `remote_api` 时会多出 `base_url`、`model`、
> `api_key`、`stats`、`cache_entries` 字段。已退役的本地名字（`local_cpu` / `local_gpu` 等）
> 会被 Runtime 警告后回落 `disabled`，所以 `semantic_provider` 里**不会**出现 `local_*`。
> `available` 反映「是否真的能用」，而不是「是否配置过」。

`semantic_provider`（可选语义 provider 的自述，**绝不含密钥**）：

| 字段 | 含义 |
| --- | --- |
| `provider` | `disabled`（标准配置）/ `remote_api`（只有这两种） |
| `available` / `enabled` | 是否真的可用；`disabled` 时两者皆为 `false` |
| `reason` | 不可用原因（如 `disabled`） |
| `base_url` / `model` / `api_key` | 仅 `remote_api` 报告；`api_key` 只报 `configured` / `not configured` |
| `stats` / `cache_entries` | 调用计数与心理解释缓存条目数（实现相关） |

`semantics`（持久认知层的结算概况）：

| 字段 | 含义 |
| --- | --- |
| `by_status` | 各 `semantic_status` 的计数（`settled` / `unresolved` / …） |
| `by_relevance` | 各 `potential_relevance` 的计数 |
| `unresolved` | **未解释事件数**（即上面的 `unresolved` 计数） |

**怎么读这两个块（v0.2 的运维认知）：**

- `semantic_provider.provider == "disabled"` 是**完全正常的标准配置**，
  不是缺件、不是降级告警。此时粗粒度规则结算 + 确定性模板承担全部工作，
  `unresolved` 会更多，但长期连续性依然成立。
- `semantics.unresolved` 上升**是设计中的正常状态**（补丁 §11、§12、§31）：
  模糊事件被有意保留原始证据，等未来证据出现时再由低频深层刷新重新解释。
  它**不是错误**，不需要重试、不需要告警、不需要在插件侧做补偿。
- 低频深层认知刷新（粗粒度结算、`unresolved` 积压、后验重解释）**全部发生在 Runtime
  sidecar 内部**，可能由积压量、重大关系事件、未尽之事到期或系统空闲触发。
  宿主侧**不需要新增钩子**，也不应该尝试驱动它。
- 本插件只在状态命令里查询一次 `/health`，并只打印其中两个白名单字段（见 §4.8）；
  **不解析其余字段、也不据此改变任何行为**。

### 4.8 适配器侧现状：`/companion_runtime` 会查询 `/health`（v0.2，已实现）

`/companion_runtime` 状态命令除了宿主侧接缝的机械统计，还会**可选地**查询一次 Runtime 的
`GET /health`，把运行时的语义状况直接打在中间：

```text
companion Runtime adapter
- state / adapter_id / runtime / token
- observe_mode
- context deadline / ttl
- outbox: …
- semantic_provider: <name> (available=<bool>)              ← v0.2 新增
- semantics: <N> unresolved (normal: the Runtime defers …)  ← v0.2 新增
- context: N requests, N cache hits, N fetches, N timeouts, N errors, N stale fallbacks
- actions: N leased, N rendered, N sent, N rejected, N failed, N skipped, N replayed, N deferred (unreported, left to lease expiry)
- queue: N pending, N delivered, N retried, N dropped (full …/failed …/expired …)
- config issues: …
```

实现与边界（`main.py::_semantic_status_lines()`，已实现并有测试覆盖）：

- 复用既有 `transport.fetch_health()` 与 `request_timeout_ms`。整条探测是 **fail-open** 的：
  Runtime 没有 `/health`、进程不可达、响应不是 JSON 或拿不到响应体时，只多打一行
  `- cognition: unavailable (…)`，其余输出完全不受影响。
- 只渲染**白名单字段**：`semantic_provider` 的名字与 `available`、`semantics.unresolved`。
  `/health` 里的 `api_key` 只报 `configured` / `not configured`，插件**从不打印整个响应体**，
  因此运维信息不会变成泄露面（见 §8）。
- 这两行**只是展示**：上报、注入、outbox 消费三条路径都不读它们，
  `disabled` 与 `remote_api` 下插件行为完全一致，也不会因为 `unresolved` 偏高而做任何补偿。
- Runtime 是 v0.2 之前的版本（`semantic_provider` 不是对象）时，第一行改为
  `- semantic_provider: unknown (Runtime predates patch v0.2)`。

> ⚠️ **字段名不一致（现状，不是本文档笔误）**：状态命令从 `semantic_provider` 块里读 `name`，
> 而 Runtime 的 `/health` 报的是 `provider`（见 §4.7）。因此对接真实 Runtime 时，
> 上面第一行目前会打印 `semantic_provider: unknown (available=False)`。
> 这是实现侧一行待修的改动（本文档不修改 `.py`），在此如实记录。

> 与本地模型无关：状态命令既不假设任何模型存在，也不报告任何模型的健康度 ——
> 这两行回答的只是「Runtime 现在用哪种语义 provider」与「有多少事件被有意留作未解释」。
> 想看得更完整就直接查询 Runtime：
>
> ```bash
> curl -s http://127.0.0.1:8787/health | python -m json.tool
> ```

---

## 5. Runtime 侧契约（v0.2）

本章说明 v0.2 之后**宿主侧应该对 Runtime 抱有什么期待**，以及**哪些事不该由宿主侧操心**。
协议本身（§4）没有变化；变化的是持久认知层的内部能力。

### 5.1 v0.2 新增的三项 Runtime 内部能力

这三项**全部发生在 Runtime sidecar 内部**，对宿主侧不暴露新端点、不要求新钩子：

| 能力 | 做什么 | 宿主侧需要做什么 |
| --- | --- | --- |
| 粗粒度语义结算 | 只对高置信事件给出「方向 + 粗粒度强度 + 时间 + 来源」，不要求命名具体情绪 | 无。插件继续逐条上报原始事件即可 |
| `unresolved` 事件积压 | 低置信事件保留原始证据并标记未解释，不硬猜 | 无。**不要**因为计数上升而重试、补报或告警 |
| 低频深层认知刷新 | 必要时（积压、重大事件、未尽之事到期、系统空闲等）重新解释旧事件，生成 `reappraisal_event` 与建议集 | 无。宿主侧不驱动、不轮询、不代理它 |

Runtime 的 Reducer 仍是唯一写者：深层刷新只产出**建议**，最终经
`APPLY / REBASE / DISCARD` 落地。插件在这条链路上没有任何写入权限，也不该有。

### 5.2 未解释的事件是正常状态，不是错误

这是 v0.2 最容易误读的一点，请务必按运维口径理解：

- **`unresolved` 不是失败、不是降级、不是待处理告警。** 它表示持久认知层**有意**推迟结算：
  当前证据不足以高置信判断，于是只保存原始事件与 `potential_relevance`。
- 设计意图是「**允许错过当下，但不能丢失原始证据**」：主 LLM 当前轮已经有机会自然理解这句话，
  Runtime 不必马上硬猜；以后出现新证据时再回头重新解释，正是长期关系里很自然的叙事连续性。
- 因此下列做法都是**错的**：把 `unresolved` 当成上报失败而重发事件；为降低计数而放宽置信门槛；
  在插件侧加「未解释事件补偿」逻辑。原始事件已经落库，重复上报只会被 `event_id` 幂等忽略。
- `semantic_provider.provider == "disabled"`（标准配置）下 `unresolved` 会更多，这同样是正常的：
  确定性模板 + 粗粒度规则足以维持长期连续性，只是「后来想明白」的能力更弱。

### 5.3 双层时间模型下双方的义务

```text
宿主主 LLM：本轮即时理解与演出 —— 只看当前原话 + 上下文 + 注入的背景
Runtime   ：跨轮持久连续性     —— 上看长期状态、下结算长期影响
本插件    ：机械搬运           —— 上报 / 临时注入 / 租约消费，不含语义判断
```

- 插件**不要求** Runtime 在关键路径上语义完备：注入超时即放弃，Runtime 慢一点不会阻塞回复
  （§6 fail-open）。
- 插件**不假设** Runtime 有任何生成式模型可用：`disabled` 时全部接口照常工作。
- 插件**不解释**注入文本、**不排序**、**不改写**（§4.2）。
- Runtime **不要求**插件替它维持任何状态：进程重启后丢失的去重缓存与本地队列，
  靠租约到期与 `(action_id, attempt_id)` 语义兜底（§11）。

---

## 6. 失败策略与并发行为

**fail-open（不影响宿主）**
- 事件上报、上下文获取、结果回报：任何异常都被吞掉（仅 debug 日志），AstrBot 照常回复用户。
- 上下文获取超时/失败：优先使用过期缓存，其次放弃注入；**绝不阻塞或改写请求**。
- 渲染失败/超时：以 `failed` 回报，不影响前台对话。

**fail-closed（不替 Runtime 说话）**
- 只在 `send` 路径：无有效租约、授权请求失败、授权被拒 → 不发送。

**v0.2：Runtime 的语义结算状态永远不触发上面任何一条**
- `semantic_provider` 是 `disabled`、或 `semantics.unresolved` 很高，都**不是失败信号**：
  插件不因此重试、不因此降级、也不因此关闭注入。这些状态只影响 Runtime 内部「想得多深」，
  不影响宿主侧接缝的成败判定。
- 唯一与「语义能力」有关的失败面仍然只有注入超时（fail-open）与发送授权（fail-closed），
  两者都不看 provider 字段。

**有界与去重**
- 本地重试队列有界（默认 256），写满丢弃**最旧**条目；单条最多 6 次、最长 10 分钟，之后丢弃并计数。
- 同一 `(action_id, attempt_id)` 重复租约：重放已存结果，不二次渲染/发送（进程内最多记忆 256 条）。
- 同一 attempt 正在执行时收到重复租约：直接忽略。
- 队列与租约统计可通过 `/companion_runtime` 查看。

---

## 7. 生命周期

- `__init__`：只解析配置，**不做任何 I/O、不创建任务**；同时立刻接管
  `_ObservationScopeFilter.observe_all`（该过滤器实例由装饰器创建、被所有事件共享，
  可能被上一个实例留在放宽状态）。
- `initialize()`：构建 transport / 队列 / bridge / outbox，随即启动队列 worker 与 outbox 消费者。
  幂等；配置不可用时直接进入“静默不做事”状态。
- 消息路径上会在需要时惰性调用同一套启动逻辑（同步 `_start()`），因此即使宿主未调用 `initialize()` 也能工作。
- `terminate()`：先**有界优雅停机**（最多 `SHUTDOWN_GRACE_S = 5s`）再取消后台任务并 `await gather`、
  停止队列 worker、取消预热任务、关闭 HTTP 会话；幂等，未启动时调用也安全。
  - 优雅窗口只等**已经在飞的租约**：Runtime 已授权的发送不可逆，中途取消会留下
    「消息可能已经送达、而回报丢失」的局面，Runtime 只能按租约到期重派 → 同一条消息发两遍。
  - 窗口耗尽后，已授权的 `send` 仍会吸收**一次**取消，等投递落地再收尾（第二次取消照常生效，
    停机时间因此始终有界）。
  - **终止是终态**：`terminate()` 之后到达的消息钩子不会再启动任何 worker
    （AstrBot 在重载完成前仍会把事件派给旧实例）。
- 观察范围（`observe_all`）只在适配器**真正运行**时才可能为 `true`：关闭、启动失败（3 次后放弃）、
  已终止都会把它复位为 `false`，否则一条本插件不处理的群消息会被 AstrBot 标记为已唤醒。
- 插件类**刻意不定义 `__del__`**：AstrBot 在插件类自身定义了 `__del__` 时会**跳过** `terminate()`，
  这条约束已由 `tests/test_packaging.py` 固化。

### 7.1 无法从插件侧消除的发送/回报歧义（如实记录）

`send` 的投递与回报是两个动作，中间必然存在窗口：`Context.send_message()` 已经返回（或仍在途中）、
而 `/v1/outbox/{id}/result` 还没被 Runtime 收到时，进程崩溃、被强杀、或优雅窗口耗尽后仍被取消，
都会留下**插件无法自证**的状态 —— 它无法知道平台到底有没有把消息发出去。

- 插件侧能做到的：授权后不主动放弃投递（吸收一次取消）、把已完成的投递结果照实回报、
  同进程内用结果缓存抑制重复执行；优雅窗口超时时打一条 warning。
- 插件侧**做不到**的：跨进程崩溃、强杀，以及「窗口耗尽 + 投递已完成但回报丢失」。此时 Runtime
  只能按租约到期重派，理论上存在重复发送。要彻底消除，需要 Runtime 侧「发送前预登记 + 幂等去重」
  或平台侧可达性回执，超出本适配器的能力范围。
- **这跟 §4.5 的「不回报」是两件事，不要混淆**：那里是「**确定没发出去**」，所以沉默=安全重试；
  这里是「**可能已经发出去**」，所以插件一定回报真实结果（超时/异常也报 `failed`），
  宁可由 Runtime 记一次失败，也不能沉默着让它重派一条可能已经送达的消息。
- 另一处无法从插件侧消除的落差：Runtime 的授权闸门在**租约已过期**时也会回 `authorized=false`
  （`reason` 形如 `lease_expired` / `stale_lease`），而插件按契约把它当明确否决回报 `rejected`，
  Runtime 又会把这条 `rejected` 记成终态。要让这类**簿记性**理由也走「重派」路径，需要 Runtime
  自己区分「否决」与「租约不对」；插件不解析 `reason` 的语义（架构约束：插件不解释语义）。

---

## 8. 安全与凭据

- 仓库内**没有任何 API key 或令牌**；`runtime_token` 默认空串，且可用环境变量
  `COMPANION_RUNTIME_TOKEN` 提供，配置文件里可以不出现密钥。
- 令牌只出现在请求头，日志里只显示 `configured / not configured`；`tests/test_packaging.py`
  会扫描全部文件，命中 `sk-…`、`Bearer <长串>`、`AIza…` 等模式即失败。
- **v0.2：Runtime 侧同样遵守这条规则。** `/health` 的 `semantic_provider` 块只报告
  `api_key: configured` / `not configured`，不回显密钥。插件虽然会在状态命令里查询
  `/health`（§4.8），但**只打印白名单里的名字、`available` 与 `unresolved` 计数**，
  **绝不打印整个响应体**，以免把运维信息变成潜在凭据泄漏面。
- 仅供本机/内网使用；对外暴露 Runtime 时请自行加 TLS 反向代理（本插件支持 `https://`）。

---

## 9. 目录结构

```text
astrbot_plugin_companion_runtime/
├── main.py                      # Star 插件：监听、注入、生命周期（AstrBot 侧）
├── astrbot_executor.py          # render/send 的宿主侧执行（AstrBot 侧，仅用公开 API）
├── metadata.yaml                # 插件元数据（astrbot_version: ">=4.28,<5"）
├── _conf_schema.json            # WebUI 配置 schema
├── requirements.txt             # 空（依赖由 AstrBot 提供）
├── README.md
├── companion_runtime/           # 纯 Python 核心，不 import AstrBot，可独立测试
│   ├── protocol.py              # v1 协议类型、序列化、防御式解析
│   ├── settings.py              # 配置归一化与钳制（含 2s 硬上限）
│   ├── retry_queue.py           # 有界 fail-open 重试队列
│   ├── bridge.py                # 上下文缓存 + 严格超时 + 注入文本包装
│   ├── outbox.py                # 租约消费、render/send 执行与回报
│   ├── http_client.py           # aiohttp 传输实现
│   └── coerce.py                # 配置/协议值的无异常强制转换
└── tests/                       # 全部测试均不需要真实 AstrBot
```

---

## 10. 测试

```bash
cd data/plugins/astrbot_plugin_companion_runtime
python -m pytest tests -q          # 或：python -m unittest discover -s tests -t .
```

覆盖内容（143 项，全部离线）：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_protocol.py` | 协议解析/序列化、非法输入降级、错误截断 |
| `tests/test_settings.py` | 配置默认值（含 Runtime 默认端口 8787）、字符串强制转换、区间钳制、**2s 硬上限**、token 不进 repr、环境变量回退 |
| `tests/test_retry_queue.py` | 去重、写满淘汰最旧、退避重试、超次放弃、超时丢弃、worker 启停 |
| `tests/test_bridge.py` | 缓存命中、**超时放弃**、错误降级、过期缓存兜底、截断、版本号清洗、预热 |
| `tests/test_outbox.py` | render 成功/失败/超时、send 授权通过与拒绝、**授权没问到结论时不留终态且保持可重试**（含 Runtime 重新租约后重试成功）、平台未找到、重复租约重放、续租心跳（含**排队等待期间也续租**、**`extended=false` 停止工作/不回报/不计成功**、瞬时心跳错误不停止工作）、**租约丢失时已开始的投递仍照实回报**、取消不丢已授权投递的回报、`request_stop`/`wait_idle`、轮询退避 |
| `tests/test_plugin_integration.py` | 用 `tests/stubs/astrbot`（模拟 AstrBot 4.28 公开接口）验证 `main.py` 全链路：事件上报、`wake`/`all` 两种监听范围（含**关闭/放弃/终止后不放宽宿主管线**）、`message_type` 映射为 private/group/other、临时 TextPart 注入、send 前授权、**状态命令打印 `semantic_provider` / `semantics` 两行且 `/health` 不可用时仍可用**、启动失败自限、**终止后不复活也不丢已在飞的发送**、初始化/终止清理 |
| `tests/test_packaging.py` | schema 与 Settings 键一致（含 `runtime_base_url` 默认值一致且指向 8787）、元数据字段与版本范围、**无内嵌凭据**、仅 import 白名单内的 AstrBot 模块、纯核心不 import AstrBot、`terminate` 可达 |

> 说明：`tests/stubs/astrbot/` 只是 AstrBot 公开接口的最小替身，用来在没有 AstrBot 的环境里验证插件**接线**；
> 它不代表真实 AstrBot 行为。真实环境仍需在 AstrBot 中做一次加载 + 一次 render/send 联调。
> 当前没有针对真实 provider 的在线测试（那需要真实模型与 Runtime 服务）。

---

## 11. 兼容性与已知限制

- 目标版本：**AstrBot >= 4.28, < 5**（`metadata.yaml` 强制）。仅使用公开/文档化接口：
  `astrbot.api.*`、`astrbot.core.agent.message.TextPart`（文档给出的注入写法）、
  `astrbot.core.star.filter.custom_filter.CustomFilter`（作为 `astrbot.api.event.filter.CustomFilter` 的回退导入）。
- `TextPart.mark_as_temp()` 需要 AstrBot >= 4.24；若该能力缺失，插件**放弃注入**而不是注入会持久化的文本
  （设计 §86.10：隐藏心理上下文不得进入永久对话历史）。
- 传输为 HTTP 轮询，不引入 WebSocket/消息队列依赖，便于弱 VPS 部署；轮询间隔可调。
- 事件上报逐条入队（不批量），以保证弱网下的顺序与幂等语义简单可验证。
- 进程重启后 `_completed` 去重缓存与本地队列会丢失；此时依赖 Runtime 的租约到期与
  `(action_id, attempt_id)` 语义避免重复发送。
- **v0.2 现状**：`/companion_runtime` 状态命令**会**查询 `GET /health` 并打印
  `semantic_provider` 与 `semantics.unresolved` 两行（§4.8）；该探测是 fail-open 的，
  失败时只多一行 `cognition: unavailable`。插件没有任何与生成式模型相关的配置项或探测逻辑
  ——Runtime 默认不接模型（§0.2），这是设计选择，不是遗漏。
- **v0.2 现状（实现侧待修）**：状态命令读取 `semantic_provider["name"]`，而 Runtime 报的字段名是
  `provider`，因此真实 Runtime 上第一行目前显示 `semantic_provider: unknown (...)`（§4.8）。

---

## 12. 状态查询接线：已实现（v0.2）

> 本节在 v0.2 文档同步时曾是「建议但未执行的改动」；**这些改动已经落地**，因此改记为现状说明。
> 保留本节是为了让"曾经只是建议"这件事本身可被追溯。

已经实现的内容：

1. **`main.py::_semantic_status_lines()`**：`_status_text()` 在 `outbox` 行之后调用它，
   用既有 `transport.fetch_health()`（复用 `request_timeout_ms`）可选地查询一次
   `GET /health`，并输出两行：
   - `- semantic_provider: <name> (available=<bool>)`
   - `- semantics: <N> unresolved (normal: the Runtime defers what it cannot settle confidently)`

   探测全程 fail-open：适配器未启动、Runtime 不可达、没有 `/health`、响应不是 JSON 时，
   只输出 `- cognition: unavailable (…)`，其它输出不受影响。只渲染白名单字段，
   从不打印整个响应体（§8）。
2. **`tests/test_plugin_integration.py`**：为上面这条加了断言，覆盖成功路径
   （`semantic_provider: disabled (available=False)` 与 `semantics` 行都会出现）
   以及「Runtime 不提供 `/health` 时状态命令仍然可用」的 fail-open 行为。
3. **README §4.8** 已按现状改写（不再是「已知差距」）。

仍然遗留的一点（**未修，只记录**）：

- 状态命令读的是 `semantic_provider["name"]`，而 Runtime `/health` 报的键是 `provider`，
  于是真实部署下这一行会显示 `unknown`。最小修复是在 `main.py` 里把 `provider.get("name")`
  改成 `provider.get("name") or provider.get("provider")`，并按现状补一条断言。
  本文档不动 `.py`，因此只在此声明。

---

## 13. 版本

- `0.1.0`：首个可用版本。协议 v1；render/send 两种行动；严格短超时注入；有界重试队列；
  租约心跳；`wake`/`all` 两种监听范围；离线测试 121 项。
- `0.1.0`（文档同步，未发版）：README / `metadata.yaml` / `_conf_schema.json` 文案与
  **架构补丁 v0.2** 对齐（两层时间模型、注入块背景语义与优先级、`semantic_provider` 与
  `semantics` 健康字段、`unresolved` 属正常状态）。协议与配置键未变化。
- `0.1.0`（文档同步，未发版）：**本地模型路线已整体放弃**，Runtime 只剩 `disabled`（默认）
  与 `remote_api` 两种语义 provider；插件侧同步删除了所有"本地模型可选"的表述，
  并明确「Runtime 默认不接任何模型，插件也不需要知道 provider 是什么」。
  状态命令查询 `/health` 并打印 `semantic_provider` / `semantics` 两行是**已实现的现状**
  （§4.8 / §12），不再是「建议的改动」。插件行为与协议未变化；离线测试 **124 项**。
