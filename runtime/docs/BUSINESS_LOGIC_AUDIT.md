# 主业务逻辑：三个已复现的缺陷（**三条全部已修**）

> **状态（2026-09-16）**：**A、B、C 三条全部已修**（`29f2328` / `94549a5` / `545ddc8`）。
> 下面每条都标了当前状态；原始诊断文字保留原样，便于对照"当初是怎么判的"。
>
> 结论先行，证据先行。本文件最初是**只诊断不修**，用户随后指示"修吧"。
> 所有数字都能用一条命令重新生成：
>
> ```bash
> cd /home/bomomo/理解痞老板/xiaojiujiu
> runtime/.venv/bin/python scripts/business_logic_probes.py
> ```
>
> 探测脚本 `scripts/business_logic_probes.py`（只打印、不断言、恒退出 0）。

日期：2026-09-16 ｜ 被查版本：`e3f09b3` ｜ 范围：`runtime/src/companion_runtime/` 的决策与学习逻辑

---

## 0. 三个缺陷与它们的严重性

| | 缺陷 | 设计依据 | 用户可见后果 | 默认配置是否可达 | 状态 |
|---|---|---|---|---|---|
| **A** | 回复长度用**绝对阈值** `<= 4` / `>= 20` 判证据强弱与正负；`min(3, turns)` 让对话长度对所有人都在 3 轮饱和 | §29 | 话少的用户被系统性判为冷淡：同样的行为，证据权重 **0.072 vs 0.180**，`positive_probability` **0.619 vs 0.700**；对话长度信息在 3 轮后全部丢失 | **是**（规则路径就会写 `reply_length`） | **已修** `29f2328` |
| **B** | 硬边界用**自己那份 type 集合**判断"是否主动"，可被同义词绕过 | §52 / §86.5 | `repair` 被拦、`apology` 不被拦；`share` 被拦、`emotional_expression` 不被拦；`curious_question` 被拦、`question` 不被拦——**同一个意图换个写法就能绕过"今天别主动联系我"** | 否（provider 路径／`POST /candidates/operations`；打开 v0.2 的 `remote_api` 即生效） | **已修** `94549a5` |
| **C** | 情绪时宜性同样按 type 硬编码，同义词被区别对待 | §7 情绪与行为的匹配 | 同一个意图（`repair` vs `apology`）时宜性 **1.000 vs 0.440**，差 **2.3 倍**，而它直接进候选效用（§45） | 否（同上） | **已修** `545ddc8` |

**B 是这三个里最该先修的**：它是一条**安全约束**（硬边界）被绕过，而不是分寸问题。
`§52` 的原话是"边界是硬约束"、`§86.5` 是"任何压力、冲动、候选收益都不能绕过硬边界"——
现在绕过的成本是换一个同义词。

**A 是唯一在默认配置下就会发生的**，所以它是当前线上行为里真实存在的偏见。

---

## 1. 为什么 ~1100 条测试和四套仿真全绿

这不是"测试写得少"，是**盲区正好在这三处的形状上**：

| 检查 | 数量 | 为什么查不出 A / B / C |
|---|---|---|
| `runtime` 离线测试 | 1096 passed | 每个 type 各测各的：有测试断言 `share` 被边界拦住，也有测试断言 `emotional_expression` 能通过——**两条都是"对的"**，没有一条把它们放在一起比 |
| 用户黑盒仿真 | 77/77 | 只断言用户可见事实（收到几条、内容有没有泄漏），而 A 的效果是"模型内部对用户的评价偏低"，要很多轮才变成行为差异 |
| 韧性仿真 | 335/335 | 测传输与恢复，不测语义 |
| 记忆质量仿真 | 25/25 | 测记忆形成/召回 |
| 关系递进仿真 | 105/105 | 测关系指标的**单调性**（不能倒退），不测"同一意图的两种写法应当等价" |

一句话：**没有任何一条检查要求"同一个意图的两种拼写必须等价"**。B 和 C 的形状正是这个。
A 的形状是"绝对阈值 vs 相对基线"，也没有检查在跨用户比较。

> 这与本项目已经修过的那条（"预测侧与观察侧的行为特征编码不一致"）是**同一族**：
> 当时也是所有测试都绿，问题藏在"两条路径对同一个东西的描述不同"。区别是那次的双方都在
> 编码层，这次的双方在**类型词表**层。

---

## 2. 缺陷 A：回复长度的绝对阈值（§29）— **已修**

> **修法（`29f2328`）**：照抄已有的 `reply_delay_baseline` 路径，加了 **两条** 同类基线：
>
> - `reply_length_baseline`：`log1p(字符数)` 的 EMA 均值 + 方差，`samples >= 3` 才可信。
>   判据从"≤ 4 字符"变成"比**这个用户自己**的基线低一个对数标准差"。
>   基线不可信时**长度不表态**（不加分也不降权）——长度没有像"8 小时"那样可以当先验的
>   绝对参考值，设计 §31 也不允许在无证据时惩罚。
> - `reply_turns_baseline`：`log1p(轮数)` 的 EMA 均值（只要均值，因为这一项是**比值**不是
>   z 分数，存方差是死字段）。冷启动参考值 3.0，**与旧的 `min(3, turns)/3` 逐值相同**，
>   所以既有数据库的判定不变；习惯学到之后饱和点跟着用户走。
> - 两处绝对阈值都替换掉了：`source_weight` 的 `<= 4` 改成由模型传
>   `short_reply_relative=`（模型才有基线），`_target_rewards` 的 `>= 20 / <= 4` 改成
>   `_relative_length_delta`，且**负向只能抵消已得的加分**（与延迟项同一条不变量），
>   不会再把一次真实回复判成负面证据。
> - 持久化沿用 `params_json`，**不动 schema**。
>
> 实测（`scripts/business_logic_probes.py` probe 1）：三行现在完全一致
> （权重 0.180 / positive 0.657 / reply 0.723）。probe 4 显示冷启动曲线与旧公式逐值相同，
> 习惯 30 轮之后 3 轮拿 0.100、30 轮拿 1.000。
>
> 验收：`tests/test_reply_length_baseline.py` 12 条 + `reply_length` 组 8 个变异全部 KILLED。
> 一条既有测试的期望值被更新，原因写在下面 §2.6。

### 设计怎么说

> §29 行为必须相对用户自己的基线判断
> ……所以：回复速度、**回复长度**、对话持续长度，都应该**相对用户自己的历史基线，而不是绝对阈值**。

三个子句里，**回复速度**已经实现了（`reply_delay_baseline`：`log1p` 空间 EMA + 方差，
持久化在 `params_json`，样本不足 3 条时退回绝对参考值）。**回复长度和对话持续长度没有**。

### 代码现状

```python
# user_model.py:381（source_weight，决定这条证据有多重）
if (reaction.reply_length or 0) <= 4:
    return min(config.implicit_weight, config.slow_reply_weight)

# user_model.py:1005（_targets_for，决定这次互动算正面还是负面）
positive += 0.10 if reaction.reply_length >= 20 else (-0.05 if reaction.reply_length <= 4 else 0.0)

# user_model.py:1009（§29 的第三个子句，同样是绝对阈值）
0.2 + 0.6 * (1.0 if reaction.continued_topic else 0.0) + 0.1 * min(3, reaction.turns) / 3.0
```

### 实测

同一个用户行为（每次都回复、继续话题、主动反问、延迟一样），**只改回复长度**：

| 情形 | 长度 | 每条证据权重 | positive 前→后 | reply 前→后 |
|---|---:|---:|---|---|
| 话少但一直这样 | 3 | **0.072** | 0.605 → **0.619** | 0.494 → 0.607 |
| 中等 | 8 | 0.180 | 0.605 → 0.657 | 0.494 → 0.723 |
| 话多 | 30 | 0.180 | 0.605 → **0.700** | 0.494 → 0.723 |

两处偏见叠加：

1. **证据被压低 2.5 倍**（0.072 vs 0.180）——话少的人说什么都更不算数；
2. **正面打分更低**（0.619 vs 0.700），而且在"回复了但没继续话题"的情形会直接扣到 0.5 以下，
   即**一次真实的回复被判成负面证据**。

也就是说：**一个说话一直很简短的用户，会被这个角色长期判定为"不太想理我"**，
而这与用户的实际行为无关。设计 §31 特意写过冷启动不能"没数据 → 永久不主动"，
这里是**有数据但读错了**。

### 影响面

`positive_probability` 会进动机层的用户预期收益（`V_user`，§46），所以它不只是个内部数字：
它会让角色对这类用户**更不愿意主动**。而"话少"与"冷淡"在高语境中文聊天里是两件事。

### 修法方向（未做）

照抄已经存在的那条路径，不要发明新机制：

1. 加 `reply_length_baseline`（`log1p` 空间 EMA + EMA 方差），与 `reply_delay_baseline`
   并列放在同一个 `params_json` 里——**不动 schema**，`ADDED_COLUMNS` 也不用碰；
2. `_relative_length_delta(reaction)`：把 `log1p(reply_length)` 在该用户自己的基线上算 z 分数，
   只映射到一个小幅 nudge（与 `_relative_delay_delta` 同形）；
3. 基线样本不足时**中性**（0 分、不降权），而不是退回"≤4 就是弱证据"——
   长度没有像"8 小时"那样可以当先验的绝对参考值，设计 §31 也不允许在无证据时惩罚；
4. `turns` 同理（第三个子句），或者明确记成"有意只做两/三"。

### 验收方式（修的时候）

- 一条**跨用户等价性**测试：话少但一致的用户 vs 话多但一致的用户，N 轮之后
  `positive_probability` 的差必须小于某个阈值（现在是 0.081），且证据权重相等；
- 一条**基线生效**测试：同一个用户在习惯从 30 字变成 3 字之后，短回复**才开始**降权；
- 变异：把相对判断改回绝对阈值 → 必红；把基线写死成常数 → 必红。

---

## 3. 缺陷 B：硬边界可被同义词 type 绕过（§52 / §86.5）

### 代码现状

```python
# motivation.py:855（硬边界唯一的强制点）
if hard_blocked and is_candidate_proactive(candidate):
    blocked = True
    reason = "boundary_blocks_proactive"

# candidate.py:1300（它问的谓词，自己硬编码了一份集合）
return candidate.type in {
    "contact", "check_in", "follow_up", "curious_question", "share", "repair",
}
```

而 `user_model.py` 里已经有一份权威映射 `TYPE_TO_BEHAVIOUR`，它把 `apology` 归到 `repair`、
`question` 归到 `curious_question`、`emotional_expression` 归到 `emotional_expression`。
谓词没有读它。

### 实测

| type | 行为类 | 类是否主动 | 门是否按主动拦 | 一致 |
|---|---|---:|---:|---|
| `apology` | `repair` | True | **False** | ✗ |
| `emotional_expression` | `emotional_expression` | True | **False** | ✗ |
| `question` | `curious_question` | True | **False** | ✗ |
| `check_in` / `contact` | `proactive_contact` | True | True | ✓ |
| `curious_question` | `curious_question` | True | True | ✓ |
| `follow_up` | `follow_up` | True | True | ✓ |
| `repair` | `repair` | True | True | ✓ |
| `share` | `emotional_expression` | True | True | ✓ |
| `reply` | `reply` | False | False | ✓ |

用户说"今天别主动联系我"之后：

- 被拦住的：`['check_in', 'contact', 'curious_question', 'follow_up', 'repair', 'share']`
- **被放过的：`['apology', 'emotional_expression', 'question']`**

`apology`（去道歉）本身就是最典型的"我应该主动开口但被禁止"的行为，
而它恰好是唯一一个连**行为类**都写着 `repair`、却被门当成"非主动"的拼写。

### 可达性（重要，别夸大）

规则生成器（`candidate.py`）只产出
`{follow_up, curious_question, share, repair, reply, contact}`（分别见 `:348` `:429` `:518`
`:806` `:911` `:1117`），所以这三位**在默认配置下不可达**。它们从两个地方进来：

1. 强语义 provider 写 `type` 的自由度（`parse_deep_refresh` / `candidate_intent` operation），
   即设计 v0.2 的 `semantic.provider = "remote_api"` 路径——**这是 v0.2 的主推配置**；
2. `POST /candidates/operations`（池管理器公开端点，`ADD/UPDATE/RETIRE/REINTERPRET`）。

所以它是**潜伏缺陷**：默认部署看不见，一旦按设计文档把强 API 打开就生效。
这正好是本项目最在意的那种形状——"看起来实现了，直到换一个配置"。

### 修法方向（未做）

1. **别再加一份集合**。让谓词从 `TYPE_TO_BEHAVIOUR` 派生：
   `proactive ⇔ 行为类 != "reply"`（`reply` 是唯一非主动的类）。这样"类"只有一个权威来源。
2. 但要留意改谓词会**同时改变边界门的行为**：那三位会开始被拦——这是修的方向，
   但属于行为变更，需要一条"同义词等价"的验收测试兜住：
   **对每一对同行为类的 type，边界门的裁决必须相同**。
3. 兜底：对**未知 type**（provider 写了新词）当前是"不主动、放行"。设计 §52 的口径
   （硬约束优先）下，更安全的默认是**当成主动拦下**，或者至少拒绝该候选——
   "未知所以就让它过"在安全约束上方向反了。这条要单独决定，别顺手改。

### 验收方式（修的时候）

- 一条**同义词等价性**测试：按 `TYPE_TO_BEHAVIOUR` 分组，同组 type 在硬边界下的裁决必须一致；
- 一条**未知 type** 测试：钉住"未知 type 在硬边界下的裁决"这个决定本身；
- 变异：谓词改回硬编码集合 → 必红；把 `reply` 也算主动 → 必红（`reply` 受 `allow_reply` 管）。

---

## 4. 缺陷 C：情绪时宜性按 type 硬编码 — **已修**

### 代码现状

```python
# runtime.py:2614 / :2616（_emotion_alignment）
if top.direction == "-" and candidate.type in {"repair", "follow_up", "check_in"}:
    return clamp(0.4 + top.intensity)
if top.direction == "+" and candidate.type in {"share", "curious_question", "contact"}:
    return clamp(0.4 + top.intensity)
return clamp(0.2 + 0.3 * top.intensity)     # 其他一律进这条
```

### 实测

| 情形 | 规范写法 | 同义词 | 倍数 |
|---|---:|---:|---:|
| 用户不快时 | `repair` = **1.000** | `apology` = **0.440** | 2.3× |
| 用户开心时 | `share` = **1.000** | `emotional_expression` = **0.440** | 2.3× |
| 用户开心时 | `curious_question` = **1.000** | `question` = **0.440** | 2.3× |

用户不高兴的时候，角色"该不该道歉"这件事，取决于 provider 写的是 `repair` 还是 `apology`。
`_emotion_alignment` 进的是候选效用（§45），所以它会直接改变**角色选哪一个候选**。

同族还有一处：`protocol.py:346` 的并发分类器只认
`{follow_up, curious_question, check_in}`——它决定一个在飞的提案遇到新用户事件时是
APPLY / REBASE / DISCARD（§64）。同样是同义词覆盖不到。

### 修法（`545ddc8`，未做机械替换）

把两份 type 清单换成**按行为类**的两张表，放在 `user_model.py` 的类型词表旁边
（那里已经是 `TYPE_TO_BEHAVIOUR` / `QUESTION_TYPES` / `EMOTIONAL_EXPRESSION_TYPES` 的家）：

```python
MOOD_MATCH_NEGATIVE_CLASSES = {"repair", "follow_up", "proactive_contact"}
MOOD_MATCH_POSITIVE_CLASSES = {"emotional_expression", "curious_question", "proactive_contact"}
```

两点是**判断**而不是机械替换，因此都写进了测试：

1. **`proactive_contact` 同时出现在两张表里。** 旧的两份清单把 `check_in` 放负向、
   `contact` 放正向，而两者是同一个行为类。"主动出现"在用户低落时（关心）和高兴时
   （呼应）都合时宜——这是把旧清单的**并集**如实翻译的结果，不是新加的宽容。
2. **未知 type 拿通用值（0.2 + 0.3·强度），不加成。** 这与 B 的失败关闭方向**相反**，
   而且是刻意的：B 是硬约束（不认识就当主动拦下），C 是分寸（不认识就不该凭空加分）。
   两条测试各自把这两个方向钉住。

`protocol.reconcile` 那处同族缺陷没有新加第四份清单，改为复用已有的 `QUESTION_TYPES`
（它本来就有 `question`，而旧清单漏了）。

### 验收方式（修的时候）

- 同义词等价性测试（同 B 的做法，按行为类分组）；
- 钉住"每个行为类在正/负情绪下的合时宜性"这张表本身（含新增 type 的默认值）。

---

## 5. 共性根因：一个权威映射，四处各自硬编码

`TYPE_TO_BEHAVIOUR`（`user_model.py`）是唯一的"意图类型 → 行为类"映射。
但至少四处**各自维护了一份 type 集合**，而且互相矛盾：

| 位置 | 集合 | 用途 |
|---|---|---|
| `candidate.py:1300` `is_candidate_proactive` | `{contact, check_in, follow_up, curious_question, share, repair}` | 硬边界门（§52） |
| `runtime.py:2614` `_emotion_alignment`（负向） | `{repair, follow_up, check_in}` | 候选效用（§45） |
| `runtime.py:2616` `_emotion_alignment`（正向） | `{share, curious_question, contact}` | 候选效用（§45） |
| `protocol.py:346` 并发分类器 | `{follow_up, curious_question, check_in}` | APPLY/REBASE/DISCARD（§64） |
| `candidate.py:96` `CONTACT_CANDIDATE_TYPES` | `(contact, check_in)` | "只是想联系用户"的永久候选（§43） |
| `user_model.py:QUESTION_TYPES` | `{follow_up, check_in, question, curious_question}` | 行为特征"是否追问"（§22.1） |
| `user_model.py:EMOTIONAL_EXPRESSION_TYPES` | `{share, emotional_expression}` | 行为特征"情绪暴露"（§22.1） |

四份"看起来像业务规则"的集合里，**只有后两份**（本轮 ① 加的）是按行为类对齐过的。
前三份都是独立写下的，所以它们不一致——B 和 C 只是这种不一致里后果最严重的两个。

**这解释了为什么 A/B/C 会同时存在**：项目没有把"意图类型"当成一个有单一权威的维度，
而是当成字符串在各处随手比较。修 B/C 的时候如果只在两处各改一行，下次加 type 还会再犯。
真正的修法是**先定义这一维度的权威语义**（行为类 + 主动/被动 + 情绪方向 + 是否追问），
再让所有消费方读它。

---

## 6. 建议顺序（等用户决定）

| 顺序 | 修什么 | 为什么排这里 | 预计改动面 |
|---|---|---|---|
| 1 | **B**（硬边界绕过） | 唯一一条安全约束；修法明确（谓词派生自行为类）；且能顺带建立"同义词等价"的测试范式 | 1 个谓词 + 1 条测试族 |
| 2 | **A**（长度基线） | 唯一在默认配置下就发生；有现成模式可抄（delay baseline） | 1 组基线 + 2 处替换 + 持久化 |
| 3 | **C**（时宜性表） | 修法明确但**集合本身需要设计确认**（见 §4） | 1 张表 + 1 条测试族 |
| 4 | 收敛 §5 的权威语义 | 防止下次加 type 再犯；也是前三者的收尾 | 跨模块，需要一次设计决定 |

每一行都应当先有"同义词等价"或"跨用户等价"的验收测试，再有实现——
这三个缺陷的共同形状是**没有任何检查要求等价性**，所以先补检查，才能证明修好了。

---

## 7. 未核实 / 边界条件（别当已证）

1. **B/C 在默认配置下不可达**（provider 默认 `disabled`）。我把它们判为"潜伏"而不是
   "线上正在发生"。如果你实际部署时打开了 `remote_api`，那它们就是**正在发生**。
2. **A 的行为差异只在多轮后显现**。单轮看不出；上表的 12 轮已经足够分出 0.081 的差距，
   但我没有跑更长的收敛测试，所以"长期会收敛到哪里"未测。
3. **`positive_probability` 到最终行为的完整链路**我只读到"进 `V_user`"这一步
   （§46 用户预期收益），没有做端到端"角色因此更少开口"的仿真证明。要坐实"用户可见后果"，
   应当补一条长程仿真：话少用户 vs 话多用户，统计主动消息条数。
4. **`reply_length` 的写入方**：我确认了 `_attribute_user_reply` 会写 `len(content)`
   （`runtime.py`），也就是用户消息的**字符数**。中文 3 个字信息量远大于英文 3 个字符，
   所以阈值 `<= 4` 对中文尤其不友好——但这是我的判断，不是测出来的。
5. **未知 type 的兜底方向**（§3 修法第 3 条）我标注为"要单独决定"，没有验证过
   现有 provider 会不会写出未知 type。
6. 本轮**没有改任何代码**。所有结论都来自阅读 + `scripts/business_logic_probes.py` 的实测输出。

### 2.6 修 A 时更新的一条既有测试（必须说明）

`tests/test_user_model_time.py::test_a_relative_slow_reply_is_weaker_evidence_not_negative_evidence`
的期望值从 **0.70 改成 0.60**。原因不是放宽断言，而是那条注释里写的算术包含了一个
**已经删掉的绝对加成**：

```
旧：0.5 + 0.12(继续话题) + 0.08(反问) + 0.10(回复 >= 20 字) = 0.80，延迟项 -0.10 → 0.70
新：0.5 + 0.12 + 0.08 + 0.00(30 字正是该用户的习惯)      = 0.70，延迟项 -0.10 → 0.60
```

该测试的 fixture（`learn_replies`）每次都写 30 个字，所以 30 字**就是**这个用户的常态——
按 §29，它不该再拿"很长"的加成。测试要守的性质（延迟项只能抵消加分、不能把一次回复变成
负面证据）**没有变**，仍在同一条断言里。已在测试内注明原因与出处。

---

## 8. 修复记录

| | 提交 | 内容 | 验收 |
|---|---|---|---|
| **B** | `94549a5` | 谓词改从 `TYPE_TO_BEHAVIOUR` 派生（主动 ⇔ 行为类 ≠ `reply`）；未知 type 失败关闭 | `tests/test_boundary_synonyms.py` 8 条 + 4 变异 |
| **A** | `29f2328` | 长度与对话轮数两条基线；两处绝对阈值替换；负向只能抵消加分 | `tests/test_reply_length_baseline.py` 12 条 + 8 变异 |
| **C** | `545ddc8` | 时宜性按行为类两张表；未知 type 不加成；protocol 复用 `QUESTION_TYPES` | `tests/test_mood_matching_by_class.py` 7 条 + 4 变异 |

全仓变异数从 31 增至 **47，全部 KILLED**。
