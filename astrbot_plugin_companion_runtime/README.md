# astrbot_plugin_companion_runtime

「内源主动型长期陪伴 AI Runtime」的 **宿主侧薄适配插件**。

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

---

## 1. 职责与非职责

| 是本职 | 不是本职（Runtime 负责） |
| --- | --- |
| 传递原始事件、会话标识、消息正文 | 事件评价、语义解释、推断与事实分离 |
| 在超时预算内取回并注入上下文文本 | 决定本轮该注入什么心理状态 |
| 调用 AstrBot 当前 provider 做一次渲染 | 决定说什么、为什么说 |
| 在授权通过后发送消息并回报结果 | 决定何时主动、是否沉默、边界判定 |
| 有界重试与租约心跳 | 状态持久化、单写者 Reducer、版本与 APPLY/REBASE/DISCARD |

插件**不读取、不保存、不转发** AstrBot 的 provider API key：渲染一律通过
`Context.llm_generate()` 走宿主既有配置。插件自身不含任何凭据（见 §7）。

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
| `runtime_base_url` | `http://127.0.0.1:8720` | Runtime HTTP 根地址 |
| `runtime_token` | `""`（secret） | `Authorization: Bearer <token>`；可用环境变量 `COMPANION_RUNTIME_TOKEN` 代替，避免把密钥写进配置文件 |
| `adapter_id` | `default` | 多个 AstrBot 实例接入同一 Runtime 时必须不同 |
| `request_timeout_ms` | `1500` | 普通请求超时 |
| `context_timeout_ms` | `400` | **注入路径硬超时**，范围 50–2000，超出强制截断 |
| `context_cache_ttl_ms` | `30000` | 缓存有效期；命中即零等待 |
| `context_cache_max_sessions` | `64` | 缓存会话数上限（LRU） |
| `context_prefetch` | `true` | 收到消息即后台预热缓存 |
| `observe_mode` | `wake` | `wake` / `all`，见下 |
| `report_assistant_messages` | `true` | 上报机器人实际发出的纯文本 |
| `inject_enabled` | `true` | 关则只上报不注入 |
| `inject_max_chars` | `2000` | 注入文本长度上限，`0` 为不限 |
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

### `observe_mode` 请务必确认后再改

- `wake`（默认，推荐）：只上报 **AstrBot 本身就会处理** 的消息（私聊、被 @、唤醒词、回复机器人）。
  实现方式是自定义过滤器判定 `event.is_at_or_wake_command`，该字段只由真实的唤醒条件置位，
  因此本插件**永远不会**把一条普通群消息变成唤醒事件；handler 内部还会再校验一次同样的条件
  （双保险：即使宿主的过滤器语义变化，`wake` 模式下也不会上报非唤醒消息）。
  另外注意：AstrBot 只有在 `is_at_or_wake_command=True` 时才会调用 LLM，因此即便过滤器完全失效，
  最坏后果也只是这类消息继续流经后续管线阶段，而不会让机器人回复它们。
- `all`：上报全部消息。代价是：AstrBot 会把这类消息标记为已唤醒（`is_wake=True`）并让它们继续
  流经限流、内容安全等后续管线阶段。这会改变宿主行为，仅在你清楚后果时开启。

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
{"context": {"text": "【当前心理状态】\n克制，想联系…", "version": "182", "ttl_ms": 30000}}
```

```json
{"context": {"version": "182", "sections": {"当前心理状态": "…", "当前最终意图": "…",
  "必要记忆": "…", "表达边界": "…"}}}
```

- **时延即体验**：本接口位于 `on_llm_request` 内，硬上限 2s、默认 400ms；超时即放弃注入（不报错、不改写请求）。
- 插件只做包装（`<companion_runtime_context version="…">…</companion_runtime_context>`）与长度截断，
  不解释语义；`sections` 会被拼成 `【段落】` 形式。
- 空响应 / 204 / 空对象：视为“本轮无上下文”，不注入。
- `trigger` 为 `llm_request`（前台请求路径）或 `message`（后台预热）。预热失败无副作用。

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
- **超时、网络错误、响应不可解析一律视为拒绝**（fail-closed）：宁可这条主动消息不发，
  也不能在 Runtime 未知的情况下替它说话。这是本插件唯一 fail-closed 的地方。

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
| `failed` | 执行失败（`error` 为单行原因，超时/异常/平台未找到） |
| `rejected` | 未获授权，未发送（`error` 含原因，如 `aborted_by_user_message`） |
| `skipped` | 无法执行（`missing_session`、`unsupported_action_type:…`） |

> 行动 id 总是出现在请求路径中；仅结果回报的请求体额外附带 `action_id`，便于 Runtime 实现直接对齐。

- 设计 §69：`committed != sent`。只有 `status=ok` 且 `result.sent=true` 才算真正发出。
- 回报是幂等的；失败时进入本地有界重试队列，最终由租约到期兜底。
- 建议 Runtime 对 `rejected` 与 `skipped` 也落一条 `action_attempt` 终态，避免反复派发。

---

## 5. 失败策略与并发行为

**fail-open（不影响宿主）**
- 事件上报、上下文获取、结果回报：任何异常都被吞掉（仅 debug 日志），AstrBot 照常回复用户。
- 上下文获取超时/失败：优先使用过期缓存，其次放弃注入；**绝不阻塞或改写请求**。
- 渲染失败/超时：以 `failed` 回报，不影响前台对话。

**fail-closed（不替 Runtime 说话）**
- 只在 `send` 路径：无有效租约、授权请求失败、授权被拒 → 不发送。

**有界与去重**
- 本地重试队列有界（默认 256），写满丢弃**最旧**条目；单条最多 6 次、最长 10 分钟，之后丢弃并计数。
- 同一 `(action_id, attempt_id)` 重复租约：重放已存结果，不二次渲染/发送（进程内最多记忆 256 条）。
- 同一 attempt 正在执行时收到重复租约：直接忽略。
- 队列与租约统计可通过 `/companion_runtime` 查看。

---

## 6. 生命周期

- `__init__`：只解析配置，**不做任何 I/O、不创建任务**。
- `initialize()`：构建 transport / 队列 / bridge / outbox，随即启动队列 worker 与 outbox 消费者。
  幂等；配置不可用时直接进入“静默不做事”状态。
- 消息路径上会在需要时惰性调用同一套启动逻辑（同步 `_start()`），因此即使宿主未调用 `initialize()` 也能工作。
- `terminate()`：取消全部后台任务并 `await gather`、停止队列 worker、取消预热任务、关闭 HTTP 会话；
  幂等，未启动时调用也安全。
- 插件类**刻意不定义 `__del__`**：AstrBot 在插件类自身定义了 `__del__` 时会**跳过** `terminate()`，
  这条约束已由 `tests/test_packaging.py` 固化。

---

## 7. 安全与凭据

- 仓库内**没有任何 API key 或令牌**；`runtime_token` 默认空串，且可用环境变量
  `COMPANION_RUNTIME_TOKEN` 提供，配置文件里可以不出现密钥。
- 令牌只出现在请求头，日志里只显示 `configured / not configured`；`tests/test_packaging.py`
  会扫描全部文件，命中 `sk-…`、`Bearer <长串>`、`AIza…` 等模式即失败。
- 仅供本机/内网使用；对外暴露 Runtime 时请自行加 TLS 反向代理（本插件支持 `https://`）。

---

## 8. 目录结构

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

## 9. 测试

```bash
cd data/plugins/astrbot_plugin_companion_runtime
python -m pytest tests -q          # 或：python -m unittest discover -s tests -t .
```

覆盖内容（121 项，全部离线）：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_protocol.py` | 协议解析/序列化、非法输入降级、错误截断 |
| `tests/test_settings.py` | 配置默认值、字符串强制转换、区间钳制、**2s 硬上限**、token 不进 repr、环境变量回退 |
| `tests/test_retry_queue.py` | 去重、写满淘汰最旧、退避重试、超次放弃、超时丢弃、worker 启停 |
| `tests/test_bridge.py` | 缓存命中、**超时放弃**、错误降级、过期缓存兜底、截断、版本号清洗、预热 |
| `tests/test_outbox.py` | render 成功/失败/超时、send 授权通过与拒绝、**授权失败 fail-closed**、平台未找到、重复租约重放、续租心跳、轮询退避 |
| `tests/test_plugin_integration.py` | 用 `tests/stubs/astrbot`（模拟 AstrBot 4.28 公开接口）验证 `main.py` 全链路：事件上报、`wake`/`all` 两种监听范围、临时 TextPart 注入、send 前授权、启动失败自限、初始化/终止清理 |
| `tests/test_packaging.py` | schema 与 Settings 键一致、元数据字段与版本范围、**无内嵌凭据**、仅 import 白名单内的 AstrBot 模块、纯核心不 import AstrBot、`terminate` 可达 |

> 说明：`tests/stubs/astrbot/` 只是 AstrBot 公开接口的最小替身，用来在没有 AstrBot 的环境里验证插件**接线**；
> 它不代表真实 AstrBot 行为。真实环境仍需在 AstrBot 中做一次加载 + 一次 render/send 联调。
> 当前没有针对真实 provider 的在线测试（那需要真实模型与 Runtime 服务）。

---

## 10. 兼容性与已知限制

- 目标版本：**AstrBot >= 4.28, < 5**（`metadata.yaml` 强制）。仅使用公开/文档化接口：
  `astrbot.api.*`、`astrbot.core.agent.message.TextPart`（文档给出的注入写法）、
  `astrbot.core.star.filter.custom_filter.CustomFilter`（作为 `astrbot.api.event.filter.CustomFilter` 的回退导入）。
- `TextPart.mark_as_temp()` 需要 AstrBot >= 4.24；若该能力缺失，插件**放弃注入**而不是注入会持久化的文本
  （设计 §86.10：隐藏心理上下文不得进入永久对话历史）。
- 传输为 HTTP 轮询，不引入 WebSocket/消息队列依赖，便于弱 VPS 部署；轮询间隔可调。
- 事件上报逐条入队（不批量），以保证弱网下的顺序与幂等语义简单可验证。
- 进程重启后 `_completed` 去重缓存与本地队列会丢失；此时依赖 Runtime 的租约到期与
  `(action_id, attempt_id)` 语义避免重复发送。

---

## 11. 版本

- `0.1.0`：首个可用版本。协议 v1；render/send 两种行动；严格短超时注入；有界重试队列；
  租约心跳；`wake`/`all` 两种监听范围；离线测试 121 项。
