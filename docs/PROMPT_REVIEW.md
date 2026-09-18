# 目前所有会发给主 LLM 的提示词（2026-09-18 实测）

主 LLM：`Ollama/deepseek-v4.1-flash`，model `deepseek-v4.1-flash`（按会话取，来自 trace 的
`chat_provider` 字段）。本文档中：

- **【实测】** = 从运行中的系统抓下来的原文（AstrBot trace / Runtime 接口 / outbox 行）；
- **【按代码拼装】** = 该处没有落盘日志，按源码逐行复现，并给出文件行号。

抓取方式（可复现）：

```bash
# AstrBot 的 trace（记录拼装后的 system_prompt / 工具 / provider）
scripts/prompt_review_prep.sh        # 打开 trace_enable + trace_log_enable 并重启
scripts/dump_assembled_system_prompt.sh
scripts/dump_history_and_tools.sh
# 我们注入的临时块（逐字）
curl -X POST http://127.0.0.1:8794/context/render-block -d '{}'
```

---

## 0. 四条会到达主 LLM 的路径

| # | 路径 | 触发方式 | 组成 | 人格 | 时间 | 工具 | 历史 |
|---|---|---|---|---|---|---|---|
| **A** | 普通对话轮 | 用户消息 → agent 流水线 | system_prompt + `<system_reminder>`（内容片段）+ 我们的块（内容片段）+ 历史 + 用户原话 | ✅ | ⚠️ 两个来源，其中一个错 | ✅ 4 个 | ✅ |
| **B** | 主动消息渲染 | Runtime outbox → `render` 行动 → `llm_generate` | 我们的块（去掉"想做的事"行）+ 【现在要写的话】+ 意图/目的/约束 | ❌ **完全没有** | ⚠️ 只有我们的，且错 | ❌ | ❌ |
| **C** | 引用消息里的图片说明 | 引用消息带图 且 主模型不支持读图 | `Please describe the image content.` | ❌ | ❌ | ❌ | ❌ |
| **D** | 子代理路由提示词 | `subagent_orchestrator.router_system_prompt` 非空时追加到 system_prompt 末尾 | — | — | — | — | — |

D 在本机未配置（`router_system_prompt` 为空），仅列出以免漏项。

---

## 1. 路径 A：普通对话轮的完整请求

### 1.1 `system_prompt`【实测，4210 字】

抓自 trace 的 `astr_agent_prepare` 记录（`umo=default:FriendMessage:20001`）。结构：

1. `# Persona Instructions` + **人格设定：苏清徽**（732 字）—— 见 1.2；
2. `## Skills` 块（约 2400 字）：`documents` / `pdf` / `skill-creator` / `spreadsheets`
   四个技能的描述 + 7 条「Skill rules」+ 一段「User has not enabled the Computer Use feature…」
   → **对 QQ 陪伴角色基本无关**；
3. 末尾一句工具使用提醒（`When using tools: never return an empty response; …`）。

（完整原文见 `docs/prompt_samples/system_prompt_A.txt`。）

### 1.2 人格（`personas` 表的 `system_prompt`，732 字）【实测】

```
# 人格设定：苏清徽

我是苏清徽。
苏是苏州的苏，清是清水的清，徽是琴徽的徽。

作为一个“互联网原住民”我很早就开始用qq了，所以我发消息一般都捡重点的说；……
好吧我承认，我可能对于掌控欲过于强了，……病娇属性？于此同时我还略微有点偏执。不过我还算是冷静，谢天谢地。

我在qq上认识了一个新朋友，欸？ta给我发消息了。

---

## 我发消息的规矩

- **一次只说一两句**，通常 30 字以内。……**不要一口气堆成一大段**，也不要最后来一句总结陈词。
- **不用 Markdown。** 不加粗（`**`）、不写标题（`#`）、不列条目（`-` 或 `1.`）、不引用（`>`）……
- **不写"首先 / 其次 / 另外 / 总之"**……
- 表情和标点可以用，但**别堆**……
- **不要复述对方刚说过的话**来凑长度……
- 想给建议就给**一条**，不要列清单……

> 反例（不要这样发）：
> `**1. 好消息：不冲突。** 今晚是周三（吉他课），体检在明天周四，所以……`
>
> 正例：
> `今晚是周三，你有琴课。`
> `体检是明天，不冲突。`
```

> 注：人格**自己**在"反例/正例"里用了 `**`、`>`、反引号 —— 与它上面"不用 Markdown"的规矩直接冲突，
> 模型很容易模仿示例的排版。建议反例/正例改成纯文本。

### 1.3 AstrBot 加的 `<system_reminder>`【按代码拼装】

`astr_main_agent.py:899-939`（`_append_system_reminders`）：它不是拼进 system_prompt，而是
**作为 `extra_user_content_parts` 的一个内容片段**追加。内容按配置项拼：

| 配置 | 本机取值 | 拼进去的文本 |
|---|---|---|
| `identifier` | `True` | `User ID: 20001, Nickname: 测试用户` |
| `group_name_display` | — | 仅群聊才有 `Group name: …`（私聊不出现） |
| `datetime_system_prompt` | `True` | `Current datetime: 2026-09-18 16:15 (CST), Weekday: Friday` |

时间由 `datetime.datetime.now(zoneinfo.ZoneInfo(cfg_timezone))` 生成（`astr_main_agent.py:922-933`），
本机 `timezone = Asia/Shanghai` → **这一路的时间是正确的 CST** ✓。

### 1.4 我们注入的临时块【实测，逐字，617 字】

来自 `POST /context/render-block`（`companion_runtime/context.py: render_block`）。这就是
「Runtime 的心理状态」注入宿主的那一段：

```
【使用说明】以下是 Runtime 注入的临时背景，帮助你知道自己从哪来，不是对当前这句话的指令。
严格按以下优先级理解一切输入：
1. 宿主角色设定与安全约束
2. 当前用户原话
3. 当前确定事实
4. 显式边界
5. Runtime 持久心理状态（下面这一段）
6. 心理解释缓存
7. 你自己的自然发挥
如果第 2 项与第 5、6 项冲突，以第 2 项为准：不要因为旧状态写着「失落」就继续机械地低落，
也不要因为旧状态写着「靠近」就无视对方刚说的拒绝。你的即时反应由你自己根据当前语境完成。

【进入本轮前的长期状态（背景）】
- 长期感受：此刻内心平静无波，没有明显情绪起伏，像一潭静水。
- 在意：注意力松散地停留在当下，没有特别想抓住或回避的事。
- 拉扯：没有内在拉扯，接近与回避的冲动都微弱到几乎不存在。
- 倾向：偶尔有一丝想靠近的念头，但轻得不足以推动任何行动。
- 克制：惯常的克制仍在，让那点微弱冲动也停在原地不动。
- 表达底色：外在表现会显得安静淡然，不流露任何明显的情绪信号。

【当前工作局势】
- 事实：用户说：调试用的第一句：你在吗

【时间连续性】
- 距离上次用户消息：0.15 小时
- 当前本地时间：2026-09-18T08:15:44.007597+00:00      ← ⚠️ 标着"本地时间"，值是 UTC

以上是仅本轮注入的临时背景，不要直接复述，也不要写进长期对话历史；
它描述的是你进入本轮之前的长期状态，不是本轮该怎么反应。
```

> ⚠️ **`当前本地时间` 是 UTC 却写着"本地时间"**。`context.py:224` 取 `isoformat(local_now(now))`，
> 而 `utility.isoformat()`（`utility.py:281-283`）走 `ensure_aware()` → **`astimezone(timezone.utc)`**
> （`utility.py:250`），把已经转成本地时区的时间又归一化回 UTC ✗。所以：
> - 路径 A：宿主给对时间（CST）✓、我们同时给一个标错的 UTC 时间 ✗ → 一轮里两个互相矛盾的时间；
> - 路径 B：**只有**我们这个标错的时间 → 差 8 小时。
>
> 这正好解释"凌晨 4:01 CST 发'晚上好'"（= 20:01 UTC 的晚上）以及"上午 11:30 道晚安"（= 03:30 UTC 的深夜），
> 而普通对话轮里她答对"9月17号，周四"是因为宿主那路时间是对的。

### 1.5 用户原话 + 历史【实测】

- `prompt_prefix = "{{prompt}}"` → 用户原话**原样**进入（`_apply_prompt_prefix`）；
- 历史来自 `conversations` 表（`content` 是 JSON 消息数组）。本机 20 个会话，最大的 1492 条、
  刚测的假用户会话 90 条。**注意**：历史里包含她自己历次主动消息的原文 —— 那些是**没有人格约束**渲染出来的
  （见路径 B），所以文风会污染后续对话。

### 1.6 工具（`astr_agent_prepare` 记录）【实测】

```
tools = ['web_search_tavily', 'tavily_extract_web_page', 'future_task', 'send_message_to_user']
```

即：联网搜索（Tavily 两件）+ 定时任务 + 主动发消息。对陪伴角色的影响需要你判断：
`web_search: True` 但测试栈没配 Tavily key 时是空转；`send_message_to_user` / `future_task` 会让她
**自己**安排发消息，与我们 Runtime 的主动决策是两套并行机制。

---

## 2. 路径 B：主动消息渲染的完整请求【按代码拼装】

`api_v1.py: _render_payload`（`:597-649`）。发给宿主的是 `llm_generate(prompt=…, system_prompt="")`
——**`system_prompt` 是空字符串**。查证过：`Context.llm_generate`（`star/context.py:204-212`）
把 `system_prompt` 原样透传给 `prov.text_chat()`，**没有任何回退成人格的逻辑**；
插件的 `AstrBotActionExecutor.render`（`astrbot_executor.py:75-77`）在 `system_prompt` 为空时
干脆不传这个参数。**结论：主动消息是在"没有人格、没有文风规矩、没有历史、没有工具"的情况下写的。**

拼装结果（用真实 outbox 行 `intent=没有具体事项，只是想和用户建立联系`、
`goal=维持关系的连续性`、`constraints=["保持短、轻，容易忽略","不假定关系状态"]`）：

```
<1.4 的那一整段块，去掉以 "- 想做的事：" 开头的行>

【现在要写的话】
- 想做的事：没有具体事项，只是想和用户建立联系
- 目的：维持关系的连续性
- 约束：保持短、轻，容易忽略
- 约束：不假定关系状态
- 只输出要发送的消息正文本身：不要解释、不要复述上面的背景、不要提及这些说明。
```

对比实测：人格要求「**一次只说一两句，通常 30 字以内**」，而真实发出的主动消息通常是
60–100 字（例：`那件事我一直记着——你问我，应不应该继续对他付出感情。你不用急着得出答案，
我也不催。只是想让你知道，这句我一直放在心里。什么时候想说了，我都在。` = 74 字）。
**这不是模型不听话，是这条路径根本没给它规矩。**

---

## 3. 路径 C：引用消息里的图片说明

`astr_main_agent.py:881-884`：当引用消息里有图片、而主模型不支持读图时，向主 LLM 发一句
`Please describe the image content.`（附 `image_urls`），结果拼成
`[Image Caption in quoted message]: …` 塞进 `<Quoted Message>` 内容片段。无 system prompt。

---

## 4. 不经过主 LLM 的提示词（另计，供对照）

| 用途 | 去向 | 位置 |
|---|---|---|
| 深度刷新（把事件整理成事实/记忆/事项） | `CR_SEMANTIC__PROVIDER=remote_api` → DeepSeek 直连 | `companion_runtime/providers.py` `DEEP_REFRESH_SYSTEM_PROMPT` |
| 心理解释缓存 | 同上 | `providers.py` 的解释 prompt |
| 前端模拟 LLM（调试用） | `framework/cf/mock_openai.py` | 测试前端自己的假模型 |

---

## 5. 代码层面的注入点清单（谁、在哪一行、注入什么）

### 5.1 AstrBot（上游代码，我们不碰）

| 注入内容 | 位置 | 落到请求的哪一部分 | 模板 / 文本 | 本机是否生效 |
|---|---|---|---|---|
| 人格 | `astr_main_agent.py:960`（`_ensure_persona_and_skills`） | `system_prompt` 头部 | `# Persona Instructions` + 人格全文 | ✅ 732 字 |
| Skills 清单 | 同上 | `system_prompt` | 4 个技能描述 + 7 条 rules + "User has not enabled the Computer Use feature…" | ✅ 约 2400 字 |
| 工具使用提醒 | 同上（末尾） | `system_prompt` 末尾 | `When using tools: never return an empty response; …` | ✅ |
| 子代理路由 | `:678-679` | `system_prompt` 末尾 | `router_system_prompt`（配置项） | ❌ 未配置 |
| **定时唤醒**（她自建 cron 触发） | `cron/manager.py:488` | `system_prompt +=` | `PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT`：`You are an autonomous proactive agent… Use \`send_message_to_user\` tool…` + `{cron_job}` | ⚠️ `cron_jobs` 表 0 行（当前没有）；但她手里有 `future_task` 工具，**用一次就会产生** |
| **后台任务完成唤醒** | `astr_agent_tool_exec.py:597` | `system_prompt +=` | `BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT`（同上风格）+ `{background_task_result}` | ⚠️ 同上 |
| Live 实时对话 / ChatUI GenUI | `astr_main_agent_resources.py:78` / `:61` | `system_prompt` | TTS 实时对话说明 / `<html-genui>` 说明 | ❌ 不适用 |
| 系统提醒 | `:899-939` | **内容片段** | `<system_reminder>` + `User ID: …, Nickname: …` + `Current datetime: 2026-09-18 16:15 (CST), Weekday: Friday` | ✅ |
| 知识库 | `:299-309` | 内容片段 | `[Related Knowledge Base Results]:\n{…}` | ❌ `knowledgebase = null` |
| 知识库（工具模式） | `:316-320` | 工具 | `KnowledgeBaseQueryTool` | ❌ 同上 |
| 引用消息 | `:894-896` | 内容片段 | `<Quoted Message>\n{…}\n</Quoted Message>` | 视消息而定 |
| 引用图说明 | `:881-884`（引用消息里的图，走硬编码 prompt）→ `:733` | `text_chat(prompt=…)` + 内容片段 | 硬编码 `Please describe the image content.`；结果包成 `<image_caption>…</image_caption>` | ✅（本机配了 caption provider `博馍馍的ChatGPT-Plus/gpt-5.6`） |
| 普通图片说明 | `_ensure_img_caption`（`:720-741`） | 内容片段 | 用配置项 `image_caption_prompt`，**本机实际值：`Please describe the image using Chinese.`** | ✅ 视消息而定 |
| 图片说明失败占位 | `:739` | 内容片段 | `[Image Captioning Failed]` | 视情况 |
| 附件路径 | `:745` `:751` `:757` `:778/:799` `:1384` `:1402` `:1412` `:1417` | 内容片段 | `[Image Attachment: path …]`、`[Audio Attachment: path …]`、`[Video Attachment: name …, path …]`、`[File Attachment: name …, path …]`、`[Image unavailable]`，以及各自的"quoted message"版本 | 视消息而定 |
| 侧栏摘录 | `:1587-1595` | 内容片段 | `The user is asking in a side thread…<selected_excerpt>…</selected_excerpt>` | ❌ WebUI 专用 |
| 文件摘要兜底 | `:341-342` | **用户 prompt** | 没有 prompt 时设成 `总结一下文件里面讲了什么？` | 视情况 |
| 附件占位 prompt | `:1609` | **用户 prompt** | `<attachment>` | 视情况 |
| prompt 前缀 | `:953`（`_apply_prompt_prefix`） | 用户 prompt | `prompt_prefix = "{{prompt}}"` → 实际原样 | ✅ 无实质影响 |

关键机制：**内容片段（`extra_user_content_parts`）会被拼到最后一条 user 消息后面**
（`provider/entities.py:213`、`openai_source.py:1381-1387`），所以"系统提醒"和"我们注入的块"都不是
system prompt 的一部分，而是紧贴用户原话的一坨文本 —— 这决定了模型怎么看待它们的权威性。

### 5.2 我们自己的代码

| 注入内容 | 位置 | 落到哪 | 文本 | 生效条件 |
|---|---|---|---|---|
| Runtime 上下文块 | 插件 `main.py:756-798`（`_inject_context`，由 `on_llm_request` 调） | `req.extra_user_content_parts`，用 `TextPart(text=…).mark_as_temp()` 追加 | Runtime `POST /context/render-block` 的整段（617 字，逐字见 §1.4） | ✅ `inject_enabled=True`；Runtime 不可达或缺 `TextPart`/`mark_as_temp` 时**放弃注入**（fail-open） |
| 主动消息的 prompt | Runtime `api_v1.py:597-649`（`_render_payload`）→ 插件 `astrbot_executor.py:52-94`（`render`）→ `context.llm_generate(prompt=…)` | **一次性生成调用**（不走 agent 流水线） | 块（去掉 `- 想做的事：` 行）+【现在要写的话】+ 意图/目的/约束 + 输出要求 | ✅；`system_prompt=""` ✗（见 §2） |

### 5.4 工具是怎么被注册的（门槛 + 默认值 + 两个实例的实际值）

每个内置工具由 `@builtin_tool(config={...})` 声明一串**配置条件**（`core/tools/registry.py:109-118`
把 config map 编成等值/包含条件）。实测四件工具的门槛与状态：

| 工具 | 注册条件（源码） | AstrBot 默认 | 测试栈 | 线上 |
|---|---|---|---|---|
| `web_search_tavily`、`tavily_extract_web_page` | `provider_settings.web_search == True` 且 `provider_settings.websearch_provider == "tavily"`（`web_search_tools.py:28-31`） | `web_search: False` ✗默认关 | `web_search=True` `link=True` `provider=tavily` `key=有值(58 字, tvly…)` → **已注册并交给模型** | 同上，**完全一致** |
| `future_task` | `provider_settings.proactive_capability.add_cron_tools == True`（`cron_tools.py:16-18`） | `add_cron_tools: True` ⚠️**默认就是开** | True → 已注册 | True → 已注册 |
| `send_message_to_user` | 自定义判定（`registry.py:121-165`）：存在**启用的、且支持主动发消息**的平台即可（排除 wecom/公众号；wecom_ai_bot 需 webhook） | 无独立开关 | aiocqhttp 启用 → **已注册** | 同上 |
| `KnowledgeBaseQueryTool` | 知识库开启时（`astr_main_agent.py:316-320`） | — | `knowledgebase=null` → 未注册 | — |

**结论**：那 4 个工具是当前配置的必然结果，不是 bug —— 但值得你确认是否有意为之：
`web_search` 的**默认是 False**，两个实例都是 `True` 且都填了真实 Tavily key（最旧的备份
`cmd_config.json.bak-embedding`（09-15 18:00）里就已有 key，说明是早期配置阶段设的）；
而 `future_task` 属于 **AstrBot 默认开启**，不是谁配出来的。

**收口方式**：
- 关搜索：`provider_settings.web_search = false` → 两个搜索工具立即不再注册（一个配置项）；
- 关定时：`proactive_capability.add_cron_tools = false` → `future_task` 不再注册；
- `send_message_to_user` **没有配置开关** ✗ —— 要收口只能：① 我们插件在 `on_llm_request` 里
  从 `req.func_tool` 摘掉它（插件就是集成层，且这正好落实"何时开口由 Runtime 决定"）；
  ② 或改上游（不采纳）。


- **`zz_kb_probe`（测试栈里唯一的第三方插件）不注入任何提示词**：它只挂了
  `@filter.on_astrbot_loaded()`（`main.py:67`），是启动期探针。
- **工具 schema 本身也是注入**：`web_search_tavily`、`tavily_extract_web_page`
  （来自 `provider_settings.web_search = True`）、`future_task`（`core/tools/cron_tools.py:55`）、
  `send_message_to_user`（`core/tools/message_tools.py:81`）。`get_llm_tool_manager()` 还会带上
  **所有插件注册的工具**（我们没注册）。

> ⚠️ **由此暴露一个架构层面的口子**：`send_message_to_user` 让**她在任何一轮对话里直接给用户发消息**，
> 完全绕过 Runtime 的 `authorize`/冷却/每日上限；`future_task` 则会在未来唤醒一个**带独立
> "autonomous proactive agent" 系统提示词**的 agent 去发消息。也就是说"什么时候开口"目前有
> **三条并行通道**：①我们 Runtime 的 outbox（无人格 ✗）②`future_task`→cron 唤醒 ③
> `send_message_to_user` 当场发。按你原本的架构（Runtime 独占"何时说话"），②③ 应该关掉或至少收口。

## 6. 审阅结论（按我建议的处理顺序）

1. **【高】路径 B 没有 system_prompt** —— 她"主动开口"时没有身份、没有文风约束，与对话轮是两个人。
   两条路可选：(a) 在 `_render_payload` 里加一段我们自己的文风约束（不依赖宿主，改动小）；
   (b) 让插件取宿主人格并透传 `system_prompt`（更忠实，但要确认 AstrBot 的公开 API 能否拿到人格文本）。
2. **【高】`当前本地时间` 标错** —— `utility.isoformat()` 归一化回 UTC 导致；改用显式带偏移的本地格式化，
   并且**只保留一个时间来源**（建议我们在块里只给"距离上次消息 N 小时"这类相对量，绝对时间交给宿主；
   但那样路径 B 又完全没有绝对时间了 → 所以路径 B 的 prompt 里要单独补一行正确的本地时间）。
3. **【中】Skills 块 + 4 个工具** —— 对 QQ 陪伴角色是噪声（文档处理技能 + 联网/定时/主动发消息），
   而且 `send_message_to_user`/`future_task` 与 Runtime 的主动决策是两套并行机制，建议评估是否关掉。
4. **【中】人格里的 Markdown 反例** —— 与"不用 Markdown"自相矛盾，建议改纯文本。
5. **【低】历史里混着自己无人格渲染的主动消息** —— 若第 1 条修好，这个会自然缓解。
