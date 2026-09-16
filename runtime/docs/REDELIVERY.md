# 崩溃窗口：`committed != sent` 与"平台已发出 / 回执未上报"

> 设计依据：§69（`committed` 不等于 `sent`）、§86.9（行动尝试必须有完整状态机）、
> §63/§64（结果回来时先判依赖，不直接 UPDATE）。

## 1. 窗口在哪

```text
Runtime 决定要联系   →  attempt = committed
渲染完成             →  attempt = ready_to_send，outbox 里出现一行 send
host 领取（租约）    →  outbox.status = leased，attempts += 1
平台真的发出去了     ←  ★ 只有 host 知道这件事发生了
host 上报结果        →  Runtime 才把 attempt 标成 sent
```

`★` 与下一行之间，host 可以死。Runtime 手上留下的是：**一行 `leased` 的 send，
attempt 停在 `ready_to_send`**。它不知道消息到底有没有进用户的聊天框——这是信息论上的
缺口，不是实现疏忽：那个事实只存在于 host 和平台之间。

## 2. Runtime 现在怎么做：至少一次

租约过期后 `outbox.reclaim_expired`：

| 条件 | 结果 |
|---|---|
| `attempts < outbox.max_attempts`（默认 3） | 回到 `pending`，`available_at = now` |
| `attempts >= max_attempts` | 标成 `failed`，随后 `close_settled_outbox_attempts` 终结 attempt |

所以崩溃窗口的正常后果是**重发**：消息最多被平台发出 `max_attempts` 次。实测（`lease_seconds=45`）：

```text
第1次领取: row_attempts=1   过期后 status=pending attempts=1
第2次领取: row_attempts=2   过期后 status=pending attempts=2
第3次领取: row_attempts=3   过期后 status=failed  attempts=3
attempt 最终: failed
```

**这是刻意的选择，不是缺陷。** 反过来做（领过就不再发）是至多一次，代价是"host 领取后立刻
崩溃、消息根本没发出去"时，这条意图被静默丢掉——而 Runtime 无法区分这两种崩溃。宁可重复
也不丢，是这里选的方向。

## 3. host 能看到的（v0.2 起）

`POST /v1/outbox/lease` 返回的每个 item 现在带两个字段：

| 字段 | 含义 |
|---|---|
| `attempts` | 这一行被领取过几次（含本次） |
| `redelivery` | `attempts > 1`，即**这一行可能已经发出去过** |

```json
{
  "action_id": "obx_…",
  "action_type": "send",
  "lease_id": "host:obx_…:2",
  "attempts": 2,
  "redelivery": true,
  "payload": { "text": "在忙吗", "attempt_id": "att_…" }
}
```

`redelivery` 只是 `attempts` 的读法，不是独立判断；它读的是**领取计数**，所以 render 行上
是同一个口径。

在此之前，**v1 协议**（真实 AstrBot 适配器走的那条）里没有这两个信息：`attempts` 只被编进
`lease_id` 的第三段（`{adapter}:{outbox_id}:{attempts}`），那是**防旧租约续期的失效令牌**，
不是给 host 读的信号；`redelivery` 这个名字哪里都没有。结果是 host 连"我正在被重发"都看不出来，
也就无法实现任何策略。

> 精确一点：**旧版** `POST /outbox/claim` 回的是完整的 `OutboxItem.to_dict()`，里面一直有
> `attempts` / `max_attempts`。所以缺口不是"Runtime 从不暴露计数"，而是"**适配器实际调用的 v1
> 协议**没有暴露"，而且两处都没有把"这是重发"这件事说出来。改的是 v1，旧端点不动。

## 4. host 可以怎么用

### 4.1 至少一次（现状，默认建议）

什么都不用做。接受最多 `max_attempts` 次重复，重复的那几条里只有第一条真的到用户手里
（其余会被平台以同样内容再发一次）。

想降低观感损失：把 `redelivery: true` 的 send 记录到日志，运维能对上"用户为什么收到两条"。

### 4.2 至多一次（需要 host 侧持久化）

只有 host 有持久记录时才能安全地做——**必须**在"平台发送成功"和"上报成功"之间把
`action_id` 先写盘：

```text
领取 send（attempts=n）
  ├─ action_id 已在本地"已发出"表里 → 不上报发送，直接上报一个明确的重复结果
  └─ 否则：
       1. 本地落盘：action_id → sending
       2. 平台发送
       3. 本地落盘：action_id → sent
       4. 上报 /v1/action/result
```

顺序不能换：先发送再落盘，等于把窗口移了个位置。

`redelivery` 让第 1 步成为可能；没有它，host 甚至不知道自己该去查表。

**注意**：插件当前的重试队列是**内存**的（`main.py::_report_action` 失败时
`queue.put(...)`，进程一死就没了），所以它现在只能做至少一次。要做至多一次，需要把它换成
落盘队列或落盘的"已发出"表——那是插件侧的独立工作。

## 5. 另外两条实测发现（同一窗口的间接后果，尚未改）

### 5.1 耗尽后 attempt 落成 `failed`，用户**真的回复了也不被归属**

```text
attempt: failed        candidate: active
用户发言后 observations: 0 -> 0        该 attempt 有 observation: False
```

原因：回复归属只看最新的 `sent` attempt（`_newest_sent_attempt`），而这条已经是 `failed`；
`_record_absent_replies` 的静默清扫也只扫 `sent`，所以它连"被无视"都不会被记。

**后果**：一条**真的送达、用户也真的回了**的消息，对用户模型的教学量是 0。
方向没定：把它当"未知"去归属，会在消息其实没送达时把用户的一句普通发言误记成回复；
维持现状，会丢掉一次真实互动。这是产品判断，**没有动代码**。

### 5.2 attempt 终结了，candidate 仍然 `active`

`close_settled_outbox_attempts` 的 docstring 把"candidate stays active"列为**它要修的泄漏后果**之一，
但实测终结 attempt 之后 candidate 仍是 `active`。它会在之后某一轮被重新选中 → 同一件事被再说
一次（这一次不是传输层重发，是意图层重来）。

同样两种方向都说得通（重试意图 vs 退役这条提议），**没有动代码**。

## 6. 怎么复现

```bash
cd runtime
.venv/bin/python -m pytest tests/test_redelivery_visibility.py      # 4 条
# 完整的两条后果（重发序列 + 归属缺失）见 HANDOFF 的"崩溃窗口"一节
```

变异证据：`scripts/mutation_design_conformance.py` 的 `redelivery` 组（见该文件）。
