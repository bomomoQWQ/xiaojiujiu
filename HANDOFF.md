# 换一台机器接着干（HANDOFF）

这份文档假设你在一台**全新的机器**上，只有这两个 GitHub 仓库，想把项目跑起来并接着改。

---

## 1. 这个项目的两个仓库

| 仓库 | 作用 | 语言/依赖 |
|---|---|---|
| [`xiaojiujiu`](https://github.com/bomomoQWQ/xiaojiujiu) | 主程序：持久认知 Runtime sidecar、Docker 部署、设计文档、验证脚本 | Python 3.11+，FastAPI + uvicorn，SQLite |
| [`astrbot_plugin_companion_runtime`](https://github.com/bomomoQWQ/astrbot_plugin_companion_runtime) | 宿主侧薄插件（AstrBot 用），独立维护、独立发版 | 纯标准库 + aiohttp（AstrBot 自带） |

两者**没有代码耦合**，只有 HTTP 协议耦合（协议 v1，见 `runtime/src/companion_runtime/api_v1.py`）。
`AstrBot/` 是上游项目，本仓库**不含**它，也从不修改它。

```bash
git clone https://github.com/bomomoQWQ/xiaojiujiu.git
cd xiaojiujiu
git clone https://github.com/bomomoQWQ/astrbot_plugin_companion_runtime.git
```

第二个克隆必须放在主仓库根目录、且目录名保持 `astrbot_plugin_companion_runtime`——
`docker-compose.yml` 会把该目录挂进 AstrBot 容器，两个端到端脚本也会导入它。
主仓库的 `.gitignore` 已把它排除，不会重复提交。

> **这条踩过一次，而且症状不像"路径错了"。** 本次接手时两个仓库是**同级目录**
> （`理解痞老板/xiaojiujiu` 与 `理解痞老板/astrbot_plugin_companion_runtime`），
> 看着很合理，但两个仿真脚本都把插件路径算成 `REPO_ROOT / "astrbot_plugin_companion_runtime"`
> （即主仓库的**子目录**），同级布局下这个路径不存在。实测报错（退出码 1）：
>
> ```
> RuntimeError: shipped plugin not found at .../xiaojiujiu/astrbot_plugin_companion_runtime/main.py
> ```
>
> 注意它**不是** ImportError，看着像"插件仓库缺文件"，实际是布局问题；
> `docker-compose` 的 `./astrbot_plugin_companion_runtime` 挂载点也会变成空目录。
> 判断方法：`ls xiaojiujiu/astrbot_plugin_companion_runtime` 有内容才对。
> 修法就是把目录移进去（`git mv` 不适用——它是**另一个仓库**，整体 `mv` 即可，
> 两边的工作树都不会因此变脏）。

需要一个真的 AstrBot 来跑集成时，另外克隆上游到 `AstrBot/`（该目录同样被忽略）：

```bash
git clone https://github.com/AstrBotDevs/AstrBot.git AstrBot
```

---

## 2. 环境准备

```bash
# Runtime（独立环境，不要污染 AstrBot 的环境）
cd runtime
python -m venv .venv            # 需要 Python 3.11 或 3.12；3.14 实测也可以（见下）
.venv/bin/pip install -e ".[test]"        # Windows: .venv\Scripts\pip.exe
.venv/bin/pip install pyyaml aiohttp pytest-subtests   # 见下"三个不在 pyproject 里的依赖"
```

插件测试只需要 AstrBot 桩（仓库自带 `tests/stubs/`），不装 AstrBot 也能跑。

### 三个不在 `pyproject.toml` 里的依赖

`pip install -e ".[test]"` **不足以**跑完第 3 节那四条命令。另外三个包必须装进**同一个 venv**
（两个仿真脚本是用 Runtime 的 venv 去加载插件的，所以插件的运行期依赖也得装在这里）：

| 包 | 谁需要 | 不装的后果 |
|---|---|---|
| `pyyaml` | 插件 `tests/test_packaging.py`（`import yaml`） | 插件测试**收集阶段就报错**，退出码 2，一条都跑不了 |
| `aiohttp` | 两个仿真脚本（走插件真实传输层 `AiohttpRuntimeTransport`） | 脚本拒绝启动：`cannot start: required dependencies are missing (ModuleNotFoundError: No module named 'aiohttp')` |
| `pytest-subtests` | 只为复现第 3 节那个"`+ 13 subtests`"计数 | 不影响正确性，但绿色计数会显示成 `156 passed`（见第 3 节） |

插件自己的 `requirements.txt` 是**故意留空**的（aiohttp 由 AstrBot 自带，写进去反而触发多余的依赖检查）——
所以这不是插件仓库的缺陷，而是"HANDOFF 只教了建 Runtime venv"这一个缺口的副作用。

**Python 版本**：`requires-python = ">=3.11"`，本文原写 3.11/3.12。本次接手在一台
**只有 Python 3.14.7** 的机器上按上面命令实跑，四条验证命令全部通过
（fastapi 0.141.1 / pydantic 2.13.5 / uvicorn 0.53.0 / pytest 9.1.1；
aiohttp 3.14.3 有 cp314 预编译轮子，不需要本地编译）。

**不需要任何模型、任何密钥、任何外网。** 默认 `SemanticProvider` 是 `disabled`，
Runtime 完全靠确定性代码工作；可选的远端语义 provider 的 key 只从环境变量
`CR_SEMANTIC_API_KEY` 读取，**永远不要写进任何文件**。

---

## 3. 四条验证命令（改完代码先跑这四条）

```bash
# 1. Runtime 离线测试
cd runtime && .venv/bin/python -m pytest              # 期望 1124 passed

# 2. 插件离线测试（在插件仓库里）
cd ../astrbot_plugin_companion_runtime
PYTHONPATH=tests/stubs:. ../runtime/.venv/bin/python -m pytest tests -q
#                                                      # 期望 170 passed, 13 subtests passed

# 3. 用户黑盒仿真：真实 uvicorn + 文件 SQLite(WAL) + 真插件钩子，只断言用户可见事实
cd ..
runtime/.venv/bin/python scripts/blackbox_user_simulation.py --base-dir ./bb-run

# 4. 高仿真故障恢复：并发上报、租约过期、断网恢复、重启续跑、队列 >150 行
runtime/.venv/bin/python scripts/e2e_resilience_simulation.py --base-dir ./res-run
```

> **三个"看着像失败、其实全绿"的显示陷阱**（都是本次接手实测，各跑两遍稳定）：
>
> 1. **命令 1 别再加 `-q`。** `runtime/pyproject.toml` 的 `addopts` 已经带了 `-q`，
>    命令行再来一个就是 `-qq`，pytest 会把 `828 passed` **整行省掉**——屏幕上只剩一排点，
>    看着像没跑完，其实是全绿。命令 2 的插件仓库**没有** pytest 配置，所以那里 `-q` 是安全的。
> 2. **命令 2 要看 subtests 计数就得给 `-q` 或 `-v`。** 实测三种写法：
>    不加 flag → `143 passed`；`-q` → `143 passed, 13 subtests passed`；
>    `-v` → 同上。**默认（verbosity 0）反而看不到那 13 个**，容易以为少跑了。
> 3. **别用裸 `python`。** 四条命令都要用 Runtime 的 venv（命令 2 也是——系统解释器里没有
>    pytest，会报 `No module named pytest`）。更阴的是：命令后面接了 `| tail` 时，
>    **管道会把退出码吞掉**，你看到 `exit code: 0` 却一条测试都没跑
>    （本次接手第一次跑插件测试就是这样：`No module named pytest` + 退出码 0）。
>    要看真实退出码就 `set -o pipefail`，或别接管道。
>
> **`pytest-subtests` 决定绿色计数长什么样**（实测）：
>
> | 装 `pytest-subtests`？ | 命令 2 的 summary |
> |---|---|
> | 装了 | `143 passed, 13 subtests passed`（需 `-q`/`-v`，见陷阱 2） |
> | 没装 | `156 passed`（13 个 subtest 被并回父测试，数字变成 143+13） |
>
> 两种都算通过，**不是"某个配置下测试变恒真"**：用一个故意失败的 subTest 探针实测过，
> 装与不装**都会红**（退出码 1，分别报 `SUBFAILED(i=N)` 和重复的 `FAILED`）。
> 换句话说 `pytest-subtests` 只影响**报告粒度**，不影响断言是否生效。

两个仿真脚本都会**真的起服务、真的走 HTTP**，并把产物写在 `--base-dir` 下
（`report.json`、`diagnostics.log`、`transcript.md`、若干 `runtime.sqlite3`）——
**除此之外不写任何文件**，也不会把字节码写到源码目录。任何一项检查失败都会以非零码退出。

用法速查：

```bash
python scripts/blackbox_user_simulation.py --list-phases            # 列出 12 个阶段
python scripts/blackbox_user_simulation.py --only setup,boundary   # 只跑指定阶段
python scripts/blackbox_user_simulation.py --fault leak             # 注错，验证检查真的会失败
```

`--fault` 有六种：`leak`（把内部标记/密钥塞进用户可见文本）、`duplicate`（每条主动消息发两次）、
`topic`（主动消息只聊被禁话题）、`guilt`（加追责话术）、`cross_session`（把私聊内容发到群聊）、
`default_session`（全部发到默认会话）。每一种都应当让**对应的那几条检查**失败——
这是"测试不是恒真"的证据，改断言之前先跑一遍。

---

## 4. 当前状态（最后一次提交时实测）

| 项目 | 结果 |
|---|---|
| Runtime 离线测试 | **1124 passed / 17 skipped**（无 DSN；PG 专项恒跳过） |
| 插件离线测试 | 143 passed + 13 subtests |
| 高仿真故障恢复 | 335/335 |
| 用户黑盒仿真 | **77 / 77**（退出码 0，连跑多次一致） |
| 记忆质量仿真 | **25 / 25**（`scripts/e2e_memory_simulation.py`，见第 6 节） |
| framework 测试 | **293 passed**（`framework/tests`，实测约 50s；旧文档写 227/102，已更正） |
| 版本 | Runtime 0.3.2（进行中）；插件 0.1.0 |
| 许可证 | GPL-3.0-or-later |

> **换机器复现记录**（Linux / Python 3.14.7 / 全新 venv，2026-09-15）：0.2.0 时点上的四行
> **逐条复现**——`828 passed`、`143 passed, 13 subtests passed`、`335/335`、`70/70`，四条退出码全 0。
> 另跑一次注错确认断言会咬人：`--fault leak` → `checks failed: 3`、退出码 1，与下表 `leak` 行一致。
> 复现过程中发现的两个环境缺口（三个额外依赖、目录布局）已补进第 1、2 节。
> 0.3.0 的数字是本机（Windows 11 / Python 3.13）实测，测试数与黑盒项数都变了，见 `CHANGELOG.md`。

**已验证的"检查真的会咬人"**（`--fault` 注错，每条都让对应断言失败；下表是 0.3.0 的实测值，
每格各跑两次取范围——仿真的唤醒时刻由真实墙钟驱动，所以同一注错的失败**条数**会有 ±1 的浮动，
"必然失败"才是保证）：

| 注错 | 黑盒失败的检查数 |
|---|---|
| `leak`（把内部标记/密钥塞进用户可见文本） | 3 |
| `duplicate`（每条主动消息发两次） | 5 |
| `topic`（主动消息只聊被禁话题） | 10–11（0.2.0 时点 9） |
| `guilt`（加追责话术） | 9（0.2.0 时点 8） |
| `cross_session`（把私聊内容发到群聊） | 4 |
| `default_session`（全部发到默认会话） | 5 |
| `memory`（维护间隔配到永不"到点"，于是永远不形成长期记忆） | 3 |

### 🚫 已冻结（2026-09-15，先不做）

下面两件**明确冻结**，不要因为"看起来该做"就顺手捡起来。冻结的是**发布动作**，
不是准备工作——`metadata.yaml` 已按市场规范校准，CI 要跑什么也已经清楚，随时可以解冻。

| 事项 | 冻结原因 | 解冻条件 |
|---|---|---|
| **AstrBot 插件市场发布** | 需要 AstrBot Cloud 账号，本项目这边没有；且发布是对外动作，时机应由所有者定 | 有了账号并决定发布时：到 <https://cloud.astrbot.app/> 提交插件仓库地址即可，`metadata.yaml` 已按[市场 JSON 规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)校准，无需再改 |
| **加 CI** | 需要令牌带 `workflow` 权限（或在网页上手动建文件），本项目现有的推送凭据没有该权限 | 拿到带 `workflow` 权限的令牌，或决定在网页上直接新建 `.github/workflows/` |

> 冻结期间：**不要**尝试推送 `.github/workflows/`（GitHub 会直接拒绝），
> **不要**为了发布去改 `metadata.yaml` 或两个 README 的安装说明。
> 两个 README 现在都写"克隆安装"，这与"尚未上架"是一致的，不是待修的缺陷。

### ⚠️ 主业务逻辑三个缺陷（2026-09-16）

报告：**`runtime/docs/BUSINESS_LOGIC_AUDIT.md`**（含实测数字与每条的验收方式）。
**A / B / C 三条全部已修**（`29f2328` / `94549a5` / `545ddc8`）。三条各自的性质：

1. ~~**回复长度用绝对阈值 `<= 4` / `>= 20`**~~ —— **已修（`29f2328`）**。
   照抄 `reply_delay_baseline` 加了 `reply_length_baseline` 与 `reply_turns_baseline`
   两条基线（后者只要均值：这一项是比值不是 z 分数）；两处绝对阈值替换；
   负向只能抵消已得加分，不再把真实回复判成负面证据；冷启动曲线与旧公式**逐值相同**，
   所以既有数据库判定不变。验收 `tests/test_reply_length_baseline.py` **13 条 + 9 个变异**
   （第 13 条与第 9 个变异来自 `019ee8a`：清点工具发现我写了两个基线视图却没接进
   `numeric_view`，成了新的死代码——见下"这一轮的工具反过来抓到了我"）。
   原始描述留档：`user_model.py:381` `:1005`，`turns` 在 `:1009`。
   违反设计 §29「回复速度、回复长度、对话持续长度都应相对用户自己的历史基线」。
   **唯一在默认配置下就发生**的一条：话少但行为一致的用户，证据权重被压低 2.5 倍
   （0.072 vs 0.180），`positive_probability` 只涨到 0.619 而非 0.700。速度那条基线已经实现了，
   照抄即可。
2. ~~**硬边界可被同义词 type 绕过**~~ —— **已修（`94549a5`）**。
   谓词不再自带 type 列表，改为从 `TYPE_TO_BEHAVIOUR` 派生（主动 ⇔ 行为类 ≠ `reply`）；
   未知 type 走 `behaviour_class_of` 的默认值即**失败关闭**。
   验收 `tests/test_boundary_synonyms.py` 8 条（核心那条按行为类分组断言同义词等价，
   所以将来新加的 type 当天就被覆盖）+ 4 个变异。
   原始描述留档：`motivation.py:855` 问的 `candidate.py:1300` 自己那份集合。
   `repair`/`share`/`curious_question` 会被"今天别主动联系我"拦住，同义的
   `apology`/`emotional_expression`/`question` 不会。违反 §52/§86.5「边界是硬约束」。
   默认配置下不可达（规则生成器不产出这三个），一开 v0.2 的 `remote_api` 就生效。
   **最该先修**：它是安全约束。
3. ~~**情绪时宜性按 type 硬编码**~~ —— **已修（`545ddc8`）**。
   改为按行为类两张表（`MOOD_MATCH_NEGATIVE_CLASSES` / `MOOD_MATCH_POSITIVE_CLASSES`）；
   **未知 type 拿通用值、不加成**——与 B 的失败关闭方向相反，是刻意的（B 是硬约束，
   C 是分寸）。`protocol.py:346` 改为复用 `QUESTION_TYPES`（旧清单漏了 `question`）。
   验收 `tests/test_mood_matching_by_class.py` 7 条 + 4 个变异。
   原始描述留档：同一个意图 `repair` 1.000 / `apology` 0.440，差 2.3 倍，直接进候选效用。

共同根因：`TYPE_TO_BEHAVIOUR` 是唯一权威的类型→行为类映射，但**四处各自硬编码了 type 集合**
且互相矛盾（清单在报告 §5）。三条的修法都是把消费方收敛到那个权威映射，而不是各处改一行。
**这三条现有测试一条都查不出来**（当时的 1096 + 四套仿真全绿），因为没有任何检查要求
"同义词必须等价"或"跨用户可比"。修复的顺序就是先补这类等价性检查、再动实现——
已修的各项，第一条测试都是等价性断言，而不是把某个数字钉死。

#### 接手从这里接：两件**需要人拍板**的事（都不是"还没写代码"）

**① 给"意图类型"这一维度定义单一权威语义（报告 §5 第 4 项）。**
现在"行为类"有了单一来源（`TYPE_TO_BEHAVIOUR`），但**另外三件事仍是各消费方自己判断的**：

| 这一维度 | 现在谁在定义 | 后果 |
|---|---|---|
| 主动 / 被动 | `candidate.NON_PROACTIVE_BEHAVIOUR_CLASSES`（B 新加的，从行为类推） | 已是单一来源 |
| 情绪方向（合时宜性） | `user_model.MOOD_MATCH_{NEGATIVE,POSITIVE}_CLASSES`（C 新加的） | 已是单一来源，但**表在 user_model 里、语义属于情绪** |
| 是否追问 | `user_model.QUESTION_TYPES` + `protocol` 复用 | 已是单一来源 |
| **加一个新 type 时要改几处** | 4 处（行为类 + 上面的三项按需） | 仍然靠人记得 |

要拍板的是：**要不要把这三件事变成类型词表里的显式字段**（例如
`TYPE_BEHAVIOUR = {"apology": {"class": "repair", "proactive": True, "mood": "-"}}`），
让"加一个 type"只需改一行、且漏改会当场报错。
倾向是要（那样它就只有一处来源），但这会**改变 `TYPE_TO_BEHAVIOUR` 的形状**、
动到 4 个模块的读取方式，属于一次小重构，不是补丁。**没做，等决定。**

**② 两处既有测试的期望值被我改过，需要确认可接受。**
B/A 修好之后有两条既有断言必须动，我改的是数字，但这属于"改别人的断言"，应当由你裁定：

| 测试 | 改动 | 我的理由 |
|---|---|---|
| `test_action_encoding_parity.py` 的 `CANONICAL_ACTION_FEATURES` | `apology`/`question`/`emotional_expression` 的 `proactive`：0 → 1 | 那张表编码的是**旧分类**（B 修的就是它）。同文件真正防回归的"预测侧 == 观察侧"断言未受影响 |
| `test_user_model_time.py::test_a_relative_slow_reply_is_weaker_evidence_not_negative_evidence` | `0.70` → `0.60` | 它的注释算术含一个已删除的绝对加成（`回复 >= 20 字` +0.10），而 fixture 每次都写 30 字——按 §29，30 字**就是**该用户的常态。要守的性质（延迟项只抵消加分、不翻转符号）没变 |

**若你判定这算削弱既有测试**，替代做法是把原始断言改成针对旧行为的 `xfail(strict=True)`
并在注释里写明"旧行为已被 §29/B 取代"，而不是直接改数字——**告诉我一声我就改过去。**

#### 怎么复核这一节（四条命令，都能独立回答"修了没有"）

```bash
cd /home/bomomo/理解痞老板/xiaojiujiu
# 1) 现象：三个探针打印改前/改后对照，恒退出 0，不断言
runtime/.venv/bin/python scripts/business_logic_probes.py

# 2) 验收：三组各自的可证伪断言（8 + 13 + 7 条）
cd runtime && ./.venv/bin/python -m pytest tests/test_boundary_synonyms.py \
    tests/test_reply_length_baseline.py tests/test_mood_matching_by_class.py

# 3) 变异：把 bug 放回去，看测试是否变红（全仓 48 个，全部应 KILLED）
cd .. && runtime/.venv/bin/python scripts/mutation_design_conformance.py
runtime/.venv/bin/python scripts/mutation_design_conformance.py boundary_synonyms  # 只跑一组

# 4) 清点：改完 A 之后这里必须仍是 0，否则就是我刚犯过的那个错（新增死代码）
runtime/.venv/bin/python scripts/dead_code_inventory.py
```

变异框架的分组名，按修的顺序：
`encoding`(①) `priors`(⑤) `declared_unused`(⑦) `redelivery`(崩溃窗口)
`chat_history`(③-1) `boundary_synonyms`(B) `reply_length`(A) `mood_classes`(C)。

**这一轮的工具反过来抓到了我**：修完 A 之后跑第 4 条命令，A 段从 0 变成 2——
我写了 `reply_length_baseline_view` / `reply_turns_baseline_view` 却没接进 `numeric_view`，
造出了新的死代码（正是 ⑦ 刚清掉的那一类）。`019ee8a` 修掉并补了一条测试 + 一个变异。
**结论：这三条检查（探针 / 验收 / 变异 / 清点）不是仪式，改完主业务逻辑后必须全跑一遍。**

### 接下来值得做的

0. **需要人拍板的两件**（详见上面"接手从这里接"）：① 要不要把"行为类 / 主动被动 /
   情绪方向 / 是否追问"合并成类型词表的显式字段（防止下次加 type 漏改）；
   ② 确认 B/A 修复时改动的那两处既有测试期望值可接受。**这两件不解决，下面的都可以先不做。**
1. ~~`committed != sent` 与"平台已发出 / 结果已上报"之间的崩溃窗口~~
   **已做（部分）**：Runtime 侧能做的部分做完了 —— 见下"崩溃窗口"一节。
   剩下的是 host 侧选择（至多一次需要落盘的"已发出"表），以及两个**有意留着**的判断。
2. framework/ 与 scripts/ 两个仿真脚本的整合（见 §7.1 的分工说明；
   目前两者互不依赖，这是有意的，整合前先想清楚要合并什么）。
3. ~~跨会话历史~~ **已完成**（见下"聊天窗口：持久对话记录"一节）。
   聊天窗口还剩：多行输入、全屏 curses 版本、回复的 Markdown 着色（清单见
   `framework/README.md` §8）。

**已修复但值得记住的形状**：渲染 prompt 里曾有两行同名指令。背景块自己也有
`- 想做的事：…`，内容是 Runtime 当前持有的意图——对一条主动消息来说往往是**上一次**
想说的那件事——而指令区又有一行同名的。任何读者取第一行就会照旧的写，
"群聊里的体检提醒被写成考试"就是这么来的。现在背景块那行改标为
`- 之前想做的事（背景，不是现在的任务）`，并且在渲染主动消息时整行剔除
（`api_v1._drop_intent_lines`）；黑盒仿真有一条正向断言守着这个性质。

```bash
python scripts/blackbox_user_simulation.py --base-dir ./bb        # 70/70，退出码 0
python scripts/blackbox_user_simulation.py --base-dir ./bb --fault leak   # 注错：证明检查会咬人
```

**发布状态（冻结中，见上表）**：插件市场尚未提交，市场里搜不到，两个 README 的安装说明
都是"克隆"；没有 CI。这两项都**不是**待修缺陷，是**有意暂停**。


---

## 5. 容易踩的坑（都踩过了）

1. **备份脚本必须保留 UTF-8 BOM**：`scripts/backup.ps1` 被无 BOM 重写后，Windows PowerShell 5.1
   会按 ANSI 解码，默认的中文 `-Source` 路径被破坏，脚本**照样打印快照路径却产出空快照**。
   改完这个文件务必确认新快照不是空的。
2. **CLI 没有 `--db` 选项**：存储路径由 `--base-dir`（解析相对路径）、`--config <toml/json>` 或
   环境变量 `CR_STORAGE__DATABASE_PATH` 决定，而且 `--base-dir` 是**全局参数，必须写在子命令前面**：
   `companion-runtime --base-dir /data serve`。
3. **`AstrBot/` 与插件目录都不进 Git**：`.gitignore` 与 `.dockerignore` 已排除。别用 `git add -A`。
   仿真脚本的产物同理：第 3/4 节用 `--base-dir ./bb-run`、`./res-run`，注错跑用 `./bb-fault`，
   这三个目录之前**没有**被忽略（`*.sqlite3` 被忽略了，但 `report.json` / `diagnostics.log` /
   `transcript.md` 没有），于是按文档跑一遍就把工作树弄脏了。已补进 `.gitignore`
   （连同 `pip install -e` 产生的 `*.egg-info/`）。换 `--base-dir` 到别处时记得一起加。
4. **凭据纪律**：API key、GitHub 令牌只走环境变量；不要 `git remote set-url https://<token>@...`
   （会把令牌写进 `.git/config`，随后被打进每一次备份 bundle）。
5. **`runtime/tests` 与插件测试是两套**：Runtime 的 pyproject 里配了 `pythonpath = ["src"]`，
   插件那套靠 `PYTHONPATH=tests/stubs:.`。在错误目录下跑 `pytest` 会去收集上游 AstrBot 的测试
   （表现为上百个 collection error），那不是你的改动坏了。
6. **别在 `runtime/` 之外的目录跑 `python -m pytest`**：同样会收集到 `AstrBot/` 的测试套件。
7. **`import` 到的不一定是你写的那份源码。** CPython 只按 **mtime + 大小**校验 `.pyc`，
   所以**同长度的改动在同一秒内还原**会留下一个"看起来有效"的缓存，让还原后的文件按改动版运行。
   任何"改文件→跑测试→还原"的工具都必须设 `PYTHONDONTWRITEBYTECODE=1` 并清缓存
   （`scripts/mutation_design_conformance.py` 三层防护 + 断言，见 ⑨）。
8. **改完源码后第一次跑黑盒仿真，teardown 会报 `changed: __pycache__/*.pyc`**——
   `repo_sources_changed` 把 `.pyc` 也算进了"源文件被改"，于是刚刚跑过 pytest（重编译了改动的模块）
   就会让这一次仿真在一条**指责仿真自己**的检查上失败；第二次跑就绿了，于是很容易被当成"偶发"。
   **已修**：字节码不再进"源文件被改"这个集合，改由它自己的检查（`no bytecode was written next to
   the sources this run imports`）和一条 note 负责——与 `scripts/e2e_resilience_simulation.py`
   早就正确的处理方式对齐。同时记住：`... | tail` 会把退出码换成 `tail` 的 0，这次差一点把红当成绿
   （见 §3 的显示陷阱 3）。

---

## 6. 改代码的推荐节奏

1. 先在 `runtime/tests/` 写一条**会失败的**测试（描述不变量的那句话），再改实现；
2. 跑 `.venv/bin/python -m pytest`（**别再加 `-q`**，理由见第 3 节的 venv 陷阱）；
3. 改动涉及跨时间行为（调度、未结事项、回复闭环、投递、**记忆形成与召回**）时，**必须**再跑一次黑盒仿真——
   0.2.0 靠它抓到两个单测完全没覆盖的缺陷（已了结的义务被重新打开、跨会话投递），
   0.3.0 又靠它抓到一条**断言自身**的随机误报（同一瞬间但投递更早的主动消息被算成"又问了一次"）；
   改动记忆模块时**必须**额外跑 `scripts/e2e_memory_simulation.py`——0.3.1 的十条修复里有八条是它先抓到的
   （最典型的一条：记忆形成后约 12 小时就再也检索不到，"长期记忆"实际只有半天有效期）；
4. 涉及并发/租约/重启时跑韧性仿真；
5. 提交信息写清：现象 → 根因 → 改了什么 → 怎么复现 → 怎么验证 → 还没做什么。

---

## 7. 目录约定

| 路径 | 说明 |
|---|---|
| `runtime/src/companion_runtime/` | Runtime 全部代码（`api.py` 原生路由、`api_v1.py` 插件协议层） |
| `runtime/docs/PATCH_V0.2_MAPPING.md` | 设计章节 → 代码位置 → 状态的对照，含**诚实缺口清单** |
| `runtime/docs/BUSINESS_LOGIC_AUDIT.md` | **主业务逻辑**的三个已复现缺陷（回复长度绝对阈值 / 硬边界可被同义词绕过 / 情绪时宜性硬编码），含实测数字、可达性分析与修法方向。**未修**，复现：`runtime/.venv/bin/python scripts/business_logic_probes.py` |
| `runtime/README.md` | 运维手册：配置项、API、降级、蓝屏恢复 |
| `framework/` | **外接测试框架**：可控虚拟时钟 + OpenAI 兼容 mock 端点 + 变量日志 + `cf` 命令行。不改原程序，见 `framework/README.md` |
| `scripts/` | 验证与运维脚本（**四个**仿真：黑盒 / 韧性 / 记忆质量 / 关系递进，外加 `runtime_bench.py`、`backup.ps1`、`dead_code_inventory.py`、`mutation_design_conformance.py`、`mutation_assistant_report.py`、`inspect_runtime_backend.py`、`send_test_message.py`） |
| `docs/SIMULATION_INTEGRATION.md` | `framework/` 与 `scripts/` **该不该合并**的书面评估：结论是「不合并套件、只共享机械件」，含两侧能力对比与全部 `file:line` 证据；§9 是父代理的独立复核 |
| `archive/` | 已放弃的本地模型路线（留档，不参与构建，包名是历史遗留） |
| `RECOVERY.md` | 备份 / 恢复 / 权重位置 |

### 7.1 framework/ 与 scripts/ 的区别

> **要决定「要不要把它们合并」之前，先读 `docs/SIMULATION_INTEGRATION.md`** —— 那是这件事的书面
> 评估（结论：不合并套件，只把 `free_port`、时钟重绑、源码快照、插件配置这四类无行为语义的机械件
> 抽出来共享）。下面这段是结论的浓缩版。

两者都在测 Runtime，但定位不同，别搞混：

* `scripts/` 下那两个仿真脚本是**为固定剧本写死的验收**（黑盒 13 阶段 77 项、韧性 335 项检查），
  跑一次给一个是/否，改断言前先跑 `--fault` 注错确认它还会咬人。
* `framework/` 是**可交互的实验台**：起一个 harness，然后用命令行在运行中拨时间、灌输入、看变量、
  给假端点注错。适合"我想知道改成这样会发生什么"，而不是"发布前必须全绿"。

框架自带 293 个测试，其中包含用子进程跑 `cf run` 再拿客户端命令驱动它的验收测试：

```bash
cd framework
PY="$(cd ../runtime && pwd)/.venv/bin/python"   # 绝对路径，避免 sys.prefix 噪音警告
"$PY" -m pytest tests                            # 293 passed
```

框架要用原程序的 venv 跑（它要启动 uvicorn），但框架代码本身只用标准库。

文档里的 `F:\理解痞老板\...`、`E:\companion_runtime_backup\...` 是**作者本机的路径**，
不是代码依赖；换机器时所有脚本都用 `--base-dir` 指定输出位置即可。

---

## 0.3.3 进行中：关系递进仿真报出的产品问题（异地接手请先读这一节）

`scripts/relationship_progression_simulation.py`（0.3.2 新增，陌生人→恋人 + 模拟时钟 + 后台审视）
首次跑通后报出 7 条产品问题。当前进度、证据与**未完成部分的确切状态**如下。

### 远程 Docker 端到端：插件与 Runtime 之间有**两处静默断点**（本节点，已修）

用测试前端（`POST /send`）驱动远程 Docker 测试栈时，出现"前端能聊、Runtime 收不到任何东西"
（`raw_events` 一直 43、`memories` 0、镜像 43 行、消息不在镜像里）。**两个原因叠加，两个都不会报错。**

**断点 A：`plugin_set` 白名单（AstrBot 侧配置，最隐蔽）**
- `star_handlers_registry.get_handlers_by_event_type()` 在**唤醒检查之前**就把"不在
  `plugin_set` 里的插件"的**所有 handler 丢掉**（`star_handler.py:169-184`，非保留插件一律 `continue`）。
- 于是插件**照样被加载、`initialize()` 照样执行、outbox 照样轮询**（日志里能看到
  `adapter started (observe_mode=all)` 与成百上千条 `POST /v1/outbox/lease`），
  但 `on_message_observed` / `on_llm_request` / `on_llm_response` **一次都不跑**：
  `POST /v1/events` 与 `/v1/context` 在 Runtime 侧**计数为 0**。
- 测试实例的 `plugin_set` 是 `["astrbot_plugin_zvv","astrbot_plugin_math_plotter","astrbot_plugin_gptimg"]`
  （三个本实例里根本不存在的插件名，疑似从另一台机器带过来的配置）。
  加进本插件并重启后**立刻通了**：`v1/events` 0→1、`v1/context` 0→1，
  `/companion_runtime` 指令有回复，`raw_events` 出现带 `"source":"v1","adapter":{...}` 的真实事件。
- **产品侧加固（已做）**：`main.py::_warn_if_whitelisted_out()` 在 `initialize()` 自检并在
  被漏掉时打一条 WARNING（把"看起来健康、其实全静默"变成一行日志）。
  测试：`test_an_unwhitelisted_plugin_warns_at_startup` / `test_a_whitelisted_plugin_stays_quiet`。
  文档：插件 README 新增「白名单」一节。

**断点 B：流式输出下根本不存在"发送后"钩子（插件侧缺陷，已修）**
- AstrBot 默认开流式：`respond` 阶段走 `send_streaming()` 后**直接 return**（`respond/stage.py:217-231`），
  因此 `@filter.after_message_sent()` **永不触发**；同一个流式结果在 `result_decorate`
  阶段也**提前 return**（`stage.py:134`），`on_decorating_result` 同样不触发。
- 实测：连续 10 轮真实对话后 `raw_events` 里 `assistant_message` = **0**
  （`event_*` 只有 `user_message`/`system`/`candidate_proposal`）。
  日志佐证：`Applying streaming output (default)` + `Prepare to send - ...: `（消息链为空）。
- **改法**：改从 `on_llm_response` 上报这一轮答复（agent runner 在任何投递模式下**每轮恰好调用一次**，
  拿到模型最终文本；流式下就是用户看到的那段话）；`after_message_sent` 保留用于
  "没经过 LLM 的回复"（指令输出等），同一轮用 `event.set_extra()` 标记去重。
- 测试：`test_streamed_turn_is_reported_from_the_llm_response`、
  `test_a_reported_turn_is_not_reported_twice`、`test_a_reply_without_an_llm_turn_is_still_reported`。
  变异证据：M1（`on_llm_response` 不上报）/M2（去掉去重）/M3（不跑自检）/M4（自检不看名字）
  四条**全部 KILLED**（`scripts/mutation_assistant_report.py`，本节点新增，跨平台）。
- 已知代价（写在 README）：长回复被 t2i 渲染成图片时，上报的是模型原始文本而不是那张图片。

**顺带确认的两件事（都不是缺陷）**
- `memories` 短时间恒为 0 是**设计**：候选要先等
  `memory.consolidation_interval_seconds`（默认 3600s）之后的下一个内源轮次才合并，
  或由 `companion-runtime --base-dir /data consolidate` 手动跑一趟。真正的中间态在
  `memory_candidates`（`status: pending`）与 `working_situation_items` 里，实测都在正常增长。
- **测试工具自己的坑（记下来，别再踩）**：Windows 上 `Invoke-RestMethod` 直接发字符串 body 会把
  中文变成 `?`（AstrBot 自己的 `core.event_bus` 日志也是 `????,???????????,?????`，
  即损坏发生在**离开发送端之前**）。必须显式编码：
  `[System.Text.Encoding]::UTF8.GetBytes($json)` 作为 `-Body`。
  本节点把两件工具固化进了 `scripts/`，以后不要再手搓：
  - `scripts/send_test_message.py`：往测试前端发消息（内部就是 UTF-8 字节，跨平台），
    `--transcript` 读回机器人回答；
  - `scripts/inspect_runtime_backend.py`：只读打印后台（各表行数、**宿主到底发了哪些事件**、
    会话、候选/记忆/激活池/工作局势/runtime_state/outbox），
    本地 `--db <file>` 或 `docker exec -i <容器> python - < scripts/inspect_runtime_backend.py`。
  之前那批 `?` 事件因此污染了测试用 Runtime 的时间锚。

**远程回归（本次，在部署 checkout 上跑）**
- Runtime：**1127 passed / 17 skipped / 0 failed**（Python 3.12.3，`~/xiaojiujiu_test/runtime/.venv`，
  该 venv 是指向 `~/xiaojiujiu_test/runtime/src` 的 editable 安装，与部署 checkout 同为 `4829e60`）。
- 插件（部署克隆 `3861678`）：**148 passed / 13 subtests passed**。
  注意：那个 venv 里原本缺 `pyyaml`，`tests/test_packaging.py` 会**收集失败**
  （`ModuleNotFoundError: No module named 'yaml'`）→ 按 §3 的说明 `pip install pyyaml pytest-subtests` 即可。
- 本地同一条数：Runtime 1127/17，插件 148+13 —— 两边完全一致。
- 四个仿真（本地）：blackbox **77/77**、resilience **335/335**、memory **25/25**、
  relationship **105/105 但会偶发 1 条红**（见下）。

**仍存在：relationship 仿真的"召回后必须回到 active"偶发红（未修，已定位到 harness 层）**
- 今天 11 次运行里红了 2 次（1/4 与 1/6 两批）。红的那条永远是
  `PHASE 3 ... asking about the acquaintance-stage facts brings each memory back into the working set`，
  但**失败的记忆每次不同**（一次是 `通勤`，一次是 `团子`）；同一次运行里另一个同样
  `low_activation` 的记忆（`医院`）却成功回到了 `active`。
- 已经排掉的：打分不再是随机 —— `retrieve` 的 epsilon 已是 `memory_id` 的纯函数，
  `Scheduler` 的 ±5% 抖动在仿真里是**显式播种**的（`rng=random.Random(SCHEDULER_SEED)`，
  脚本 1575 行）。
- 剩下的唯一时钟是**真时钟**：`config.scheduler.min_interval_seconds=0.02 /
  max_interval_seconds=0.05`（脚本 1670-1671 行）是**真实秒**，而检查要求
  "推进两个模拟分钟后，恰好**一轮**自主回合看到用户刚说的那句话"（脚本 2226-2231 行注释）。
  在 20–50 ms 的真实间隔下，这两分钟里实际跑了几轮是真实调度竞态 → 关键那一轮的线索
  有时不是用户刚说的句子，于是那条记忆没被重新召回。
- 结论：**这是 harness 的竞态，不是打分的不确定性**，所以没有改产品代码，也没留假绿的测试。
  要根治应让 harness 自己驱动回合（而不是靠真实定时器），或把断言从"必须回到 active"
  放宽成"这一轮确实以用户那句话为线索"。

### 主动消息（内源主动联系）：**已全程跑通**（本节点实测，含一次我自己的误判）

链路：`hazard 触发 → proactive_committed → outbox(render) → 插件租约 → 用本会话模型 render
→ Runtime authorize → send → delivered`。

实测证据（测试栈，1v1 会话 `default:FriendMessage:20001`）：
- `POST /endogenous {"now": +2h}` → `{"acted": true, "reason": "hazard_triggered"}`；
- `action_attempts.state=committed`、`proactive_committed` 落 raw_events、outbox 出现 `render` 行动
  （`intent=询问等待检查结果`、`constraints=[避免造成催促感, 不要连续追问]`）；
- 另一个会话先说话时，Runtime 判定 `reconcile:rerender / reason=user_spoke_first`
  （"用户先开口了，原措辞需要重写"）→ **没有把旧措辞发出去**，`committed != sent` 在真实链路上成立；
- 最终 `outbox: {"stats":{"delivered":2}, status="delivered"}`，聊天里出现那条**没人触发**的消息：
  "体检结果出来了吗？不着急，有消息了跟我说一声就行。" —— 与 intent/constraints 完全吻合，且发给
  正确会话（当时正在说话的是另一个 user）。

**⚠️ 我在这条上误判过一次，教训写在这里**：我把 `available_at` 改到 19:40 后，**19:41 就直读数据库**，
看到 `attempts=0` 就断言"主动消息根本落不了地"，还把正常的 `dispatch: false / attempt_in_flight`
当成死结。真相是投递要走"租约 → 渲染（要调一次 LLM，几十秒）→ 授权 → 发送"四步。
两条方法论：**多步异步链路的观察窗口至少 1–2 分钟**；**直读 DB 的瞬时快照只能证伪"已发生"，
不能证明"不会发生"**。

### 按会话路由到不同 Runtime（**已实现并在测试栈验证**）

**结论**：插件 `44250be` 起支持 `session_routes`，一人一个 Runtime，记忆不再混。

真机验证（测试栈加第二个 Runtime：`runtime-b` + 独立卷，`session_routes =
{"default:FriendMessage:20002": "http://runtime-b:8787"}`）：
- 插件启动日志：`targets=http://runtime:8787, http://runtime-b:8787`（两个目标都在跑）；
- 默认 Runtime：`raw_events 44 / memories 9`，新到的只有**小林**那句；
- `runtime-b`：`raw_events 4 / memory_candidates 1 / memories 0`，**只有小周的新事实**
  （"我是小周，我养了一只叫团子的橘猫，它三岁了。"），**一条小林的东西都没有**；
- `runtime-b` 自己在收 `POST /v1/outbox/lease`（每个目标各自一个轮询器，实测在跑）。
- 注意：默认 Runtime 里**历史上**已经有小周的旧数据（路由生效之前写进去的）；要让实例彻底干净，
  清那个卷重来即可。

**过程中踩到两个真实故障（都是"本地全绿、上机直接炸"）**：

1. **AstrBot 会删掉 schema 里没声明的配置键**。先把 `session_routes` 写进插件配置、后部署代码，
   AstrBot 加载时直接 `Config key removed: session_routes` 把它清掉 → 路由静默不生效。
   **顺序必须是：先部署带 schema 的代码，再写配置。**
2. **插件 schema 里 `"object"` ≠ 自由映射**，它会让**整个插件加载失败**（不是降级）：
   `astrbot_config.py::_parse_schema` 对 `object` 无条件递归 `v["items"]`，没有 `items`
   就 `KeyError: 'items'` → `Failed to load plugin`。自由映射要用 `"type": "dict"`
   （`check_config_integrity` 的注释写明了）。已修，并把 `test_packaging` 那条类型检查
   补强成与 AstrBot 解析器同规则（`object` 必须带 `items`），否则下一个人还会踩。

**已勘察的接口事实（留档）**：

**为什么需要**：`memories` / `memory_candidates` 表**没有 `conversation_id` 列**（列是
`memory_id/kind/summary/structured_json/topics_json/importance/confidence/status/source_event_ids/created_at/updated_at/archived_at`），
`list_memories` / `_retrievable` 也不按会话过滤 → **长期记忆是全局一份**。实测两人各自私聊时
事件与投递路由是分开的（`default:FriendMessage:20001` 19 条 / `...20002` 5 条），主动消息也投给了
正确的会话，但记忆池共享。封测形态是"好几个人各自和它 1v1"，所以要么一人一个 Runtime，要么记忆必串。

**选定方案：插件按会话选 Runtime（不动 Runtime 认知层）**。理由：认知层加会话作用域要改 schema +
检索过滤，动静大且碰语义；路由只加一层寻址。

**已经勘察好的接口事实（省去重新摸索）**：
- 事件上报：`main.py::on_message_observed` → `_enqueue_event(record)` → `self._queue.put({...})` →
  `_deliver(item)` 用 `self._transport.post_events(...)`；**队列 payload 里加 `target` 即可让单队列多目标**
  （`BoundedRetryQueue` 的 sender 是 `self._deliver`，`item.payload` 是自由字典）。
- 注入：`on_llm_request` → `_inject_context` → `self._bridge`；`ContextBridge` 的缓存**本来就按 session 键**
  （`_cache` + `context_cache_max_sessions`），所以每个目标一个 bridge 实例即可，缓存语义不变。
- 主动消息：`OutboxConsumer.run()` 用 `self._transport` 租约 → **这是唯一真正的扇出点**：
  需要每个目标一个 consumer（或一个 consumer 轮询全部目标），租约请求里的 `adapter_id` 保持不变。
- 会话取值：`event.unified_msg_origin`（与 raw_events 的 `conversation_id` 一致，如
  `default:FriendMessage:20001`）。
- 目标取值：新增 `Settings.session_routes: dict[str, str]`（配置里一张 JSON 表，键为 session 或
  session 前缀，值为 `http://runtime-b:8787`），未命中回落 `runtime_base_url`。
  **不要**只加配置项不接线——那正是本项目反复清掉的"声明了却没有读者"。
- `_status_text()`（`/companion_runtime` 指令）要按目标分行报 queue/bridge/outbox 统计，
  否则多实例下状态不可读。

**验收（必须可证伪）**：
1. 离线：两个 `FakeTransport` 目标 + 两个 session，断言"事件落到各自的 transport、注入读的是各自的
   bridge 缓存、outbox 只租约自己那台的行动"；再加一条"未配置路由时全部回落默认目标"（保护单实例部署
   的既有行为）。
2. 变异：把 `target_for()` 改成恒返回默认值 → 上面第一条必须变红。
3. 真机：在 `astrbot_test` compose 里再加一个 `runtime-b`（同镜像、另一个卷、`CR_CONVERSATION_ID` 不同），
   两个 user_id 各聊一轮，**各自 `/memories` 里只应看到自己的事**，且 `assistant_message` 两台的
   raw_events 各自 1:1。

**当前状态（本轮结束时）**：插件仓停在 `3861678`（干净、148 passed + 13 subtests），Runtime 仓 `c0e6687`，
测试栈可用且**主动消息已全程跑通**（见下），**没有半成品改动**。


### 封闭测试形态：一人一个 Runtime（B+ fleet）+ 零重启加人（本节点完成，测试栈已验证）

**形态**：一个 `runtime-fleet` 容器（`scripts/runtime_fleet.py`）里为每个人起一个
`companion-runtime serve`——各自端口（8787…）与各自 `--base-dir /data/<person>`（独立 SQLite）、
各自日志 `/data/logs/<person>.log`、崩溃自动重启并计数；控制面 `:8800` 提供
`GET /fleet/status`、`GET /fleet/routes`、`POST /fleet/provision`、`POST /fleet/restart/<person>`、
`POST /fleet/deprovision/<person>`；`people.json` 是唯一事实来源。
生成：`python3 scripts/build_runtime_fleet.py --people 20001-20012`（环境从手写 `astrbot.yml` 的
runtime 服务复制，避免两处漂移；角色价值观 profile 也在这里注入）。

**为什么不是"一人一个容器"**：10 个容器要手管日志/重启/升级，实测铺开后立刻变负担；
进程级隔离已足够（各自 SQLite、各自进程、无共享内存）。真子容器（管理容器 + `docker.sock`）
作为升级路径保留——插件只认 URL，换实现对插件零改动。

**零重启加人**：插件新增 `route_registry_url`，定期读 `/fleet/routes`，按需为新 URL 建
transport/bridge/outbox 轮询器；优先级 = 显式 `session_routes` > 注册表 > `runtime_base_url`。
实测：fleet 上 provision 第 12 个人（端口 8798）→ **立刻**发消息 → **AstrBot 全程未重启** →
消息落进他自己的实例，默认实例里没有他。

**验证过程抓出的两个真 bug（都是我引入的，且都只在真机验证时才现形）**：
1. **10 个进程共用一个数据库**。镜像 ENV 的 `CR_STORAGE__DATABASE_PATH=/data/companion.sqlite3`
   是绝对路径，被 `os.environ.copy()` 原样传给每个子进程，于是 `--base-dir` 形同虚设。
   证据：`/data` 下只有一个 `companion.sqlite3`（WAL 已 688KB），10 个 per-person 目录全空。
   修法：每个子进程显式设置 `CR_STORAGE__DATABASE_PATH` / `CR_STORAGE__RAW_LOG_PATH` 到自己的 base dir。
2. **首次消息竞态**。刚 provision 的人第一条消息到达时注册表缓存还没刷新，插件回落默认实例 →
   那个人的第一句话进了**别人那份记忆**。修法：注册表已可达而某会话无路由时，事件**带 session
   入队、不带 target**，投递时重新解析，没答案就抛错让有界重试队列稍后再试（宁可等几秒）。
   同时区分"注册表不可达"（`None`）与"答了但不知道这个人"（`{}`）：只有注册表**答过至少一次**
   才启用"等待"语义，从未答过时行为与以前完全一致（回落默认实例，不做回归）。

**价值观**：`ValueProfile` 只在实例**第一次建库**时写入 state（`runtime.py:418 → ensure_defaults`），
之后以库里那份为准——**改 env 对已有实例无效**，必须直接改 `runtime_state.values_json`。
两处都要改：库里那份管已存在的人，`fleet.yml` 那份管新开通的人。
工具：`scripts/set_value_profile.sh`（改顶部 PROFILE 块即可，自带预测/快照/逐实例核对/同步 fleet.yml）。

当前画像（2026-09-17 起，高依附版）：`boundary_respect .05 / user_care 1.0 /
relationship_maintenance 1.0 / stability_commitment 1.0 / conflict_directness 1.0 /
curiosity 1.0 / emotional_expression 1.0 / autonomy .05`
（旧的是 `br .92 / uc .90 / rm .85 / sc .85 / ee .30 / cd .55 / auto .75 / cur .70`）。

> ⚠️ **8 个轴里有两个是死代码**：`emotional_expression` 与 `autonomy` 只出现在
> `emotion.appraise_event`，而该函数**没有生产调用方**（入口走
> `semantic.settlement_to_evaluation`），所以它们目前**不产生任何行为差异**。
> 设置它们只是为了记录意图。真正生效的是另外 6 个（见下）。

> ⚠️ **"只改价值观"的真实效果是"行动变便宜"，不是"憋不住"**（我一开始算错过，这里记正解）：
> 价值观只改 restraint / impulse 的**目标值**（`target_drives`，motivation.py:654-681），
> 而这两个是**状态变量**，以 `tau_restraint 9000s` / `tau_impulse 5400s` 收敛 ⇒ 效果以小时计。
> 真正的沉默效用是 `motivation.py:214-221`：
> `U = 0.28 + 0.45*restraint + 0.30*risk + 0.25*cooldown + 0.42*impulse - 1.2*pressure²`。
> 把 restraint 目标从 sigmoid(0.45)=0.611 压到 sigmoid(-0.2625)=0.435，只让沉默效用降约 0.08；
> 而 impulse 升高又因 `impulse_gain` 为**正**（有意的设计：想说话让"憋着"更贵）把它加回约 0.05
> ⇒ **净变化仅约 -0.03**。真正被拉动的是行动侧：`boundary_cost = 0.75*br*max(...)` 随 br .92→.05
> **塌掉 87%**，实测首条新判决 `advantage` +0.13~+0.20（例：0.3652 → 0.5693）。
> 所以现在**闸门是 hazard 而不是 utility**：新判决仍是 `hazard_not_triggered`（hazard~5e-5），
> 她要等 hazard 触发才会开口。想让"憋不住"也变，得动 `CR_SILENCE__*` 系数（用户明确说先不动）。

**当前测试栈布局**：`astrbot-test`(6186/6299) + `xxj-onebot`(6300) + `xxj-napcat-test`(6098，真 QQ 已登录) +
`xxj-runtime-fleet`(控制面 8800；内部 8787–8799 + 8801，共 14 人) + `xxj-runtime-test`(默认回落实例)。
旧的 9 个 per-person 容器与 `runtime-b` 已回收，卷已 tar 备份到 `~/astrbot_test/backups/fleet-migration-*`。

**加一个人（AstrBot 不动）**：
`curl -XPOST http://192.168.1.15:8800/fleet/provision -H 'Content-Type: application/json' -d '{"session":"default:FriendMessage:<QQ>"}'`
——插件在下一次同步（≤5s，或该人第一条消息时立刻触发）自动接上。

**更省事：已经不需要手动 provision 了。** `route_auto_provision: true`（默认 false，测试实例已开）
让插件在遇到"注册表里没有的会话"时自己向 fleet 发一次
`POST /fleet/provision {session}`（每会话一次，上限 64），拿到地址后再同步一次，
于是**陌生人的第一句话就落进为他新建的实例**。实测：
`29999` 从没被 provision 过 → 插件日志 `fleet provisioned default:FriendMessage:29999 at
http://runtime-fleet:8799` → fleet 13 人 → 他自己的库里只有他自己的会话。

> ⚠️ **测试坑（本轮实际踩到）**：`docker restart astrbot-test` 之后，插件日志里的
> `adapter started` 出现在**平台适配器连接之前**（本次相差约 10 秒，日志里等的是
> `aiocqhttp(OneBot v11) 适配器已连接`）。在这个窗口里往测试前端发消息：
> 前端 HTTP 会返回 200（它只负责投进自己的 WS 队列），但 **AstrBot 一条都收不到**，
> 前端 `/state` 的 `errors` 会涨。**等"适配器已连接"再发**，否则会得出"功能没生效"的假结论
> （本轮就是这样误判了一次）。

**默认回落实例已清空**（`xxj-runtime-test` 里路由生效前的 20001–20010/20099 历史已删除），
现在它只作为回退存在，只剩下系统自己的 `default` 会话。

### 封测采集（P0–P2，已完成并在测试栈验证）

**为什么要有这一节**：封测的产出不是"跑了一周"，而是**能读完的数据**。动手前先核对现状，
发现两样东西**完全没有落盘**：动机博弈的判决（效用分解、为何不动）、以及状态/情绪的时间序列；
注入内容按设计是临时的，也没有历史。

**Runtime 侧（只增加记录，不改变行为）**：
- 新表 `decisions`：每次内源轮次一条，**acted=false 的也写**——"她今天为什么没主动说话"
  只能从没动的那些轮次里读；payload 保留完整 `MotivationResult`（每个落选候选的
  internal/user/relation/成本/概率都在）。
  ⚠️ 踩过的坑：`endogenous_round` 的 **foreground_pause 分支在动机博弈之前就 return**，
  一开始没记录 → 聊天最活跃的时段一条日志都没有。已补（`trigger=foreground_pause`）。
- 新表 `state_samples`：每轮一个采样点（valence/arousal/stability/impulse/restraint/pressure），
  把 `runtime_state` 的单点快照变成曲线。
- 新表 `refresh_runs`：**每次深刷新尝试一行，被跳过的也写**。列 `ran_at / trigger / ran /
  reason / provider / degraded / operations / settled_events / latency_ms` + 完整 payload
  （含 `trigger` 对象与 `violations`）。补得晚，代价是一整晚的排查靠手搓脚本：
  之前只有 `runtime_state.meta.last_deep_refresh_at` 一个时间戳，于是"刷新跑了但什么都没结算"
  和"刷新压根没跑"在数据上完全一样。注意 `ran` 的语义是"有提案走到 reducer"，
  要和 `reason` 成对读（`ran=0 + empty_suggestions` = 模型什么都没说）。
- 注入留痕：每次 `/v1/context` 写一条 `system/context_rendered` 原始事件（trigger、version、
  总字数、**各段字数**）；全文另由 `observability.record_context_text` 控制（默认关）。
- 配置段 `ObservabilityConfig(enabled=True, record_context_text=False)`；写入走 reducer 自己的
  事务（不引入第二个写者），失败只告警不拖垮轮次。
- 读回端点（不必再进步进容器）：`GET /cognition/backlog`（还剩什么没结算）、
  `GET /cognition/refreshes`（为此做过什么）、`GET /observability/decisions`、
  `GET /observability/state-samples`。注意这些在**各人自己的端口**上（8787+/8801），
  而那个端口只在 docker 内网里 —— 要读就先看 `GET /fleet/status` 拿端口，
  再从 fleet 容器内部访问。

**采集密度（重要）**：一条用户消息**不会唤醒 Runtime 自己的调度器**，默认上限 5400s
意味着最坏 90 分钟才有一次决策，一周每人只有几十条博弈日志。fleet 现在设
`CR_SCHEDULER__MAX_INTERVAL_SECONDS=900`（MIN=60）——因为 hazard 是在两次决策之间积分、
**按设计频率无关**（两个短区间与一个长区间的生存概率相同），所以**只提高分辨率、不改变行为**。
实测：静置 5 分钟后自动多出 1 条决策。

**工具（`scripts/`）**：
| 脚本 | 用途 |
|---|---|
| `export_beta_data.py` | 容器内只读挂载 fleet 卷 → 每人 `events/decisions/state_samples/refresh_runs` + 全部表 JSONL + `summary.json` + `runtime.log` + manifest |
| `replay_session.py` | 把"用户说了什么 / 当时注入了什么（含分段字数与版本）/ 之后的决策与落选候选"并排打印，尾部附**深刷新账本与 reappraisal** |
| `beta_daily_report.py` | 每日每人 markdown（对话/决策原因分布/候选/未解释/**深刷新次数与结算条数**/状态曲线/实时健康），写 `reports/<date>.md` |
| `snapshot_beta.py` | 优化前用 SQLite backup API 冻结整支 fleet（含 compose 与路由快照） |
| `beta_daily_collect.sh` | 上面三条的定时包装（导出 → 日报 → 收尾），crontab 每天 08:05 CST 调它 |
| `fleet_probe_instance.sh` | 在 fleet 容器里读某个人的库：事件直方图 / 判决列表 / 采样数 / 语义状态（`QQ=<QQ>` 作环境变量传） |
| `fleet_probe_context.sh` | 网络内直调该实例 `POST /v1/context` 并回读 `context_rendered` 是否 +1（验证埋点闭环用） |
| `fleet_probe_semantics.sh` | 看某人"为什么情绪是平线"：结算/未结算分布、深刷新是否回写、mood 列、情绪事件计数 |
| `fleet_probe_ledger.sh` | 直接打印 `refresh_runs` 账本（谁触发的、跑没跑、结算几条、降级没） |
| `fleet_probe_refresh.sh` | 对某人强制跑一次 `/cognition/refresh` 并回读结算结果（`QQ=<QQ>`） |
| `beta_e2e_check.sh` | 端到端验收：导出 → 回放 → 日报，一次跑完看三段输出 |
| `fleet_probe_dashboard.sh` | 验收看板：`/fleet/status` 的 refresh 字段 + dashboard 表头与指定人的那一行 |
| `fleet_probe_outbox.sh` | 追一条主动消息的去向：outbox / action_attempts / attempt_events（判断"提交了但没送到"卡在哪一步） |
| `fleet_probe_real_person.sh` | 真人实例一览：最近事件、结算分布、账本、情绪/心情、状态曲线、判决 |
| `fleet_probe_proactive.sh` | 她到底主动过几次、动机池里有什么 |
| `fleet_runtime_usage.sh` | 容器/实例用量盘点：谁在真收消息、谁只是模拟残留、资源占用、插件路由配置 |
| `deploy_plugin.sh` | 更新测试栈插件（服务器上是 git clone）、重启 AstrBot、等适配器连上并验加载 |
| `fleet_final_check.sh` | 收尾体检：容器 / fleet 健康 / 镜像一致性 / 定时任务 / 磁盘一屏看完 |
| `deploy_test_runtime.sh` / `set_semantic_budget.sh` | 重建镜像并重建 fleet / 改 fleet 语义配置并重建（`docker cp` 进 fleet 容器会报 `/proc/self/fd`，用 stdin） |
| fleet `GET /fleet/dashboard` | 只读、30s 自刷新的总览页；`/fleet/status` 增加 unresolved / open_unfinished / deep+explain 调用数 |

**数据落在服务器机械盘**：`/mnt/xz/xiaojiujiu-beta/{<批次>,reports,snapshots}`（`/dev/sda1` 932G，
519G 可用）。一次 14 人导出约 5MB。

**每日流程（三条命令）**：

> 已经装成定时任务了，正常不用手打：`bomomo` 的 crontab 里一条
> `5 8 * * * /home/bomomo/astrbot_test/beta_daily_collect.sh`（= 08:05 CST = 00:05 UTC，
> 见下"为什么是这个钟点"）。包装脚本在服务器 `~/astrbot_test/beta_daily_collect.sh`，
> 仓库同源副本 `scripts/beta_daily_collect.sh`；日志按 UTC 时间戳落在
> `/mnt/xz/xiaojiujiu-beta/logs/<UTC>.log`（脚本自己 `exec >>` 重定向，不依赖 cron 邮件）。
> 日志里显式 `LANG=C.UTF-8`：cron 的 locale 是 POSIX，不钉死会写出乱码甚至 UnicodeEncodeError。
> 手跑一次：`ssh bomomo@192.168.1.15 '~/astrbot_test/beta_daily_collect.sh'`。

```bash
# 1) 采集（容器里读卷，写机械盘）
docker run --rm -v astrbot_test_runtime-fleet-data:/data:ro \
  -v /mnt/xz/xiaojiujiu-beta:/export -v ~/astrbot_test/src/xiaojiujiu/scripts:/scripts:ro \
  python:3.12-slim python /scripts/export_beta_data.py --note "day-N"
# 2) 读某个人这一周
python3 ~/astrbot_test/src/xiaojiujiu/scripts/replay_session.py \
  --export /mnt/xz/xiaojiujiu-beta/<批次>/people/<person>
# 3) 每日汇总（--date 省略即今天）
python3 ~/astrbot_test/src/xiaojiujiu/scripts/beta_daily_report.py \
  --export /mnt/xz/xiaojiujiu-beta --out /mnt/xz/xiaojiujiu-beta/reports
# 优化前先冻结基线
python3 ~/astrbot_test/src/xiaojiujiu/scripts/snapshot_beta.py --note "before tuning" \
  --root /mnt/xz/xiaojiujiu-beta/snapshots     # 同样挂上卷与导出盘运行
```

**为什么是这个钟点**：`events.created_at` 是 UTC，日报按 **UTC 日期前缀**过滤，而服务器是
CST=UTC+8。若在 CST 白天跑，`--date 今天(UTC)` 只能看到"从 CST 08:00 到现在"的半截；
所以定时任务固定 **08:05 CST**（= 00:05 UTC）跑，取 `--date 昨天(UTC)`——报告覆盖的是
一个**刚刚走完的完整 UTC 日**。代价：当天 00:00–08:00 CST（= 前一天 16:00–24:00 UTC）
的聊天要等第二天早上才进报告，急事直接看下面那个看板。

**看板地址（完整端点）**——宿主 `192.168.1.15`，控制面把 8800 发布在 `0.0.0.0`，
局域网内任何机器直接开：

| 端点 | 是什么 | 是否只读 |
|---|---|---|
| `http://192.168.1.15:8800/fleet/dashboard` | HTML 总览页，30s 自刷新（每人 health/端口/事件数/unresolved/未了事项/deep+explain 调用/重启次数/uptime） | 只读 |
| `http://192.168.1.15:8800/fleet/status` | 上面那张表的 JSON（`count` + `people[]`），`beta_daily_report.py --fleet` 也吃这个 | 只读 |
| `http://192.168.1.15:8800/fleet/routes` | 会话 → `http://runtime-fleet:<port>` 的路由表，插件同步的就是它 | 只读 |
| `http://192.168.1.15:8800/fleet/provision` / `restart/<port>` / `deprovision/<port>` | **会改状态**（建人/重启/删人） | ⚠️ 写 |

> ⚠️ 控制面**没有鉴权**，且 8800 发布在 `0.0.0.0` ⇒ 同一局域网内谁都能 POST
> `/fleet/deprovision/<port>`。测试期这样最省事；封测给外部人之前，要么把这三条写端点
> 收进 docker 内网（只留 dashboard/status/routes 对外），要么直接别在不可信网络里开。
> 各人的 8787+/8801 只在 docker 内网（`astrbot_test_test_net`），宿主没发布，从别的机器
> 打不到——想手工调某个实例，得进容器：`docker exec xxj-runtime-fleet python3 - <脚本>`。

**证据**：`runtime/tests/test_observability.py` 7 条；`scripts/mutation_observability.py`
4 条变异全部 KILLED（不写决策/不写曲线/不记录渲染/忽略全文开关）；runtime 全量
**1151 passed / 15 skipped**。测试栈实测：静置后自动采样、导出→回放→日报全链路通过。

> 测试计数的两次变化都对应新加的观测面：1141（JSON Output + 提示词契约 6 条）、
> 1150（深刷新账本 5 条 + 读回工具 3 条）、1151（refresh 计分板进 `/health`）。

**定时任务与看板的实测（2026-09-16 夜）**：
- crontab 已装（`5 8 * * *`，`systemctl is-active cron` = active），包装脚本与仓库副本
  **md5 一致**（`abb54b15d60009bf73919dc96d050c08`），并用 `env -i`（只给 PATH/HOME 的
  最小环境，模拟 cron）跑通：`exit=0`，日志落 `/mnt/xz/xiaojiujiu-beta/logs/2026-09-16_1535.log`。
- 三项控制面从**本机（Windows）**访问 `http://192.168.1.15:8800/fleet/dashboard` =
  `HTTP 200`，标题 `小九九 fleet`，14 实例全 `health=ok`；`/fleet/status`、`/fleet/routes`
  同样 200。
- **看板已加 refresh 计分板**（2026-09-17 凌晨）：`/fleet/status` 每人多出
  `deep_refresh_attempts / deep_refresh_settled / deep_refresh_degraded /
  last_refresh_reason / last_refresh_at`，dashboard 相应多三列。**当"有积压但从未结算过"
  时两格标黄** —— 封测里最该被抓到的形状就是它。实测真人那一行 `2 / 8`、`last refresh
  applied`；模拟用户 20001 则标黄（`3` unresolved、`0 / 0`）。
  数据源是 `/health` 新增的 `deep_refresh` 段（`observability.refresh_stats()` 的 SQL 聚合，
  进健康轮询不心疼）。
- **"调用→落库"闭环实测**：在网络内对真人实例（8801）调一次 `POST /v1/context`
  （`{"session":"default:FriendMessage:1670681411","trigger":"llm_request"}`）→ `HTTP 200`，
  返回 2901 字、`version=684`、四段（进入本轮前的长期状态 / 当前工作局势 / 必要记忆 / 时间连续性），
  库里 `context_rendered` **0 → 1**。即仪表本身是好的。
- 顺带澄清一个假警报：真人实例某次导出显示 `context_renders=0`，疑似注入没记录。实际是
  **早期（自动开通生效前）那些 `/v1/context` 打到了共享/回退实例**，后来该人被分到自己的库
  （卷里 `default-friendmessage-1670681411/` 的 mtime 22:44 就是那一刻），旧调用自然不在他的新库里。
  卷根只残留一个 `raw_events.jsonl`（容器默认路径的产物，443B），可以删。

> ⚠️ **埋点密度提醒（封测第一周要盯的第一个数字）**：一条用户消息**不产生** `decisions` 记录
> ——它把状态推进、把 `foreground_pause` 续上，但动机判决只在**内源轮次**里发生
> （`trigger=endogenous_round` / `foreground_pause` 那个提前 return 分支）。以 900s 上限估算，
> 一次刚聊完（pause ≈ 60s）+ 持续对话的时段，判决可能只有个位数/小时。真人实例 50 分钟里
> 12 提问 5 回复**只有 1 条判决**。所以日报里的"决策次数"要连着"对话轮数"一起读，
> 别把"她在聊天"误读成"博弈没跑"；真要加密样本，调 `CR_SCHEDULER__MAX_INTERVAL_SECONDS`。

### ✅ 曾经的封测主目标缺口：情绪曲线是平线 —— 根因是深刷新被一句提示词打哑（已修）

**现象**：日报里 14 个实例、含真人 12 问 5 答，`mood_valence` **全是 +0.000**，
`active_emotion_events = 0`、`emotion_explanations = 0`。真人那 12 条消息的语义状态是
**清一色 `unresolved / potential_relevance=low / reason=no_explicit_anchor`**，
已结算行 **0 条**，深刷新 `deep_refresh_id` 一个都没写上。

**机制（`runtime.py` 1188–1202 附近，架构补丁 v0.2 的有意设计）**：

```
settle_on_ingest → classify_event(粗分类)  ── 命中 → 结算 → settlement_to_evaluation → 情绪事件
                                          └─ 未命中 → record_unresolved(no_explicit_anchor)
                                                       └─ 注释原话："An unresolved event yields
                                                          no emotional after-effect yet."
```
即**未结算 = 零情绪事件**。而粗分类走的是 `semantic.py` 的关键词信号表
（`谢谢 / 想你 / 太累了 / 面试过啦 / 算了 / 随便 / 哈哈 …` 几十条），日常口语
（"调试好手上的东西就睡"、"你很好奇我在干什么吗"）**基本一条都不命中**。
于是每条真实消息都进"待理解"队列，回头结算只能靠深刷新（触发：积压 ≥ 4 或静默 ≥ 1h，
最小间隔 900s）。**未结算 = 情绪曲线平线**——不是"她今天心情没变"，是"系统没读"。

**根因（2026-09-16 夜，全部对着线上实例实测）**：深刷新每次都以
`{"ran":false,"reason":"empty_suggestions","degraded":false}` 结束。顺着四步定位：

1. 用 runtime 自己的 `build_request` 复现：**请求是好的**——12 条 unresolved 带正文、
   3 条 `key_quotes`、mood、`situation.facts`，10KB，provider 是 `remote_api/deepseek-chat`。
2. 直接问 provider：raw reply 只有 **212 字节**、六个字段全是空数组/空对象、
   `finish_reason=stop`、`completion_tokens=57`。**是模型主动选择"没什么可说的"，
   不是截断**（`max_tokens` 1024→4096，回复仍是同样的 57 token）。
3. 逐句 bisect 提示词，定位到唯一一句：**"证据不足时返回空数组或空对象，不要猜测。"**
   加上它 → 57 token 空结构；去掉它 → 1300+ token 的真实解读。
   这句本意是"不许编造"，但模型读成"看不懂就别解读"——而深刷新的全部价值就是解读
   "没锚点"的消息，**那句提示词正好把这条路径关了**。
4. 三种改写实测都通，取了最贴近原意、产出最多的
   **"不要编造输入中没有的事件、会话或时间戳。"**：
   1030 completion token、3 reinterpretations / 3 memories / 1 candidate /
   2 user-model / 5 psych。

**修复（commits：提示词两处 + JSON Output + 账本）**：
- 提示词防编造措辞改写（`DEEP_REFRESH_SYSTEM_PROMPT`、`EXPLAIN_STATE_SYSTEM_PROMPT`），
  并按 JSON Output 的硬性要求补 JSON 样例（官方要求提示词里含 `json` 字样与格式样例）。
- **JSON Output**（用户提议，采用）：provider 增加 `json_mode`
  （`response_format={"type":"json_object"}`），深刷新与情绪解释两条结构化调用都带上；
  `CR_SEMANTIC_JSON_MODE=0` 可关（给不接受未知 body 字段的网关）。它保证拿到合法 JSON，
  正好堵住这轮踩的"半截/空对象被静默当成有效结果"那条路。
- **提示词必须写全条目键名**：只列六个顶层字段名时，模型把事件 id 放在 `event_id` 上，
  而 grounding 只认 `sources/source_ids/source_event_ids` → 每条解读都被判
  `missing_sources` 丢掉，刷新"ran=true"却结算 0 条。现在样例里五种条目各带 `sources`。
- **`max_tokens` 要留足**：模型肯说话之后输出涨到 900–1200 token，1024 会把 JSON 从中间截断
  （JSON Output 只在**没被截断**时才保证合法）→ 整个刷新降级为空。fleet 现在设
  `CR_SEMANTIC_MAX_TOKENS=65536`（API 允许 1~384K，未设默认 8K；**只按实际生成计费**，
  上限不是开销）。这是配置项，不改代码。
- 6 条新测试锁住契约：JSON 字段真的发出去、能关、env 开关两条路都通、
  `{}` 是"安静的回答"而非降级、提示词必须写"不许编造"且不得再出现"证据不足"、
  样例里五个字段都带 `sources`。
- 复现/验证用的探针固化在 `scripts/fleet_probe_refresh*.sh`、`fleet_probe_prompt_*.sh`、
  `fleet_probe_max_tokens.sh`、`fleet_probe_provider.sh`、`fleet_probe_env.sh`、
  `fleet_probe_stop_seq.sh`——下次怀疑 provider 层，先跑这些，别重头猜
  （顺带排除了一条猜想：stop 序列没必要，模型本来就自然收尾 `finish=stop`）。

**端到端验收（2026-09-17 凌晨，全部在测试栈实跑）**：
- 真人实例 `1670681411`：12 条消息从 **12 unresolved / 0 settled** →
  两次刷新后 **12 resolved / 0 unresolved**，`interpretation_versions` 12 条、
  `reappraisals` 12 条、`memories` 3 条、`unfinished_matters` 1 条。
- 账本：`refresh_runs` 两行（`applied` / `forced`，`settled_events` 8 与 0）。
- 读回链路：`export → summary.json`（`refresh_settled_events` 等字段齐全）→
  `replay_session.py`（账本 + reappraisal 段）→ `beta_daily_report.py`
  （每人一行"深刷新：2 次尝试，结算 8 条，降级 0 次"；模拟用户 20001 显示
  "0 次尝试，但 3 条仍未结算 —— 没人回头读"）。

> ⚠️ 排查教训：`extract_json` 会"取第一个 `{` 到最后一个 `}`"。一个被截断的 JSON
> 因此能变成一个**合法但残缺**的对象，于是 `degraded=false`、`reason=""`，
> 报表上看起来像"模型选择了沉默"。看到 `empty_suggestions` 时，先确认
> `finish_reason` 与 `completion_tokens`，再看请求体。

**诊断怎么做**（都固化在脚本里，不要再手搓 curl）：
- 库里看曲线与语义：`QQ=<QQ> bash scripts/fleet_probe_instance.sh`、
  `QQ=<QQ> bash scripts/fleet_probe_semantics.sh`
- 强制跑一次深刷新：`QQ=<QQ> bash scripts/fleet_probe_refresh.sh`
- 看账本：`bash scripts/fleet_probe_ledger.sh`（换 QQ 改脚本里的库路径）
- 端到端：`bash scripts/beta_e2e_check.sh`
- 看主动消息的投递去向：`bash scripts/fleet_probe_outbox.sh`（outbox / action_attempts /
  attempt_events 三段，能看出"提交了但没送到"的具体一步）
- 看情绪/结算细节：`docker exec -i xxj-runtime-fleet python3 - < 一段脚本`
  （`docker cp` 往这个容器里拷文件会报 `Could not find the file /proc/self/fd`，用 stdin）

### ⚠️ 主动消息在"平台链路瞬断"时会被永久丢掉（已修）

**取证**（真人实例，2026-09-16T16:13 UTC）：一次 `hazard_triggered` 的主动尝试走完了
`proposed → committed → rendering → ready_to_send`，文案也写好了——
**"东西调试完就去睡吧。另外昵称那条我没太看明白，等你方便了再说，不急。"**
——然后 `attempt_events` 记：

```
ready_to_send → failed : ActionExecutionError: send_message failed: ApiNotAvailable:
```

`ApiNotAvailable` 来自 **`aiocqhttp.exceptions`**（"OneBot API 不可用"），即那一刻
**OneBot 连接做不了这次调用**——不是"消息被拒绝"，而是链路不通，**什么都没发出去**。
但插件把它包成 `ActionExecutionError` 上报为 `failed`，Runtime 的契约是
"任何非 ok 的 send 结果 → `mark_delivered(success=False)` → `nack(terminal=True)`"，
于是这次尝试被**永久关闭**，消息再也不会重发。

**根因是失败分类漏了一层**：插件对"Runtime 不可达"（授权拿不到答复）本来就有正确处置——
**故意不上报**，留租约过期让 Runtime 重投，并且 `tests/test_outbox.py` 里
`test_unavailable_authorize_leaves_the_action_retryable` 把这条契约写得很清楚
（"silence is the only shape of 'retry this later' the contract has"）。
但同样的道理没覆盖"**平台链路**不可用"这一层。

**修法**（插件仓库 `9b574f2`）：
- `companion_runtime/protocol.py` 新增 `TransportUnavailable`，以及纯函数
  `is_transport_unavailable`：**按异常的名字/模块判定，不 import aiocqhttp**
  （`companion_runtime` 必须能在没有 AstrBot 的环境里导入，这正是它能被单测的原因）。
  `ApiNotAvailable` 名字独特，按名字认；`NetworkError` 太通用（httpx 也有），
  只在它确实来自 `aiocqhttp` 模块时才认。
- `astrbot_executor.send`：传输不可用 → 抛 `TransportUnavailable`，不再混进
  `ActionExecutionError`。
- `outbox._send`：捕获 `TransportUnavailable` → **不产生任何回报**、`stats.deferred + 1`、
  记 warning，与授权不可达同一处置。真正的执行失败（平台拒绝 / 无 provider / `sent=False`）
  **仍然**上报为终态，避免把坏 session 无限重试。
- 测试 +3（延迟路径、反向保护、分类函数对同名异常的区别）；插件全量 **162 passed**（原 159）。

> ⚠️ 这条对封测很关键：**主动消息是产品的一半**，而"链路抖一下"在真机上是常态
> （NapCat 重连、QQ 会话切换）。修之前，每次抖动都会静默吃掉一条她已经决定要说的话，
> 而且账面上只留一条 `failed`，看起来像"她不想说"。

### 🔴 主动消息**从来没有一条送出去过** —— 根因是"两个 OneBot 客户端"(2026-09-17 定位并验证)

**现象**：6 个实例里 4 个触发了主动（`hazard_triggered` + `acted=1`），文案全部渲染成功
（`render = delivered`），**发送全部失败**。最早那条（09-16 16:13）是
`ActionExecutionError: send_message failed: ApiNotAvailable`；其余全是
`outbox_failed: lease expired` —— 而且**四次都是 137 秒**，正好
`lease_seconds 45 × max_attempts 3`。

**先说清楚两件事**：
1. **不是发错人**。对账测试前端 transcript 的 376 条 `send_private_msg`，**全是它自己的模拟号**
   （20001/20005/20010/20012/29999），真人该收的文案一条都没进去。是"卡在出口"，不是"投给别人"。
2. **也不是 QQ 掉线**。失败窗口中间（14:46）**有一条回复成功送出**。

**根因（AstrBot 侧，非我们代码）**：主动发送不带 `self_id`。

```python
# astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:103-105
routing_params = {}
if isinstance(event, Event) and event.get("self_id"):
    routing_params["self_id"] = event["self_id"]
```
- **回复**：有 event ⇒ 带上 `self_id` ⇒ aiocqhttp 按号找到连接（`api_impl.py:131`
  `self._api_clients[str(self_id)]`）⇒ 正常。
- **主动**：`send_by_session` 传的是 **`event=None`**（适配器注释："这里不需要 event"）
  ⇒ `routing_params` 为空 ⇒ 只能靠 `UnifiedApi.call_action` 在 `_wsr_api` / `_http_api`
  之间兜底，两者都 `ApiNotAvailable` 就 `raise ApiNotAvailable`。
  而本栈 NapCat 配置里 `httpServers: []`（没配 HTTP API）。

**为什么兜底会失败**：这个平台上**同时挂着两个 OneBot 客户端** —— 真 QQ（`xxj-napcat-test`，
反向 WS）和测试前端 `xxj-onebot`（self_id 10001，反向 WS）。"不指定发给谁"遇上"两个客户端"，
就挑不出来。

**验证**：`docker stop xxj-onebot` 之后，**第一条主动消息真的送达了**：

```
18:21:56 render leased → 18:22:08 render delivered → 18:22:09 ready_to_send -> sent → delivered
文案：“对了，之前你提到"同 qq 昵称"，我还没太对上——是指哪个昵称、要同步到哪里呀？不急，你方便再说。”
```

**现状与后续**：
- 测试前端**保持停止**（它已完成使命：13 个模拟号早已摘除）。封测期不要把它开回来 ——
  **一开回来主动投递很可能再次全灭**。
- **已加防护**：`astrbot.yml` 里给 `frontend` 服务加了
  `profiles: ["legacy-frontend"]`，所以裸的 `docker compose up -d` **不会**再把它带回来
  （只是 `docker stop` 挡不住 `up`，这一层拦住那个静默回归）。要跑模拟测试时显式启用：
  ```bash
  docker compose -p astrbot_test -f astrbot.yml --profile legacy-frontend up -d frontend
  ```
  校验过：默认 `config --services` 是 `runtime, astrbot, napcat-test, runtime-b`（不含 frontend），
  带 profile 才出现 frontend，其它服务未变。
- （同一类隐患，但无害，未动：`runtime-b` 也还在 compose 里，裸 `up` 会多起一个单实例
  Runtime —— 它不接平台，不会影响投递。）
- 真正的修法在**插件侧**：主动发送不要用 `context.send_message(umo, chain)`（它丢 self_id），
  而是解析平台实例、按回复路径的写法显式带上 `self_id`。这样就不依赖"平台上恰好只有一个客户端"。
  未实施。
- ⚠️ **样本只有 1 次**（改前 5 次全失败、改后第 1 次成功），机制解释与现象一致，但严格说还需复现。
- **人格热更新：本轮搁置**（用户 2026-09-17 决定"先算了"）。结论仍然有效、随时可用：
  人格存在 `data_v4.db` 的 `personas.system_prompt`，但 `get_persona_v3_by_id` 读的是
  **启动时加载的内存副本**，**直接改库不生效**；只有 `persona_mgr.update_persona()`
  会顺便重建缓存（`self.personas[i] = persona` + `get_v3_persona_data()`），
  所以**走 WebUI 人格页或 `PUT /api/personas/by-id` 就是热更新**，不用重启。
  三条交付路线（WebUI 手改 / 给密码脚本化 / 插件命令从文件热加载）留待以后挑。

### ⚠️ 已 failed 的 attempt 无法复活（设计不变量，试过两次）

想"只测投递这一跳"时走过这条路，结论值得记下：
- 把 outbox 行 `failed → pending` ⇒ 2 秒内又 `failed`，错误 `attempt_terminal:failed`。
- 再把 attempt `failed → ready_to_send`（附审计事件）⇒ **同一条错误**，2 秒内又 `failed`。
- 判定处是 `authorize.py:208`：`attempt.state in TERMINAL_STATES → "attempt_terminal:{state}"`，
  且它是**现读库**（`authorize.py:132`）。

⇒ **投递失败一次后，那条消息在系统里不可恢复**，只能等下一条新决定。这是"终态就是终态"的
刻意不变量，不是 bug。也正因如此，上面那个 `ApiNotAvailable` 修复的价值在于**让这种失败不再发生**，
而不是"事后能救"。

### ⚠️ 跳时钟做 hazard 实验的四个坑（都踩过）

为了在有限时间里触发一次主动，用 `POST /endogenous {"force":true,"now":"<未来>"}` 推时钟：

1. **一次 45 分钟只值 12%**：`P = 1 − exp(−hazard × Δt)`，`hazard ≈ 4.8e-5`。
   要到 90% 得 **Δt ≈ 13 小时** —— 无论怎么切片都躲不开这个总量（`hazard_base = 3e-5` 的设计代价）。
   实测 7 轮 × 45 分钟（累计 57% 把握）**一次都没中**，符合概率。
2. **冷却会挡住**：刚动作过就有 40 分钟 `cooldown_until`，`force` 只绕过前台屏障和调度闸门，
   **不绕冷却**（第一次强制就撞在 `cooldown_active` 上）。
3. **新 outbox 行的 `available_at` 落在未来** ⇒ 插件按挂钟 claim（`available_at <= now`）
   **永远领不到**，看起来像"插件不工作"。跳钟后必须把 pending 行的 `available_at` 拉回挂钟。
4. 跳完还要**调和时钟锚点**（`updated_at` / `last_tick_at` / `cooldown_until` /
   `meta.last_decision_at` / `meta.last_deep_refresh_at`），否则 `last_decision_at` 在未来会让
   hazard 区间算成负数，**她会被冻结十几个小时**。历史行（decision/attempt）保持原样，
   只在文档里说明那段是模拟的。

工具：`scripts/hazard_jump_13h.sh`、`scripts/deliver_after_jump.sh`、`scripts/reconcile_clock.sh`。
快照：`snapshots/2026-09-17_101227`（复活实验前）、`2026-09-17_101901`（跳钟前）。

### ⚠️ 自己埋的假报警：账本把"没调 provider 的跳过"记成了降级（已修）

`DeepRefreshOutcome.degraded` 默认 `True`（对调用方是保守的正确默认："没有可信建议"），
抄进账本就成了故障计数：真人实例 5 次 `not_needed / min_interval_not_elapsed` 的跳过
全被记成 `degraded=1`，日报会写"降级 5 次"——而 Runtime 只是选择不花钱。

修法：结果对象新增 `provider_called`（真正调用 provider 前一刻置 True），账本写
`degraded = outcome.degraded and outcome.provider_called`。两条测试锁住两侧：
`disabled` / `provider_unavailable` 必须 0；provider 抛异常必须 1 且 `ran=0`。

### ⚠️ 未尽之事被重复创建（看板查出来的，已修）

**怎么发现的**：按用户要求核对看板时，`open matters = 10` 这个数字不对——它不是 10 件事，
**是同样两件事各被创建了 5 次**：

```
来源事件只有 2 个：
  evt_0672dfff…  "🫡，当个事办"      → 5 条未完之事
  evt_82a2632f…  "同 qq 昵称"        → 5 条未完之事
创建时间 15:57 / 16:03 / 22:57 / 01:57 / 03:02（每次深刷新各一轮）
非 open 条数 = 0 —— 从来没有一条被解决过
```

**根因**：`unfinished.create` 是无条件新建（`new_id` + `upsert`）。规则路径有去重
（`detect(existing=subject_guards(...))` + `_same_subject`），但**深刷新那条
`reducer._apply_unfinished_suggestion` 完全没接**——而深刷新**每次都重读同一批 unresolved**，
模型就用新措辞把同一件事重新提一遍（措辞变化到标题比对抓不住），两条路也互不知道对方。

**为什么这不是小事**：一周按 ~10 条/天/人 无界增长（10 人 ≈ 700 条），而**每一条都会进注入块**、
还会参与候选的 grounding——等于往她的提示词里持续灌重复内容。

**修法**（`b17af1c`）：
- `unfinished.py` 新增 `already_spoken_for(title, sources, matters)`，两种重述都挡：
  ①**来源事件已经产出过一条**（深刷新的病根：同事件重读、措辞不同）；
  ②**主题已被占用**（`_same_subject`，覆盖"同一件事从另一个事件提出来"）。
- `reducer._apply_unfinished_suggestion` 先查 `subject_guards(list_all(200))`，命中则不建并返回
  False；调用方记 `skipped:unfinished_matter:already_spoken_for`、**不计入 applied**，
  但**仍把这批来源事件算作已理解**（它们确实被理解了，否则积压会永远为它重触发刷新）。
- 6 条新测试，其中一条是写测试时发现的**第三条路**：`"面试完告诉你结果"` 入站时就被**规则**
  建成 `"等待面试结果"`，深刷新再读同一事件又提一遍——两条路同时创建同一条，实测正是这样
  攒出 5 份的。
- 历史数据清理：10 条 → 保留最早 2 条，其余 8 条标 `invalidated`（`resolution_note` 指向保留项，
  写明是 b17af1c 之前的重复）。改前快照 `snapshots/2026-09-17_035608`。
- 部署后实测：`open matters 2`、`unresolved 0`、0 条 error，且重启窗口没有丢消息
  （03:59 那次刷新把 3 条待理解的结算了，对话历史连续）。

> 教训：**看板上一列"看起来只是有点大"的数字，值得追到底**。10 这个数不算离谱，
> 但它背后的形状（2 个事件 → 10 条记录、0 条曾解决）才是问题。这也是当初把
> `open matters` 放进看板的理由。

### 当前 runtime 用量（2026-09-17 凌晨实测）

- **容器只有两个**：`xxj-runtime-fleet`（一个容器里跑 14 个 Runtime 进程，控制面 8800）
  与 `xxj-runtime-test`（单实例回退，只在注册表不可达时兜底）。
- **真正承载真人会话的只有 1 个实例**：`default-friendmessage-1670681411`（端口 8801）。
  其余 13 个（`20001`–`20012`、`29999`）是验证留下的模拟号，各带 0–3 条种子消息。
  它们最近的 `last_event` 时间戳看着很新，是**内源调度在跑**（`candidate_proposal` /
  `proposal:deep_refresh` 等 system 事件），不是在收消息——别误读成"有人还在跟它们说话"。
- 资源：fleet 14 进程 **622 MiB / 4% CPU**（≈44 MiB 一个），回退实例 44 MiB。
  机器 15.5 GiB，封测 10 个真人完全不是问题。
- 回退实例里只有它自己的 `default` 会话，没有任何人被路由过去——即注册表一直可用。

### 移除模拟实例（2026-09-17 凌晨，已做）

- 动作：`POST /fleet/deprovision/<slug>` × 13（`20001`–`20012`、`29999`）。
  摘之前先冻结：`/mnt/xz/xiaojiujiu-beta/snapshots/2026-09-16_222134`（14 人）。
- 结果：`/fleet/status` 14 → **1**、`/fleet/routes` 与 `people.json` 都只剩
  `default:FriendMessage:1670681411`；真人实例（8801）健康、events 64、
  刷新 31 次/结算 8 条。`deprovision` 本身**只停进程、保留数据**。
- **卷也收干净了**：13 个目录移到 `/data/_retired/`（数据不删，但不再被扫到），
  并删掉 `/data` 根下两个探针空壳（`companion.sqlite3`、`raw_events.jsonl`）。
  导出随即变成 `found 1 runtimes`，日报只剩真人的一段。

> ⚠️ **踩到的两个真实缺口（都没改代码，先记下来）**：
>
> 1. **导出/日报是按卷里的目录 glob 的，不是按名册**（`export_beta_data.py` 的
>    `data_root.glob("*/companion.sqlite3")`）。所以 `deprovision` 保留在原地的人，
>    第二天会**照旧出现在日报里**——摘了等于没摘。本次靠"把它们移进 `_retired/`"绕过。
>    正经修法：导出改为以**在用名册**（`people.json` 或 `/fleet/routes`）为准，
>    另给 `--all` 兜底读历史。注意导出容器现在没挂 `fleet-data`，要走这条得一起改
>    `beta_daily_collect.sh` 的挂载。
> 2. **插件的注册表同步只加不删**（`_sync_registry_once` 里只有 `_ensure_target`，
>    没有移除路径）。摘掉的人会留下"僵尸 target"，其 outbox 轮询器**永远**去敲已经不存在的
>    端口，失败只在 debug 级别记（`outbox.run` 的 backoff 里），所以日志上看不见、但一直在做。
>    影响很小（十几条连接 + 退避），修法是"连续 N 次成功同步都不在注册表里才剪掉"，
>    不要一看到不在就删——注册表短暂返回不完整会把好 target 一起拆掉。
>
> ⚠️ 另一个坑（本次清理时才发现）：**用默认配置实例化 Runtime 会在镜像的默认路径建库**
> （`CR_STORAGE__DATABASE_PATH=/data/companion.sqlite3`）。我早期那次
> `load_config()` + `Runtime(cfg)` 的复现脚本没覆盖 storage 路径，就在卷根留下了一个
> 27 万字节的空壳库。写探针时必须显式指定 `cfg.storage.database_path`。

**摘人带出的三件事（都已处理）**：

1. **fleet 容器的健康检查被端口号绑住了（已修）**。镜像自带的 healthcheck 探
   `http://127.0.0.1:8787/health`，而在 fleet 容器里 8787 **只是"恰好占用第一个空闲端口的
   那个人"**。20001 被我摘掉后 8787 上没人监听 → 容器报 `unhealthy`，而它其实服务得好好的
   （控制面 200、唯一实例健康）。已在 `fleet.yml` 与生成器 `build_runtime_fleet.py` 里
   覆盖为探**控制面** `:8800/fleet/status`：这个容器的职责是 supervisor，就该为 supervisor
   负责；每个人的健康在 `/fleet/status` 与看板上。修完容器立刻 `healthy`。
   （教训：**别让容器健康依赖于"名单里恰好有谁"**——这种 false alarm 会把真警报淹掉。）
2. **端口会在名册变化后重排**：名册清空后真人从 **8801 挪到了 8787**（`_next_port()` 取最小
   空闲端口）。路由不靠端口号、靠注册表（`/fleet/routes`），插件下一次同步（≤5s）就跟上了；
   实测该实例日志里 `POST /v1/outbox/lease` 由 AstrBot 持续打进来（27436 次）。
   **永远别把某个人的端口写死**，要用 `/fleet/status` 现读。
3. **历史账本订正**：修复前写入的 21 行"跳过"被错记成 `degraded=1`，会把第二天第一份日报
   写成"降级 21 次"。已按"`reason` 属于从未调用 provider 的几种 **且** payload 里没有
   `provider_called`（新代码必写该字段）"这条件订正为 0，改前又冻结了一份
   （`snapshots/2026-09-16_222912`）。`/health` 的 `degraded` 随之从 21 变 0。

**这一份日报就是"正常应该长什么样"的参照**（`reports/2026-09-16.md`，只剩真人一个实例）：

```
- 对话：12 提问 / 5 回复，注入渲染 1 次
- 决策：30 次（主动 1 次），原因分布 {'no_candidate_beats_silence': 3, 'hazard_triggered': 1,
        'cooldown_active': 2, 'hazard_not_triggered': 24}
- 候选：7 条新建；语义：12 条，其中 unresolved 0
- 深刷新：31 次尝试，结算 8 条，降级 0 次，原因分布 {'applied': 7,
          'min_interval_not_elapsed': 3, 'not_needed': 21}
- 状态曲线（30 个采样点）：valence +0.000→+0.000, impulse 0.110→0.380, restraint 0.548→0.677
```
注意 `valence` 仍是平的（第 3 节的机制：结算不产生情绪事件），而 `impulse`/`restraint`
是在动的——封测第一周要盯的就是这两个：**"想说话"的冲动在涨、"克制"也在涨**。

### ⚠️ 宿主坑：AstrBot 把 OneBot 的「通知」包装成空正文的消息事件

真机取证（真人 QQ，14 秒内 8 条 `user_message`，其中 7 条正文为空、`message_id` 是 UUID、
`sender_name` 退化成 id）。根因在宿主侧：
`aiocqhttp_platform_adapter.py::_convert_handle_notice_event` 把 OneBot 的 notice
（**戳一戳**、好友请求、群成员变动）包装成 `AstrBotMessage`：

```python
abm.sender = MessageMember(user_id=str(event.user_id), nickname=str(event.user_id))  # 昵称=id
abm.message_str = ""            # 正文为空
abm.message_id = uuid.uuid4().hex  # 非数字 id
```

后果（插件修复前）：Runtime 把 7 次戳一戳记成 7 条"用户什么都没说"，
`working_situation_items` 里出现空的「用户说：」，每条还各触发一次前台处理并压住主动派发。

**修法**：正文（`strip()` 后）为空的消息一律不上报，并在 `/companion_runtime`
里报一行 `skipped empty events: N`（否则它又是一个静默丢弃）。
若将来"戳一戳"本身值得进入认知，应在协议里给它一个自己的 event kind，而不是让插件编造文本。
测试 `test_an_empty_message_event_is_not_reported`；变异 M10（空事件照常上报）KILLED。

### ⚠️ 又一个只能在真机上现形的 bug：fleet 把控制面端口发出去了

`8787 + 13 = 8800`，而 8800 是 fleet 控制面自己的端口。第 14 个人（真号 `1670681411`）
被分到 8800 → 他的 Runtime 绑不上端口，注册表却已登记 `http://runtime-fleet:8800`
→ 插件的 `/v1/events` 打到控制面（404）→ **他的第一条消息一条都没落进自己的库**。
修法：`_next_port()` 同时排除 ①已分配端口 ②控制面端口 ③当前 bind 不上的端口。
**封测名单超过 13 人就会撞上，务必保留这个修复。**

### 已修并验证（本节点）

**#2 记忆种类由子串决定** —— `runtime/src/companion_runtime/memory.py`
- 现象：`PREFERENCE_MARKERS` 里有光秃秃的单字 `最`，于是"其实我**最近**挺难的…"被存成
  `kind: user_preference`（实测 2.5 个月运行里 4 行）；偏好分支还是三条分支里**唯一没有
  `is_question` 守卫**的，于是"我喜欢你这件事情，你还记得我说过吗？"被存成持久偏好。
- 改法：删掉 `最`（单字不成词，`喜欢` 已覆盖"最喜欢/最爱"）、删掉 `别再`/`不要`（那是**对角色下的
  指令**，不是用户的偏好——边界声明被存成偏好事实就是这么来的）；给偏好分支补上 `is_question` 守卫。
- 测试：`runtime/tests/test_memory_kind_markers.py`（4 条，含"真偏好仍是偏好"的反向保护）。
- **已知残留**（诚实记录）：`一直`/`总是`/`从来` 是**习惯**标记，但指令句里也会出现
  （"不要一直追问"），所以含这类副词的指令仍会被读成习惯 → 要根治需要"指令框架"识别，不能只靠标记表。
- 另一处同类风险**未动**（不在本次报出的范围）：`TRANSIENT_MARKERS` 里也有单字 `吃`/`嗯`/`哦`，
  它只影响"是否值得记"（更保守的方向），但 `吃` 会命中"吃不下饭"这类真实心事。

**#4 话题边界主体绑错** —— `runtime/src/companion_runtime/boundaries.py`
- 现象：`referent_for` 的第三步**无条件**返回"上一句话"（`return summarize_text(text, 40)`），
  于是"有件事想说清楚，不要一直追问我在干嘛"被绑到它前面那句礼貌语
  "谢谢你听我说这些。"——**零 bigram 重叠**。后果是这条边界**看起来在生效**，
  而 `blocks_candidate` 永远匹配不上，声称保护的主体其实毫无保护。
- 改法：删掉第三步（它与自己的 docstring "绝不猜测" 矛盾），docstring 改写并写明这次实测。
- 测试：`runtime/tests/test_boundary_referent.py`（礼貌语不得被绑 + 主体必须是那个未尽之事的标题）；
  旧测试 `test_the_referent_falls_back_to_what_was_said` **按新契约反转**（原名与新期望都留在注释里）。
- 实测证据：四套仿真复跑 **77/335/25/105 全绿**，且新仿真 `inspection.md` 里
  **`boundary_subject_mismatch` 已消失**（此前每次跑都会报）。
- **未验证**：绑定链的第二步（"讨论过、与未尽之事标题文本重叠"）我构造了一个用例**没绑上**，
  没查清是标题取了整句还是阈值没到，因此**没留假装验证过的测试**，把问题记在这里。

### 未完成（异地继续时从这里接）

**#1 原始事件镜像不完整**（最严重，**已修 + 已验证**）—— `runtime/src/companion_runtime/eventlog.py`
- 现象：`backend/raw_events.jsonl` **320 行** vs `/health.raw_events` **393**；
  **全部 51 条 `user_message`、11 条 `proactive_sent`、11 条 `assistant_message` 都不在镜像里**，
  且无任何警告。镜像 docstring 与 `runtime/README.md` 都承诺它是事件日志的忠实前缀。
- **根因**（打点探针定位，非猜测）：`EventLog._release_frame`。保存点释放时要把排队的事件交给
  **外层**事务，而"外层"是按"depth-1 上已注册的 frame"查的——**只有该层自己先记过事件才会有那个
  frame**。而用户消息是**在两层深（保存点里）**追加的，且往往是那个保存点里**唯一**的事件，
  于是查找返回 `None` → **整批行被静默丢弃**。打点原文：
  `after_commit type=user_message depth=2 ... frame_after=new pending=1` → `RELEASE frame pending=1`
  →（无后续）→ 只有后来 depth=1 的 `system` 被写出。
- **改法**：外层层若还没有 frame，**就地为它建一个**并让释放的行排队等它提交；
  只有在"根本没有外层事务"（数据库层不会产生这种 release）时才直接写，**且是写而不是丢**。
- **测试**：`runtime/tests/test_event_mirror.py`——库内 vs 镜像**按 event_id + type 逐条比对**
  （不只比数量），并点名 user_message / proactive_committed；另有一条对照（关镜像时什么都不写）。
  注意：夹具 `conftest.build_config` 默认 `mirror_raw_events=False`，必须显式打开。
  这条测试**先以 `xfail(strict=True)` 写过**——缺陷在时是"预期失败"，修好后变 XPASS 并报错逼人摘标记，
  事实正是如此，标记已摘。
- **变异证据**：把该分支换回 `return`（修前行为）→ 测试立刻红：
  `events absent from the mirror: {'evt_...': 'user_message'}`；恢复后绿。
- **端到端证据**：四套仿真复跑 **77/335/25/105 全绿**，且关系仿真 `inspection.md` 里
  **`jsonl_mirror_incomplete` 整节消失**。
- 遗留（不在本条范围）：镜像**写失败**时仍是"记一条警告"（`_write_mirror_or_warn`），
  契约上"忠实前缀"已成立，但"失败是否应让 `/health` 可见"没有做。

**#3 + #5 用户提问进长期记忆 / 提问式记忆挤占 4 个名额**（**已实现 + 已验证**）

- **口径**（用户已确认）：命题进记忆（剥掉疑问框架），疑问框架本身作为独立的"关系证据"保留，
  且不得占用那 4 个名额。
- **实现**（5 个文件，约 40 行；`MemoryCandidate` 新增 `structured` 并穿透四层）：
  1. `typing.py`：`MemoryCandidate.structured` + `to_dict()`。
  2. `db.py`：`memory_candidates` DDL 加 `structured_json`；`ADDED_COLUMNS` 加一行；
     **`JSON_COLUMNS["memory_candidates"]` 也要加**——这一步清单里没写，但漏了它
     `structured_json` 读回来是字符串而不是 dict（`row_to_dict` 靠这张表决定解哪些列）。
     PG 侧复用同一份 DDL 与列表（`db_postgres.py` 直接取 `_SqliteDatabase.ADDED_COLUMNS`），
     所以只改 `db.py` 一处。
  3. `projections.py`：`upsert_candidate` 的列清单/VALUES/`ON CONFLICT SET` 三处 +
     `_to_candidate` 解码。
  4. `memory.py`：`proposition_of()` 剥框架；`propose_from_event` 存命题并把框架写进
     `candidate.structured["recall_check"]`（命题与原句不同时另存 `raw_text`）；
     `consolidate` 合并 `candidate.structured`；**重复候选那条路也要合并**（`_find_duplicate`
     命中时只强化已有记忆，不并 structured 的话框架会在那里丢掉）；新增 `is_recall_check()`。
  5. `context.py`：`select_memories` 的 `add()` 跳过 `recall_check` 记忆——这是**所有来源共用
     的发名额点**，所以一行覆盖四个来源；`MemoryStore.retrieve` 不动（仍可被线索召回）。
- **一个设计判断是测试逼出来的**：`FRAME_ONLY`（"你还记得我跟你讲过它吗？"）剥完命题为空。
  契约要求**原样保留摘要**（绝不产出半句话），但第③条又要求它**不能占名额**。所以
  "框架探测到"和"命题剥成功"必须解耦：摘要原样留，`recall_check` 照记。
  另加一道守卫：只有**疑问句**才认框架——"我记得你说过喜欢我"含"记得"但是陈述句，
  误判会把它当成提问，从而把一个真实披露藏出提示词。
- **验收**：`runtime/tests/test_question_memories.py` 三条 `xfail(strict=True)` 全部转正，
  标记已摘。
- **变异证据**（三处，各自精确打红对应测试）：
  | 变异 | 打红 |
  |---|---|
  | 拿掉 `select_memories` 的名额排除 | ③ |
  | `propose_from_event` 还原成存原句 | ① ② |
  | `consolidate` 不合并 `structured` | ② ③ |
- **⚠️ 过程中发现：那条"不占名额"的验收测试原本是不可证伪的。** 它写的是
  `getattr(item, "summary", "")`，而 `select_memories` 返回的是 **dict**——对 dict 取属性
  永远拿到默认值 `''`，于是 `shown` 恒为空串，`"你还记得" not in ""` 恒为真。
  **变异测试（拿掉排除）没有打红它**，才暴露出这一点。已改成 `item.get("summary", "")`，
  改完变异立刻咬人。
  教训：`xfail(strict=True)` 只抓"意外通过"，**抓不到"永远不可能失败"**——而一条长期挂着的
  预期失败正是这种缺陷最舒服的藏身处。这个文件里原本还有一个同类问题：`cue` 传的是裸字符串，
  而 `retrieve` 要 `RetrievalCue`，所以它连代码路径都进不去。两条都已修。
  修法上用 `item["summary"]` 而不是 `.get("summary", "")`：后者同样带默认值，键一改名
  又会静默变回空串；下标取值会让下一次形状变化**报 KeyError 而不是悄悄变绿**。
  排查过全量测试，同形的 `getattr(x, "字面量")` 只剩两处，都是真属性访问（函数的
  `__code__`、sqlite 错误的 `sqlite_errorname`），不是 dict 键。
  实用技巧：对挂久了的 `xfail` 跑一次 `pytest --runxfail`，它按普通测试报告真实失败原因——
  "因缺陷而红"与"因测试自己坏了而红"普通跑都是 `xfailed`，分不出来，`--runxfail` 一跑就分得清。

#### 顺手做的一轮"空断言"排查（同一家族）

`all([])` 是 `True`、`not any([])` 也是 `True`，所以 **`assert all(...)` / `assert not any(...)`
对空集合会静默通过**。这是同一家族的第三个面孔：**报告说绿，实际什么都没检查**。
（`assert any(...)` 对空集合是 `False`，会红——那是误报，不是漏报。）

按这个筛全量测试，危险形式共 24 处。逐处核对集合是否保证非空后：

- **大部分写对了**，而且作者**已经建立了守卫惯例**：`emotion_boundaries_unfinished`、
  `delivery_scheduler_context`、`integration_scenarios`、`topic_boundary_gate` 里都有
  `assert decision["utilities"]` / `assert boundaries` / `assert situation["facts"]` 这样的前置断言
  （`motivation.py` 里也有 `elif all(...) and assessments:` 这种显式兜底）。
- **7 处漏了守卫**，已补齐（沿用同一惯例，各一行）：
  `test_candidate_motivation.py` ×2（`result.assessments`）、
  `test_integration_scenarios.py` ×3（`decision["utilities"]` ×2、`stored` 边界 ×1）、
  `test_emotion_boundaries_unfinished.py` ×2（`decision["utilities"]`）。
- **变异证据**（两处变异，因为同一份数据有两种表示，分别构建）：
  | 变异 | 打红 |
  |---|---|
  | `MotivationResult(assessments=...)` 置空 | `test_candidate_motivation.py` 的 2 条守卫 |
  | `utilities=[a.breakdown ...]` 置空 | `integration_scenarios` + `emotion_boundaries` 共 6 条 |
  顺带发现：`DecisionOutcome.utilities`（`typing.py`）与 `MotivationResult.assessments`
  是**同一份数据的两处构建**（`motivation.py` 921 行 vs 三处 early return），所以一条变异
  只能打到其中一边——这本身不是缺陷，但排查时必须两边都打。
- **回归**：全量 1053 passed / 17 skipped / 0 failed；四套仿真 **77 / 335 / 25 / 105 全绿**
  （黑盒、韧性、记忆质量、关系递进）。老库原地升级实测：删掉 `structured_json` 列模拟旧库，
  重开补列成功，**旧行读回 `structured={}`**，不崩不丢。

### 本轮的方法论教训（重要）

- 我派了三个子代理做 #1/#2+#3/#4，**它们在 8 小时内没有产出任何文件改动**（工作树始终干净），
  最后我收回工单自己改。**异地继续时不要重复这个做法**：这四条里 #2/#4 是"改一处 + 加测试"的
  小活，直接做更快。
- 另：提交前对**全仓**扫一次 `git grep -n "MUTATION\|PROBE"`——我在 `6f2b146` 里误带了子代理
  正在树里的临时变异（`GET /schedule` 又被加回推进时钟的 tick），已用 `79e66ee` 更正。

---

## 聊天窗口：持久对话记录（backlog ③-1，已完成）

**病**：`framework/README.md` §8 里列着"历史跨会话持久化"——`readline` 只记得操作员**敲过**的行，
且活不过进程：重开 `cf chat` 对话就没了，`/session work` 两个会话也分不出来。对一个主张"长期连续性"
的项目，操作员看不到昨天那段对话，就没法判断这个东西到底成不成立。

**改法**（只动框架，不碰原程序）：

- 新增 `framework/cf/history.py`：JSONL 追加式记录，**一行一个 turn**（`at`/`kind`/`session`/`text`），
  与 `trace.jsonl` 同形，可直接 `tail`/`grep`。
- `ChatTUI` 接上它：用户说的、角色回的、主动发来的都写；重开窗口回显本会话最近 `recap` 条；
  新增 `/history [n]`；`/session` 切换时**按会话**重建 `readline` 的上翻历史（不这么做，
  上翻会翻出另一个会话的行，比没有 recall 更糟）。
- 配置：`[harness].history` / `[harness].recap`，命令行 `--history` / `--recap`。
- **不放进 `run_dir`**：`run_dir` 带时间戳、每次换一个，记录活不过重启就不叫记录。默认放在
  配置文件旁边；`--history` 显式给出时以它为准。

**一条设计不变量，做成了构造而不是纪律**：只记可见的三类。运行时的隐藏背景块**永远不写进来**
（§2.6 / §86.10）。`record()` 对白名单以外的 `kind` **直接抛 ValueError**——因为这种写入一旦发生是
**静默**的：文件里多了一段不该有的内容，没有任何症状，只有下次有人读它时才发现。
变异 H1（把守卫改成 `if False`）就是专门验证这一点的。

**另一条容易错的地方**：流式打印与投递回调看到的是**同一段回复文本**，两边都写就让每个回答翻倍。
用 `_reply_recorded` 标记让一条回复只落一次；又保留"没经过 `_say` 的回复"（别的终端、`cf say`）
由回调记录，否则那种回复会丢掉。两种情形各有一条测试。

**测试为什么都在 `tty=False` 下跑**：回显只在 tty 下自动发生（管道里重放一段旧对话只会污染抓下来的
转写），但**记录**与命令处理与 tty 无关——那才是会错的部分。

**证据**：`framework/tests/test_chat_history.py` 22 条（12 条存储层 + 10 条 TUI 集成）；
`scripts/mutation_design_conformance.py` 的 `chat_history` 组 6 个变异全部 KILLED
（守卫失效 / 回复记两遍 / 读时不筛会话 / 空行也算一条 / 坏行直接抛 / 用户那侧不写）；
全仓 **31 个变异全部 KILLED**。框架全量 **293 passed**（原 271）。

**顺带**：harness 现在支持跨根跑测试（`framework_tests/...` 从前缀映射到 `framework/` 下的
`tests/...` 并在该目录执行），所以框架的验收测试也能进同一套变异框架。

---

## 崩溃窗口（`committed != sent`，0.3.2 进行中）

设计 §69/§86.9 的窗口：**平台真的发出去了**这件事只有 host 知道；host 在"发出"与"上报"之间死掉，
Runtime 手上就只剩一行 `leased` 的 send 与一个停在 `ready_to_send` 的 attempt。

### 做了什么：让歧义可见（Runtime 侧）

租约过期后 `outbox.reclaim_expired`：`attempts < max_attempts`（默认 3）就重新入队，否则该行
`failed`、attempt 被 `close_settled_outbox_attempts` 终结。**所以 send 是"至少一次"，最多重复
`max_attempts` 次**——这是刻意的（反方向会在"领取后立刻崩溃、消息根本没发"时静默丢意图，
而 Runtime 分不出这两种崩溃）。

**实测**（`lease_seconds=45`）：
```text
第1次领取: attempts=1  过期后 pending attempts=1
第2次领取: attempts=2  过期后 pending attempts=2
第3次领取: attempts=3  过期后 failed  attempts=3
attempt 最终: failed
```

问题不在于重发，在于 **host 连"我正在被重发"都看不出来**：`attempts` 只被编进 `lease_id` 的第三段
（`{adapter}:{outbox_id}:{attempts}`，那是防旧租约续期的失效令牌），协议里没有这个字段。
于是 host 既不能记录这个歧义，也无法实现任何策略。

**改法**：`POST /v1/outbox/lease` 的每个 item 增加 `attempts` 与 `redelivery`（`attempts > 1`）。
它读的是**领取计数**，所以 render 行是同一个口径。新增 `runtime/docs/REDELIVERY.md`：讲清窗口位置、
至少一次/至多一次两种 host 策略，以及**至多一次必须先落盘再发送**的顺序要求（顺序反了只是把窗口
挪个位置）。也写明插件当前的重试队列是**内存**的（`main.py::_report_action` 失败即 `queue.put`，
进程一死就没），所以它现在只能做至少一次；要做至多一次得换成落盘队列，那是插件侧的独立工作。

**证据**：`tests/test_redelivery_visibility.py` 4 条（含"第一次领取不得标成 redelivery""计数与
标志必须一致""标志不是 send 专有概念"），`scripts/mutation_design_conformance.py` 的 `redelivery`
组 4 个变异全部 KILLED（含"标志恒 False""字段整个不输出""计数写死 1""差一错误"）。

**为什么这条测试必须用真时钟**：`/v1/outbox/lease` 自己盖 `utcnow()` 且内部 tick 有 5s 限流，
所以租约过期没法用 `lazy_tick(BASE_TIME+…)` 伪造（会撞上单调时钟钳制，行永远不回到可领取）。
测试把 `lease_seconds` 压到 0.05s，用 `reclaim_expired` 只补"租约已过期"这一步，
整个文件跑 0.7s。

### 两个有意留着的判断（有实测证据，没动代码）

同一窗口的两条间接后果，**两个方向都说得通，属于产品判断**，所以只记录不擅自改：

1. **耗尽后 attempt 落成 `failed`，用户真的回复了也不被归属。** 回复归属只看最新的 `sent`
   attempt，而静默清扫也只扫 `sent`。实测：
   ```text
   attempt: failed   candidate: active
   用户发言后 observations: 0 -> 0   该 attempt 有 observation: False
   ```
   后果是：一条**真的送达、用户也真的回了**的消息，对用户模型的教学量是 0。
   把它当"未知"去归属，会在消息其实没送达时把用户的一句普通发言误记成回复。
2. **attempt 终结了，candidate 仍 `active`。** `close_settled_outbox_attempts` 的 docstring 曾把
   "candidate stays active"列为**它要修的泄漏后果**，但实测终结 attempt 后 candidate 仍是
   `active`，之后某一轮会被重新选中 → 同一件事被再说一次（意图层重来，不是传输层重发）。
   退役它会让"从未送达"的意图消失；留着它会在"其实已送达"时重复。

两处的 docstring 已改成与实测一致，并指向 `docs/REDELIVERY.md` §5。

---

## 设计一致性清单（0.3.2 进行中，一次修一条）

**起因**：把仓库里那两份设计文件（`内源主动型长期陪伴AI_Runtime_完整架构设计.md`、
`PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`，与外部传来的副本 sha256 一致）
逐条对着代码核了一遍。核完先更正了三处**审计文档自己写错**的状态：

| 审计原文 | 代码现状 | 证据 |
|---|---|---|
| #2「[在飞]」9 个写库入口不推进时间 | **已修** | `LAST_DECISION_META_KEY` + `_record_decision`/`_last_decision`，`tests/test_hazard_anchor.py`，落 `6f2b146`。§50「不依赖心跳频率」正是该修法的依据 |
| #5「[部分]」runtime 侧接线待落 | **已修** | `_refresh_candidates` 已把 `observations/emotions/situations/boundaries/recent_events` 交给 `generate`；`tests/test_candidate_shapes_wiring.py` 用 spy 钉住 |
| #7「[部分]」`invalidated_by_source_state` 未接线 | **已修** | 文本匹配返回 None 时 `runtime.py` 继续问来源级判据 |

**待办（按设计依据的硬度排序，一条一条修）**：

| | 问题 | 设计依据 | 状态 |
|---|---|---|---|
| ① | 观察侧 / 预测侧的行为特征编码不一致 | §22.1 / §25 / §27 | **已修**，见下 |
| ② | ~~深层刷新的 8 条触发规则没有 tick 内调用方~~ | 补丁 §21 | **撤回——误判**，见下 |
| ③ | ~~重解释不派生四类下游、也不发 `EventType.REAPPRAISAL`~~ | §67 | **大部分撤回**，只剩"`reappraisals` 没有读取方"并入 ⑦ |
| ④ | ~~§86.10（隐藏心理上下文不进永久历史）在两个仓库都没有断言~~ | §86.10 | **撤回——误判**，见下 |
| ⑤ | `DEFAULT_THETA` 注释自称 symmetric/agnostic，字面值不对称 | §31 | **已修**，见下 |
| ⑥ | §22.1「消息长度」是死字段 | §22.1 | ① 里已从编码器移除；**是否作为真特征**见下 |
| ⑦ | 死旋钮 / 零调用函数清理 | — | **已做（旋钮有意保留）**，见下 |
| ⑧ | 黑盒仿真 teardown 把 `.pyc` 变更算成"源文件被改"（改完源码后第一次跑必假红） | — | **已修**，见下 |
| ⑨ | **变异框架会留下陈旧字节码，使"已还原"的源码仍按变异体运行** | — | **已修**，见下 |

### ⚠️ 核实后撤回的三条（②③④）—— 教训写在这里

我第一版清单里的 ②③④ **是错的**，而且错法完全一样：**我用自己想出来的词去 grep，而不是读那段
代码的属主，并且直接信了审计文档的状态标签**。逐条交代：

- **② 不是缺陷。** `runtime.py::endogenous_round` 第 1475 行就写着
  `if deep_refresh: outcome.deep_refresh = self.deep_refresh(now=stamp).to_dict()`——
  tick **本来就**会跑一次触发判定。`tests/test_refresh_scheduling.py::TestRefreshRunsUnattended`
  5 条测试一直存在，文件名字面上就叫"refresh scheduling"。我之所以没看到，是因为我
  `grep deep_refresh runtime.py | head -30` 被断管截断，恰好只看到方法定义那一处，
  又从没打开过那个测试文件。`docs/PATCH_V0.2_MAPPING.md` 也把这条记成"已修复"。
- **③ 大部分不成立。** 设计 §67 说的四类下游（情绪更新 / 未尽之事 / 候选检查 / 用户模型重归因）
  **已经存在**：它们是 `reducer._apply_deep_refresh` 处理的六种 operation kind
  （`candidate_intent`/`unfinished_matter`/`user_model_evidence`/`psychological_interpretation`
  等），而深层刷新是重解释的唯一生产者。`reappraisals` 只是这条链的审计留痕。真正剩下的只有
  "这张表没有读取方、`EventType.REAPPRAISAL` 枚举成员从未被发出"——并入 ⑦。
- **④ 不是缺陷，而且实现得很对。** 插件 `main.py::_inject_context` 用
  `TextPart(...).mark_as_temp()` 注入，并且在 `mark_as_temp` 不可用时**拒绝注入**并只警告一次
  （"Without mark_as_temp the hidden context could be written into permanent history, which the
  architecture forbids. Skip instead."）——失败关闭，正是应有的形状。断言也有：
  `tests/test_plugin_integration.py::test_context_is_injected_as_a_temporary_part` 直接断言
  `part._no_save`。我 grep 的是 `hide_ctx` / `context_pollution` 这种**我自己编的词**，
  自然一条都搜不到。

**教训**：要断言"没有调用方/没有生产方/没有测试"，必须给出**文件级**证据（读那个属主的函数体、
读那个名字最像的测试文件、`ls` 一遍测试目录），而不是关键词命中为空。审计文档的状态标签同样
不可信——本文件 §"状态校正"一节已经列出它标错的另外三条（#2/#5/#7）。

### ⑤ 先验注释与它自己的字面值（已修，0.3.2 进行中）

原来的注释写 "symmetric where the Runtime should stay agnostic"，但**每一列都是非零的**——
乘在一个特征上的信念从来不可能是中性的。设计 §31 也只授权"低风险安全探索"，没有说过先验应当对称。

修法不是把先验清零（那会抹掉冷启动的探索倾向，属于行为变更），而是**让那句话可执行**：

- 注释改成陈述真正的性质：**没有 suspicion ≠ 没有 opinion**；冷启动 `boundary_risk` 实测
  **0.175**，远低于 `utility.conservative_risk_threshold`（0.30）；风险只因**已知边界**
  （`after_boundary` +1.60）或**确证忙碌**（`busy` +0.30）上升。设计 §31 的"低风险安全探索"
  因此是**先验里的低风险**，而不是一个许可开关。
- `tests/test_user_model_priors.py` 5 条把这个说法变成断言：
  「带意见的特征必须有写下来的理由」（`DOCUMENTED_PRIORS` 与"哪些列非零"必须相等——**新增或
  抹掉一条先验而不改理由就红**）、冷启动不得把首次接触判成可能越界、边界与忙碌必须抬升风险、
  `explicit_permission` 必须是最强正向、`recent_contact_ratio` 必须**比它更强地**是负向
  （后两条是设计 §26.1 / §29 点名的两个极端证据）。

**证据**：5 条测试 + `priors` 组 7 个变异全部 KILLED（含"把 novelty 抹成 0 却不改理由"、
"让冷启动变可疑"、"让疲劳比许可更弱"）。

### ⑦ 声明了却没有生产者的东西（已做；旋钮有意保留）

**先重新推导清单，不用审计那份**（它的数字有两处对不上：12 个零调用函数里有些是 argparse
目标、`__repr__`、以值传递的回调，以及一个和属性同名的模块函数；5 个 `EventType` 孤儿里
`REAPPRAISAL` 有设计依据）。新增 `scripts/dead_code_inventory.py`：A 段=无调用方函数、
B 段=无读者配置项、C 段=从未发出的 `EventType`；**排除项全部打印**，所以"没有调用方"这句话
可以被人复核，而不是关键词没命中就算数。

- **删掉 16 个零调用函数**（`reconcile_candidates`、`render_constraints`、`delivery_window_open`、
  `clamp_window`、`describe_pool`、`row_timestamp`/`row_bool`/`row_time_columns`、`encode_payload`、
  `empirical_impulse_half_life`、`_latest`、`update_intensity`、`worker_id`、
  `_has_pending_observation`、`weighted_mean`、`contains_any`）。A 段现在报 **0**。
- **`EventType` 孤儿从 5 个收敛到 0**，两条路各走一半：
  - **删掉 4 个**：`TICK`（tick 不是关于用户的事实）、`USER_MODEL_SUMMARY`（摘要存在
    `user_model_params.last_summary_json`）、`EMOTION_EVENT_EVAL`（设计里的
    `emotion_event_eval` 是 **task_type**，已有 `TaskKind.EMOTION_EVAL`）、`MEMORY_CONSOLIDATED`
    （巩固写的是记忆本身，设计里没有这个事件）。
  - **给 `REAPPRAISAL` 一个生产者**（这是 ③ 的真正残留）：`reducer._apply_reinterpretation`
    现在同时写投影行和 `EventType.REAPPRAISAL` 事件，两者互相指认（事件 metadata 里带
    `reappraisal_id`、`interpretation_id`、`target_event_id`、`supersedes_id`）。
    顺带修掉同一条路上的两个小问题：重估记录的标识符原先用 `new_id("memory")` → `mem_` 前缀
    （现在 `ID_PREFIXES["reappraisal"] = "rap"`，因为记录共享一种 `<prefix>_<hex>` 形状但**不可
    互换**，grounding 就是靠前缀判断标识符指代什么的）；provenance 里目标事件被重复列两次
    （现在 `dict.fromkeys` 去重）。
- **新增 `tests/test_declared_but_unused.py`（3 条）**把这件事变成常驻断言：每个 `EventType`
  成员都必须有生产者，或者出现在显式的 `HOST_WRITTEN` 白名单里（`TOOL_RESULT`——`POST /events`
  接受任意 `event_type`，所以它协议可达）；白名单不许留过期条目；并且**单独钉一条**
  `REAPPRAISAL` 必须存在且被 reducer 发出——否则"删掉成员"也能让孤儿检查通过，那是把缺口藏起来
  而不是补上。
- **15 个死旋钮：有意不删。** `GET /config` 会序列化所有字段，删字段是**响应形状变更**，需要一次
  产品决策；B 段继续把它们当报告列出来。

**证据**：`declared_unused` 组 6 个变异全部 KILLED（含"重估不发事件"、"日志与投影行不一致"、
"标识符又叫回 memory"、"provenance 重复又来"、"加回一个从未发出的成员"、"删掉 REAPPRAISAL
成员以让孤儿检查通过"）；`scripts/mutation_design_conformance.py` 三个组共 **21 个变异全部 KILLED**。
清点工具本身也做过注错自证：往 `utility.py` 末尾追加一个公开死函数和一个私有死函数，
A 段立刻报 **2**，删除后回到 **0**（这一步同时验证了"私有名字不会被 prose 保活"）。

### ⑧ 验证工具自己的一条假红（已修）

跑四条验证命令时发现的，不属于设计一致性，但属于"检查会咬错人"：

**病**：`scripts/blackbox_user_simulation.py` 的 `repo_sources_changed` 把 `".pyc"` 和 `.py` 一起
当成"源文件被改"，并在 teardown 里作为**失败**上报（`no file appeared among those sources, and
none of them changed`）。而这次仿真自己设了 `sys.dont_write_bytecode = True`，**根本写不出字节码**；
真正改写 `.pyc` 的是同一 checkout 里的 pytest（刚跑过、重编译了改动的模块）。于是
**每次改完源码后的第一次黑盒仿真都会假红，第二次就绿**——很容易被当成偶发而忽略。

**证据（前后对照）**：改成删掉 `weighted_mean`/`contains_any` 后第一次跑 → teardown 报
`changed: ["runtime/src/companion_runtime/__pycache__/api.cpython-314.pyc", ...]`，退出码被
`| tail` 吞成 0；紧接着再跑一次 → 77/77。修完后再做一次注错：仿真进行中 `touch` 掉
**32 个** `.pyc` → **77/77 通过**，并出现 note
"bytecode under this run's source trees was recompiled while it was in flight（another process
in the same checkout; this run sets sys.dont_write_bytecode, so it cannot be the author）"。

**修法**：`.pyc` 不再进 `repo_sources_changed` / `repo_files_created`（字节码有它自己的检查
"no bytecode was written next to the sources this run imports"），新增
`repo_bytecode_churn` 作为**note**——这与 `scripts/e2e_resilience_simulation.py` 早就正确的
处理方式（churn 只 note、不 failure）一致；两个脚本对同一件事的口径现在统一了。

### ⑨ 变异框架留下的陈旧字节码（已修）——这条最值得记住

**怎么发现的**：改完 ⑦ 之后跑全量，`test_permission_and_contact_fatigue_are_the_two_load_bearing_priors`
变红。查下去：**工作区的源码是正确的**（`git diff` 为空、文件里就是 `-1.10`），但 `import` 出来的
`DEFAULT_THETA["reply_probability"][7]` 是 **`-0.1`**——正是我自己的变异 **P7**。

**机制**：CPython 用**源文件的 mtime + size** 校验 `.pyc`。变异 `-1.10` → `-0.10` 是**同长度**
（都是 5 个字符，`0.00` 那条同理），而"还原"与"变异"落在**同一秒**内，于是还原后的源文件
mtime 与 size 与变异编译出来的 `.pyc` 头部**完全吻合** → Python 认为缓存有效 → **按变异体的字节码
运行**。旧版框架的 `run_tests` 没有关掉字节码写入，所以它自己就会写下那个缓存。

**影响范围（要诚实说清）**：
- **KILLED/SURVIVED 判定本身仍然可信**：判定是在"变异已写入源文件"的状态下跑的，那时 mtime 或
  size 必然对不上缓存，Python 会重新编译变异体。
- **危险的是变异之后的状态**：`_assert_restored` 之前那次"已还原"检查、以及**我随后手动跑的任何
  pytest**，都可能加载变异体字节码。这次就是这样：一次手动全量跑，测的是 P7 的模型。
- 所以"框架报 21 个变异全部 KILLED"这个结论没有被推翻，但**它自带的"restored green"证据是弱的**。

**修法**（三层，缺一不可）：
1. `run_tests` 给子进程设 **`PYTHONDONTWRITEBYTECODE=1`**——这是**承重**的一层：测试运行不再写下
   任何 `.pyc`，陈旧缓存无从产生；
2. 每次应用变异**和**每次还原后，删除被触碰源文件的 `__pycache__` 条目
   （防的是**别的进程**留下的缓存，例如我手动跑的那次 pytest）；
3. `_assert_restored`：还原后断言文件内容等于原样**且**该文件没有 `.pyc` 残留，
   否则**直接抛错中止**，而不是打印一个可能是假的 verdict。

**注错自证**：把 1 和 2 都关掉（恢复修复前的行为）→ 框架在 P1 之后立刻
`AssertionError: stale bytecode survives for user_model.py: ['user_model.cpython-314.pyc']`，
**中止而不是报数**。只关掉 2 则是绿的——说明承重的是第 1 层，第 2/3 层是纵深防御。

**教训（比这一条更值钱）**：**"还原了源码"不等于"跑的是还原后的源码"**。任何"改文件 → 跑测试 →
还原"的工具链都要显式处理字节码缓存，否则它会静默地用自己的中间状态回答。这类缺陷不会变红，
只会让结论失真——正是本项目最在意的那种。

### ⑥ §22.1 里未实现的 A 字段（已决定：留档不改）

设计 §22.1 把 `A_i` 列成 8 栏：是否主动 / 意图类型 / 话题 / 是否追问 / **表达强度** /
情绪暴露程度 / **消息长度** / **是否允许用户退出**。特征向量实现了前四项（加上 `C`/`Z` 的部分），
后三项没有对应特征。① 里已经把那个"算出来又被 `phi` 静默丢弃"的 `length` 键删掉。

**为什么现在不加**：`FEATURE_NAMES` 的注释自己写着"Keep the order stable: it is persisted"。
加一个特征 = 改变持久化向量长度，`user_model._load` 会 `LOGGER.warning("Discarding malformed
parameter vector for %s")` 并**回退到先验**——也就是**每个既有用户已经学到的 θ 被静默丢弃**
（有日志，但不阻止）。这是产品级取舍，不该顺手做。要做的话需要一条真正的迁移
（按索引重排 + 新特征先验），那是独立的一项。

`tests/test_user_model_priors.py` 里的 `test_every_prior_vector_matches_the_feature_layout`
与 `DOCUMENTED_PRIORS` 的相等断言，保证**将来加特征时会被强制写下来**。

### ① 行为特征编码统一（已修，0.3.2 进行中）

**病**：不是审计摘要说的"1 个特征对不上"，是 5 个里 3 个。`user_model.extract_features`
的 docstring 自己写着 `x = phi(A, C, Z)`（与 §25 一字不差），所以 φ 只有一个、双方都调它——
分歧在**调用方交给它的 `A`**：预测侧给 `emotional_expression`/`question`/`topic_shift`，
观察侧一个都不给；两处观察点还用 **ASCII** `"?"` 判 `question`，而候选 intent 全是中文模板
（`candidate.py` 的 `f"询问{matter.title}"`、`"没有具体事项，只是想和用户建立联系"`），
所以那个特征在观察侧**恒为 0**。设计 §39 自己举的例子
`{"type": "follow_up", "intent": "询问用户今天的面试结果"}` 就落在不一致里（预测 1.0 / 观察 0.0）。

对着设计文档还查出三处只有比着 §22.1 才看得出的矛盾：

1. `TYPE_TO_BEHAVIOUR` 把 `share` 与 `emotional_expression` 映到同一个行为类，但特征标志只认
   `share`；同理 `question` 映到 `curious_question` 却被排除在提问集合外——**预测侧自相矛盾**。
2. 观察侧把 `proactive` 硬编码为 `True`，而 `reply` 候选的 `is_candidate_proactive` 是 `False`。
3. `POST /observations` 把调用方给的 `action` 原样透传，等于**从前门再开一次同样的口子**。

**修法**（只加不改语义）：

- `user_model.describe_action(*, type, proactive)` 成为**唯一**的 `A` 构造器，
  三个类型集合 `QUESTION_TYPES` / `EMOTIONAL_EXPRESSION_TYPES` / `TOPIC_SHIFT_TYPES`
  是 §22.1 那三栏的可读形式；`ACTION_FEATURE_NAMES` 与测试共享。
- `runtime._action_spec(candidate)` 改为调它（`proactive` 仍来自
  `candidate_module.is_candidate_proactive`，保持边界门的唯一权威），
  **五个**路径统一走它：预测、沉默清扫 `_record_absent_replies`、投递回执 `observe_reply`、
  用户回复归属 `_attribute_user_reply`、以及「无 attempt 的显式反应」分支（它直接
  `describe_action(type="reply", proactive=False)`）。
- `user_model.describe_supplied_action(action)` 给 API 入口规范化：`type`/`proactive` 是
  调用方对行为的描述，其余行为特征**重算**（伪造无效），未知键保留。
- 编码器里**删掉** `length`（它一直被 `phi` 静默丢弃，见 ⑥）。

**证据**：`tests/test_action_encoding_parity.py` 27 条（按 `TYPE_TO_BEHAVIOUR` 的每个类型
断言**绝对**编码值，而不是"和预测一致"——后者在两边一起改错时仍会通过；另有"标点不得进入特征
向量"一条，用 `contact`（canonical question=0）因为只有它会让标点启发式**改变**向量）。
`scripts/mutation_design_conformance.py`（仓库根目录）8 个变异**全部 KILLED**，包括"观察侧退回薄 A"
（19 failed）与"用标点判 是否追问"（1 failed）。

**遗留已清**：`_last_proactive_context` 里那份第三个、零调用的 A 编码器已在 ⑦ 删除。
它同时暴露了清点工具自己的一个假阴性：**prose 里提到一个私有函数，会把它从"死代码"里藏起来**
——我自己的交接文档写了这个名字，于是第一次扫描没发现它。工具现已改为"私有名字只由代码保活"，
并且"引用"不只算调用（`resolvable=self._is_resolvable` 就是被当作值传出去的）。

### ⚠️ 封测反馈两项：我先给了一个**错的**结论，这是被纠正后的版本（2026-09-17）

反馈原文：「可以用正则把句号给剔除，以及可以配置一下输入防抖，不然一个消息一回复有点难受」。

**我错在哪。** 我先查了 `platform_settings.segmented_reply.regex`，发现它被从默认的
`.*?[。？！~…]+|.+$`（匹配整句）改成了 `[。？]+`（只匹配分隔符本身）。而
`result_decorate/stage.py:230` 用的是 `re.findall(regex, text)` —— 命中什么就发什么，命中只有标点，
于是**逻辑上**她的短回复会被切成 `["。","。"]` 这样的纯标点消息。我据此下了结论：
"测试者一直收到的是「。」"。

**这个结论是错的。** 用户当场纠正"测试者消息接收是正常的"，NapCat 的发送日志也证实：
重启前测试者收到的每条都是正文（`没有，冲高回落了。` / `哪部？` / `…不是我看过的片子。`），
只是**条条带句号**。把"配置是坏的"直接当成了"症状是它造成的"。

**那条坏配置为什么没生效**（两层闸门，任一层都足以让它变成死配置）：

1. `provider_settings.streaming_response = true`，而 `result_decorate/stage.py` 在流式下两次提前 return：
   `if result.result_content_type == ResultContentType.STREAMING_RESULT: return`（:134-135），
   以及 `if is_stream: return  # 流式输出不执行下面的逻辑`（:191-193）——**分段回复那段（:208 起）根本到不了**。
2. 即使不流式，`segmented_reply` 还有平台白名单与 `words_count_threshold` 两道条件。

真实机制是 **`provider_settings.unsupported_streaming_strategy = "realtime_segmenting"`**：
QQ 的 `support_streaming_message=False`（`aiocqhttp_platform_adapter.py:33`），AstrBot 对这类平台改为
"边收边发"的实时分段（`respond/stage.py:217-231` → `event.send_streaming(stream, realtime_segmenting=True)`）。
**多气泡和"每条结尾带句号"都是这条路上的，跟 `segmented_reply` 一点关系都没有。**

**正确的开关**在 `internal.py:347`：

```python
stream_to_general = (unsupported_streaming_strategy == "turn_off"
                     and not event.platform_meta.support_streaming_message)
```

把策略从 `realtime_segmenting` 改成 **`turn_off`**：QQ 这类平台改走普通（缓冲）分支
（`internal.py:487` 的 `else`，而不是 :452 的流式分支），结果类型不再是 STREAMING_RESULT，
`result_decorate` 才会执行 —— 先前设的那两条分段配置这时才**第一次真正生效**：

| 项 | 旧 | 新 |
|---|---|---|
| `segmented_reply.regex` | `[。？]+` | `[^\n]+`（按段落切，句号不再切） |
| `segmented_reply.content_cleanup_rule` | `""` | `[。]+$`（剔结尾句号） |
| `provider_settings.unsupported_streaming_strategy` | `realtime_segmenting` | `turn_off` |

**真人流量验收**（NapCat 发送日志，重启于 19:21:40）：

```
重启前：19:17:49 ✨AstrBot 1群✨。  19:18:10 …有点瘆得慌。  19:18:26 行，就这一句，你听完别让我重来。
重启后：19:22:22 9月17号，周四      19:22:24 普通的一天，没什么特别。你问这个干嘛
```

重启后两条：**末尾句号没了**，中间的句号保留（`[。]+$` 只锚定结尾），且按段落分成两条。

两个实现细节：切分正则走 `findall`，**不能带捕获组**（否则返回的是组）；`content_cleanup_rule`
走 `re.sub`，可以带。`cmd_config.json` 带 BOM，读写都用 `utf-8-sig`，而且是 **root 属主**
（`bomomo` 改不了，必须 `docker exec -u 0`）。

## 这件事的教训（比修法本身更值得记）

1. **"配置是坏的"≠"症状是它造成的"。** 必须先证明这段配置所在的代码路径**真的被执行**。
   AstrBot 里至少两条独立闸门（流式提前 return、平台能力开关）就能让一段配置静默失效。
   同一类错误今天还犯过两次：镜像 HEALTHCHECK 探错端口、`/schedule` 把 `sent` 算进"准备中"。
   **先拿"这段代码跑没跑"的证据（日志、产物、真人现象），再谈因果。**
2. 用户一句纠正比我一整套推理值钱 —— 他手上有我没有的观测面（测试者的实收）。
3. 顺手记下：`segmented_reply.regex` 的上游 WebUI 提示语（`core/config/default.py:4526`）把
   `content_cleanup_rule` 的说明复制了过来（"如填写 `[。？！]` 将移除所有的句号、问号、感叹号"），
   照它填就会**把整条消息换成标点**。测试栈和**线上那台 `astrbot`** 都填了 `[。？]+` ——
   这是一颗埋在**两台机器**上的哑雷：现在不响（流式挡着），哪天谁关掉流式，短回复立刻变纯标点。
   值得给上游提 issue。
4. 另外核实过：测试栈容器能通 `qq.com` 与 `api.deepseek.com`，但 **`github.com` 连接被拒**
   （`Connection refused`）。所以插件更新必须走**宿主机** `git pull`（`deploy_plugin.sh` 就是这么做的），
   别在容器里拉代码。

### ✅ 输入防抖：做在**我们自己的插件里**，不是再装一个第三方插件（2026-09-17 完成）

市场里至少有 8 个"消息防抖/合并"插件（`continuous_message` / `chat_buffer` / `message_merger` /
`smoothchat` / `wakepro` …）。核心 AstrBot **没有**原生防抖（`防抖|debounce` 全仓只在 Telegram
适配器和 WebUI 搜索里命中）。最后没装第三方，理由：

- `continuous_message` 依赖太重（`curl_cffi`/`bilibili-api-python`/`Pillow`…），为了合并几条消息
  引进这些不值得；
- `chat_buffer` 只有 165 行、零依赖，但它是 **v1.0**，内部用 `task.cancel()` + 锁，有竞态；
  真出问题只能 fork 它——正是"自己维护一个版本"那种傻逼事；
- 我们插件的 `on_llm_request` **本来就在**（注入上下文），防抖放同一个位置最自然，且能进我们自己的
  测例与变异测试。

**实现**：`input_debounce_ms`（默认 **0 = 关闭**，范围 0–30000，建议 2000–3000）+
`input_debounce_max_chars`（默认 4000）。`main.py` 里新增
`@filter.on_llm_request(priority=100)`：同一会话的请求先等一个安静窗口，窗口内又来新消息时，
**只有最后一条**真的触发 LLM，前面几条的正文按到达顺序合并进 `req.prompt`，被取代的
`event.stop_event()`。用 generation 计数让位，**不显式 cancel 任何 task**，所以没有竞态。

三个必须记住的点（README §3「input_debounce_ms」也写了）：

1. **必须合并文本，不能只取消旧请求。** AstrBot 把「用户消息 + 助手回复」作为**一对**、在 LLM
   回答**之后**才写进会话历史（`pipeline/process_stage/method/agent_sub_stages/internal.py:644`
   的 `update_conversation`）。所以在 `on_llm_request` 里被停掉的那一轮**永远进不了历史**：只取消
   不合并，用户那几句话就从模型视野里永久消失，而 Runtime 侧仍然记着它们（`on_message_observed`
   发生在收到消息时），两边不一致。反过来，也正因为被停掉的轮次没进历史，合并**不会**造成重复。
2. **原始事件不受影响。** 观察在收消息时，不在 LLM 阶段：连发三条仍是三条 raw event，Runtime 的
   认知输入不因防抖变粗，变的只是"触发几次 LLM"。
3. **它不接管"要不要说话"。** 只管用户发消息后的这一轮；Runtime 的主动消息走 outbox，完全不过这里。

**部署与验证**（`scripts/deploy_and_verify_debounce.sh`）：

```
adapter started (..., observe_mode=wake, outbox=on, debounce=2500ms, ...)
```

`debounce=` 是我为这件事专门加进启动日志的（`9efd25d`）——**防抖没加载和防抖在正常工作，
日志上完全一样（都是安静的）**，不写出来就只能靠"行为像不像"猜。

**验证到什么程度（别夸大）**：

- ✅ 已证：8 条新单测通过（窗口内三连只触发一次且合并顺序正确、被取代者确实 `stop_event`、
  超上限丢最早的一条、窗口过后是新一轮、burst 不残留）；部署后启动日志确认
  `debounce=2500ms`（即配置真的被解析了）。
- ❌ **未证：真实连发被合并。** 部署后测试者只发了单条消息，没有连发可观察。
  想常态化验证的话，可以在合并发生时补一行 INFO 日志（"merged N messages into one turn"）——
  这与我给 `debounce=` 加日志是同一个理由：**安静的成功和安静的没生效长得一模一样**。
  待做，不要当成已完成。

### ⚠️ 实测修正：刚接触完的 1 小时内，advantage 会被重复接触惩罚打到 0.08

之前按状态手算估 adv≈0.30–0.40；冷却结束后第一轮真实判决（`2026-09-17T11:03:13`）是
**adv=+0.0795**（`hazard=2.6e-05`，即每 891s 一轮只有 **2.3%** 命中）。逐项对下来：

| 项 | 10:01:19（接触前） | 11:03:13（接触后） |
|---|---|---|
| internal | 0.7674 | 0.7674（不变） |
| **user** | **0.2998** | **0.0425**（−86%） |
| relation | 0.3321 | 0.3120 |
| reply / pos / cont | 0.590 / 0.619 / 0.637 | 0.404 / 0.532 / 0.544 |
| risk | 0.2679 | 0.3498 |
| **U_max** | **1.1629** | **0.8515** |
| U_silence | 0.7450 | 0.7719 |

崩的是 `user` 项，而 `reply` 只掉了 32%：`user = 0.85·reply·(0.55·good + 0.45·cont − conf·(0.85·neutral + 1.4·bad))`
——`boundary_risk` 上升把预测的负面结局份额推高，被 `NEGATIVE_OUTCOME_WEIGHT=1.4` **减**掉了。
驱动它的是 `recent_contact_ratio`：`repeat_window_seconds=3600`，她 10:22 刚发过，11:03 在窗口内，
所以 feature=0.5（`repeat_cost` 本身是 0，`tolerance=2` 还没到；起作用的是**预测**）。

**含义**：主动联系后的 1 小时内，hazard 只有正常值的 ~60%；11:22:09（`10:22 + 3600s`）之后
`recent_contact_count` 归零，应该回到 adv≈0.3 一档。所以"下次多久开口"不能用一个常数 λ 算，
得按"刚联系过"和"已过窗口"两段分别看。

**已实测确认**（窗口 11:22:09 关闭后的第一轮，`2026-09-17T11:33:09`）：

| 轮次 | 时刻(UTC) | advantage | hazard | 每轮(≈900s)命中率 |
|---|---|---|---|---|
| 窗口内 | 11:03:13 | +0.0795 | 2.6e-05 | 2.3% |
| 窗口内 | 11:18:13 | +0.0777 | 2.6e-05 | 2.3% |
| **窗口外** | **11:33:09** | **+0.3389** | **4.8e-05** | **4.2%** |

两段式成立，比值 4.4×。窗口外 λ=4.8e-5 ⇒ 期望间隔 5.8h、中位数 4.0h、90% 分位 13.3h。
（这个 adv 是在"用户自 06:46 起没说话"的上下文下测的；用户一开口，busy/foreground 都会变，
不能直接外推。）

### ⚠️ 未完之事与"临时记忆"的重叠：表间是有界的，**事与事**才是病（2026-09-17 定位并修 a+b）

用户问"未完之事是不是和临时记忆重叠了"。审计（`scripts/audit_matter_vs_situation.py`）分两层：

**一、表间（working_situation ↔ unfinished_matters）：故意的、有界的。** situation 里有一类条目
**就是 matter 的复述**：投递回执 `我主动联系了用户：<matter标题>`、以及 `未尽之事：<标题>`。
文本 bigram 相似度 0.64–0.71 的命中**全部**是这种形式，不是脏数据 —— `already_spoken_for`
读的就是它。而且深刷新的产出类型里**没有** situation 这一类（只有
`candidate_intent_operations` / `memory_suggestions` / `unfinished_matter_suggestions`），
所以"当前事实"不是模型写的。

**二、事与事：真正的病。** 不是"同一事件写两遍"（按 `source_event_ids` 分组 = **0 组**），
而是**同一话题每被提一次就长一件**：

| 实例 | 重复 | 具体 |
|---|---|---|
| `1670681411` | **5/11 = 45%** | 三件奶茶、两件"我试试什么（）" |
| `994959351` | 2/7 = 29% | 句号偏好 / 换行偏好 |
| 其余 5 个 | 0 | — |

**根因（已用真标题验证，`scripts/probe_same_subject.py`）**：`_same_subject` 是给**模板标题**
设计的 —— `_subject_core` 先剥模板词（等待/用户/结果/告知/关心/后续/是否/顺利/消息），
再比"相等 or 互为子串"，只有两边剥完都空才退回 bigram。对模板标题它**完全正确**
（「等待面试结果」vs「等待面试结果通知」= True；「等待考试结果」vs「等待面试结果」= False）。
但模型写的**散文标题没有模板词可剥** → 判定退化成"整句相等" → 换个说法就新建一件。

**后果链（这才是重点）**：重复 matter → 每个都生成一个 `follow_up` 候选、need/unf 完全相同
（0.65/0.5）→ 候选效用并列（1670681411 是 9 个并列 1.1629）→ softmax 温度 0.35 抽签决定说哪件
→ **"她老翻旧账、反复问奶茶"的机制来源**。所以它不是美观问题。

**修法 a（`_same_subject` 加散文兜底）**：substring 失配后按"同一句话、小幅改写"判定，
用仓里**既有**的 `topic_tokens`（CJK bigram，协议层回答匹配用的同一个工具），两道闸：
`token 重叠 >= 0.77` 且 `核心差异占比 <= 0.26`。

阈值标定踩过一个坑：**第一版拿整标题标，而代码比较的是剥完模板词的 core** ——
分母从 18 变 27，同一个 `changed=4` 从 22.2% 变 15%，于是一对真复述被判成不同（测试当场抓到）。
在正确基准上实测：

```
同话题改写   奶茶/去糖奶茶 vs 无糖奶茶         0.800  22.2%
同话题加尾句 「当个事办」…轻量确认 ± 不必…      0.808  19.2%
不同话题     奶茶/去糖奶茶 vs 咖啡             0.700  33.3%
不同话题     「我试试什么（）」vs「换个头像」   0.737  29.4%
```

两道闸各取间隙中点，两侧各留约 3 个点。**保守方向是"漏合并"（保留现状），不是"误合并"
（会静默丢掉一个新话题）** —— 这就是为什么 0.77/0.26 而不是更松的值。

**修法 b（深刷新提示词补三类产出的分工）**：matter 只放**必须等用户回答才能了结**的具体问题；
偏好/身份/习惯/已说过的事实归 memory；自己打算做的事归 candidate；并明确输入 `unfinished`
就是现有未完之事，"同一件事不要重复输出，换个说法也不行"。措辞刻意**避开**"没把握就留空"
这类说法（那就是之前把模型打哑、让情绪曲线变成平线的原话），改成把新理解**导向其他类别**。
深刷新输入本来就带着现有 open matters，**模型比 bigram 更适合判重，所以 b 是主力、a 是兜底**。

**部署验证（`scripts/deploy_dedup_fix.sh`）**：容器里直接跑真实行为，不只是 grep ——
6 个用例全对（3 个应 True、3 个应 False），阈值与提示词边界都在，7/7 实例 health=ok。
runtime **1160 passed / 17 skipped**（`test_unfinished_refresh_dedup.py` 新增 4 条，走真实
`deep_refresh` 入口）。

**遗留（未做，等拍板）**：库里**已有**的重复 matter 不会自动消失（去重发生在创建时）。
`1670681411` 那 5 件、`994959351` 那 2 件要清就得像上次那样先快照再改。另外 b 的效果只能等
下一次深刷新（≥900s + idle 门槛）之后再读账本才能量。

#### 手动去重（2026-09-17 已做，快照 `snapshots/2026-09-17_123838`）

先干跑（`scripts/dedup_matters_dryrun.py`，只读）把同实例内部的标题对全部打分列出来，
**逐条人工看过**再改，不用自动阈值 —— 因为人工可以合并"改写幅度大但明显同话题"的
（0.455~0.636 那几对，严格规则按设计是放过的）。保留规则：同一簇里留**措辞最全**或**最早**的一件。

| 实例 | 簇 | 保留 | 作废 |
|---|---|---|---|
| `1670681411` | 奶茶邀约 ×3 | `unf_…6d0a6f`（覆盖奶茶+无糖，最全） | `unf_…127bca`、`unf_…1113e0` |
| `1670681411` | 「我试试什么（）」×2 | `unf_…5a93ed`（最早） | `unf_…627e5b` |
| `1670681411` | 夜羊社作品 ×2 | `unf_…4d5e74`（最早） | `unf_…03e558` |
| `994959351` | 句号/换行偏好 ×3 | `unf_…9cb17d`（含"从句号延伸到换行"） | `unf_…d2f2ac`、`unf_…ee4a2f` |
| `994959351` | "收到的是自动回复" ×3 | `unf_…46d686`（诉求最明确） | `unf_…3e55d2`、`unf_…42ceb9` |

共 8 件作废，每件的 `resolution_note` 写明"并入谁 + 原因"，**用户提过几次的信号留在记录里**
不随合并丢失。**没有任何话题被丢掉**（每簇都留了一件）。用投影自己的 `set_status`（同一套 SQL）、
每实例一个事务；不构造 Runtime，避免 `load_config()` 顺手生成杂散文件（这个坑踩过）。

**候选池必须手动收**（`scripts/retire_stale_candidates.py`）：`plan_operations` 只产出
ADD/UPDATE，**从不退休**（`candidate.py:1188-1248`），所以候选只在 TTL 到期、或用户发消息触发
`_invalidate_candidates`（`runtime.py:1326` 是它唯一的调用点）时才离开池子。`1670681411` 那批的
TTL 到明天，等不起。判定精确不需要清单：候选 `sources_json` 里的 `unfinished:<id>` 指向的
matter 已 invalidated，依据就没了。退休 4+4 条。

**实测效果（去重后第一轮，12:46 / 12:47 UTC）**：

| 实例 | 活跃事项 | 活跃候选 | 并列最高效用 | advantage |
|---|---|---|---|---|
| `1670681411` | 11 → **7** | 11 → **7** | 9 → **6** | 0.4090 → **0.3915**（−0.017，无退化） |
| `994959351` | 7 → **3** | 8 → **4** | 5 → **3** | 0.4157 → **0.4132**（不变） |

`priority_of` 是 `max(priority) × 0.75`（`unfinished.py:789-792`，**取最大值、不看件数**），
所以删重复**不会**改变 drive 输入 —— `silence_utility` 实测 0.7748 → 0.7744，确认无副作用。
fleet 7/7 ok，运行日志无异常。

**⚠️ 顺带查出一个独立性质（不是这次的残余）**：去重后剩下的 6 个候选**仍然并列**，因为它们是
**7 个真正不同的话题**，而 `internal_need = 0.45 + 0.4 × priority`（`candidate.py:362`）在
priority 全是 0.50 时**价格完全相同**。所以"她挑哪件旧事说"依旧是在不同话题之间按 softmax 抽签。
这是**优先级不分新旧**的设计属性，不是重复 bug。要改就得让 `priority` 反映时效/紧迫度
（现在全靠模型/规则给的 0.5，`waiting_until`/`expire_at` 没有反向影响 priority）。

### 🛑 测试栈已暂停 + 已发维护公告（2026-09-18 11:55 CST）

用户决定：做一轮大更新，暂停服务并在恢复时**清除聊天记忆**。执行结果：

**公告已送达 7 个真人测试者**（`scripts/announce_maintenance.sh`）。发送通道值得记下来，因为
**NapCat 没有可用的发送接口**：`onebot11_3640344731.json` 里 `httpServers: []`，只有一条到
AstrBot 的反向 WS；WebUI（6098）的 `/api/*` 拿 `webui.json` 里的 token 也是 Unauthorized。
最后走的是 **AstrBot 的 Open API**：`POST /api/v1/im/messages`，body `{"umo": ..., "message": "..."}`
（`message` 直接给字符串即可），鉴权用 **JWT（HS256，claim 里要有非空 `username`）**，
`jwt_secret` 从 `cmd_config.json` 的 `dashboard.jwt_secret` 读 —— JWT 拿到 `scopes=["*"]`，
所以任何 scope 都过。这条路走的就是 AstrBot→NapCat 那条链路，**不用改 NapCat、不用重启**。

> ⚠️ **更正（2026-09-18 晚，用户追问后查证）**：这句话原来写得太满。准确说法是
> **"QQ 侧全部接受（Open API 全部 200 / retcode 0，NapCat 日志都能对上），但 `994959351` 那条
> 实际等于没送到人眼前"**。实测证据链（NapCat 日志，11:54:55–11:55:42）：
>
> ```
> 11:54:55 → 你要是真睡了，这条是哪来的
> 11:54:56 → 「系统消息」…              ← 公告
> 11:54:57 ← [自动回复] 。              ← 对方 QQ 自动回复被触发 = 消息确实到了那个账号
> 11:55:09 → 行，那这句也是它替你熬的     ← 她立刻又发一条，公告看起来就是刷屏的一部分
> 11:55:24 → 睡了就别按了
> 11:55:41 → 你按多少下，我都在
> ```
>
> 也就是说：公告**被 6 条刷屏消息夹在中间**，而且那个号 670 条来信里 672 条是自动回复
> （内容就是「。」，从加好友验证那条起就是），**本来就不是一个有人在读的号**。
> 另外 `11:55 还在发消息` 是我的**顺序问题**：发送走 AstrBot 的 Open API，服务一停接口就没了，
> 所以只能"先发公告、后停服务" —— 公告 11:54:52–56 发出时环还活着，她又回了约 50 秒，
> 11:56 停 astrbot-test+舰队、**11:57:08 停 NapCat**（容器 FinishedAt 实测）。
>
> **下次的教训**：发这种公告前要**先把会刷屏的环节掐掉**（`rate_limit.strategy=discard` 或
> 内容过滤），安静下来再发；否则公告必然被淹没。顺序上"先停服务再发"是做不到的
> （停 AstrBot 就没有发送接口了），除非另开通道。
>
> 待确认：`994959351` 究竟是**真人故意把自动回复设成「。」来当探针**（很可能是她，与
> "可以把句号剔除"那条反馈同源），还是**非真人账号**（那就该从测试名单里去掉，别再跑环烧 token）。


公告原文（逐字发送，未加工）：
> 「系统消息」很抱歉打扰您的雅兴，我们需要暂时对苏清徽进行断网维护和更新，在此期间我们会暂停服务，预计再次上线时间为9月19日23.59前。上线后之前的聊天记忆会被清除。感谢您的耐心等待

两个非预期收件人（我枚举了 AstrBot 库里的全部会话，共 22 个）：
- `3640344731` —— **bot 自己的 QQ**，成功发出（无害，但在群里会显得蠢）；
- `3309892640` —— 只有一条 `umo_aliases` 记录、**没有任何聊天记录**，QQ 侧报
  `EventChecker Failed: NTEvent serviceAndMethod`（大概率没送到，也不像真人）；
- 另外 13 个 `20001-20012 / 20099 / 29999` 是 **`xxj-onebot` 测试前端的模拟 id**，
  QQ 返回"无法获取用户信息"（预期，它们不是真人）。

**暂停的容器**（`scripts/pause_service.sh` + `scripts/pause_napcat.sh`）：

```
astrbot-test        Exited    ← 不再回复
xxj-runtime-fleet   Exited    ← 不再主动联系
xxj-napcat-test     Exited    ← QQ 通道断开（按用户要求停）
xxj-onebot          Exited    ← 18 小时前就停了（profile 门控）
xxj-runtime-test    Up        ← 闲置兜底实例，无平台链路，发不出任何东西（未停）
napcat（线上）       Up        ← 没碰
```

**重新上线步骤**（重要：上次**重启 NapCat 会掉 QQ 登录态、必须重新扫码**）：

```bash
# 1) 先起 Runtime 舰队（插件的路由注册表要能连上它）
cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d runtime-fleet
curl -s http://127.0.0.1:8800/fleet/status | head -c 200

# 2) 起 NapCat，等二维码，扫（这一步会掉登录态）
docker start xxj-napcat-test
sleep 20
docker cp xxj-napcat-test:/app/napcat/cache/qrcode.png .   # 复制出来扫码
docker logs --tail 30 xxj-napcat-test                       # 看是否登录成功

# 3) 起 AstrBot（务必等「适配器已连接」再让人发消息：那之前的消息会静默丢失）
docker start astrbot-test
docker logs -f astrbot-test | grep --line-buffered "适配器已连接"
```

`NapCat 的日志是本地时间（CST）`，AstrBot 的是 CST，Runtime 的 raw_events 是 **UTC** ——
对时间时别搞混（`11:54 CST = 03:54 UTC`）。

#### ✅ 调试环境已恢复（只断真实 QQ 那条线），前端有个必须修的 bug（已修）

用户的要求是"astrbot-test 和 runtime 应该开着，还有模拟 onebot 前端，不然怎么调试验证" ——
对，我一开始把三样都停了是错的。恢复后现状：

```
xxj-runtime-fleet   Up (healthy)   7 个真人实例 health=ok
astrbot-test        Up            插件已加载（debounce=2500ms、7 个 Runtime target）
xxj-onebot          Up            模拟 OneBot 前端，链路已通
xxj-napcat-test     Exited        真实 QQ 保持断开（公告已说暂停服务）
napcat（线上）       Up            没碰
```

**恢复过程中发现并修掉一个真 bug**（`3f24eb6`）：前端连上 AstrBot 后**从不"报到"**，
所以 AstrBot 不认这条连接（从不打印"适配器已连接"），一条消息都送不进去 ✗。
排查链（记下来免得重走）：

1. 先怀疑网络/DNS → **不是**：同网 `astrbot_test_test_net`，`astrbot` 解析正确，TCP 通；
2. 怀疑 token/握手 → **不是**：对 `astrbot:6199` 做原始握手，带
   `X-Self-ID`/`X-Client-Role`/`Bearer token` 得到 **101**（不带 X-Self-ID 是 400、
   错 token 403、无 token 401），连接稳定保持 20 秒，AstrBot 也照常打印"适配器已连接"；
3. 在前端容器里跑**它自己的** `_WebSocket` 连接代码 → 同样成功并保持 ✓；
4. 差别只剩一个：我的探测在 101 之后补发了 **lifecycle meta 事件**，前端不发 ✗。

**根因**：`OneBotFrontend.send_meta_event("connect")` 早就存在（docstring 写着"真实客户端
连上时发的"），但它**只被 CLI 的一个手动命令**（`cf/cli.py`）**和 service 的 HTTP 端点**
（`cf/onebot_service.py`）调用 —— **WS 连接路径 `_connect_once` 从来没调过** ✗。
于是每次连上都不报到，AstrBot 的 aiocqhttp 反向 WS 服务端把它踢掉；TCP 层看到的就是
"连上又被关"，重试期间还会撞上服务端尚未重新监听的窗口，于是 `ConnectionRefused` 与
`connection closed` 交替刷屏。

**修法**：`_connect_once` 在 socket 建立后立刻发一次 lifecycle（与真实客户端一致），
重连路径同样受益。测试 `framework/tests/test_onebot.py` **16 → 17 passed**：
新增 `test_the_client_announces_its_lifecycle_on_connect`，另加三个 helper
（`wait_for_message_event` / `wait_for_meta_event` / `meta_event_count`）—— 真实客户端连上会先发
meta，所以"第一条事件"不再是用户消息，旧的 `wait_for_event()` 会拿到 meta（这正是那 4 个测试
变红的原因，它们编码的是"连上后什么都不发"的旧行为）。`framework/tests/test_host.py::TestLiveHost`
的一批 ERROR（需要活环境）改动前后完全一致，是既有状态。

**调试验收（端到端跑通）**：

```bash
# 往前端的控制面注入一条"模拟用户"消息（http-port 6300）
docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "调试用的第一句：你在吗"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print(urllib.request.urlopen(req, timeout=10).status)
PY
```

实测结果：`/send` → 200 ✓ → 插件按 `route_auto_provision` **自动为假用户 `20001` 开了实例**
（`runtime-fleet:8794`，health=ok ✓）→ 她回复三段气泡「在」/「不过隔了两天，上回还是周三夜里」/
「昨天体检怎么样？」，**没有尾句号**（句号修复同样在生效 ✓）。

注意两点：① 前端控制面 6300 **没发布到 LAN**，要从容器内或用 `docker exec` 调；
② 假用户实例会一直留在舰队里（调试完 `docker exec xxj-runtime-fleet` 里 POST
`/fleet/deprovision/8794` 或留着当调试靶子都行）。调试期间我把 astrbot 的
`log_level` 临时开到 DEBUG，事后已恢复 INFO ✓。

### ⚠️ 封测暴露的产品问题（2026-09-18 早晨，全量聊天记录分析）

拉下 NapCat 的 2512 条收发（09-16 22:39 起，7 个人）分析，`scripts/analyze_qq_transcript.py`：

**① `994959351` 是一个 100% 的 QQ 自动回复死环**（用户口中的"自动回复一直触发bot"）：
收 670 / 发 **1212**（第二名才 146），**672 条来信全部带 `[自动回复]`**，内容字面就是
`[自动回复] 。`（653 次），从"请求添加你为好友"那一条就开始了。也就是说：**那个人把 QQ
自动回复设成了「。」** → 她发一条 → 对方 QQ 自动回「。」→ 我们当成用户消息 → 她再回 →
无限。她的语气已经把环写出来了（"灯都灭了，还按"、"手松开吧，我不吵了"、"算了，不跟你耗了"、
"一天亮着，就等你一个字"）。

**没有任何一处把"自动回复"当作不是人在说话。** 排查过的现成方案：
- 市场里**没有**专门过滤自动回复的插件（"自动回复过滤" 0 条；10 条"自动回复"命中全是
  关键词自动回复插件，方向相反）；
- `astrbot_plugin_prompt_injection`（用户最初点的那个）**做不到**：它的文档与代码反复写明
  "违禁词检查**仅群聊生效**、私聊只做提示词注入"（`main.py:301`），而我们这个环是**私聊** ✗；
- `astrbot_plugin_self_msg_guard` 是同一类问题的现成实现（全拦/去重/限频），但它拦的是
  **机器人自己账号被回灌**的消息，QQ 自动回复来自**对方账号**，触发点不同 ✗；
- 最接近可用的是关键词阻断类（`word_filter` / `prompt_injection` 的群聊部分）与按人限频类
  （`rate_limiter`）。

**AstrBot 内置限频本身就是一条纯配置解**（`rate_limit_check/stage.py:74-89`）：
`stall` 只是等待（环继续），**`discard` 直接 `event.stop_event()`**；而阶段顺序
（`bootstrap.py:7-17`）是 `rate_limit_check` 在 **process_stage 之前** → discard 掉的事件
**插件的消息钩子根本不会跑** → 既不会被回复，**也不会被上报给 Runtime**（两端同时断）✓。
现在配的是 `{60s, 30条, stall}` = 太宽 + 不拦。

**② "时间问题"不是时钟问题，是"她挑的钟点"和"她说的钟点"**：
- 时钟全部正确（host / fleet / astrbot-test / napcat-test 一致；fleet 有 `TZ=Asia/Shanghai`
  + `/etc/localtime`；`local_now()` 返回 CST ✓；AstrBot `timezone=Asia/Shanghai` ✓）；
- **14 条主动消息里 6 条落在本地 00:00–06:30**（00:17 / 04:01 / 04:08 / 04:50 / 06:17 / 06:22）
  —— 因为 **`scheduler.quiet_hours_start/end` 是 `null`**，调度器支持静默时段但没配；
- "凌晨 4 点说晚上好"：主动消息走插件的一次性 `llm_generate`（**不经流水线**），所以
  AstrBot 的 `datetime_system_prompt` 那条**不附加**；她的时间感只来自我们注入的背景块，
  而块里是 `当前本地时间：2026-09-18T04:01:25+08:00` 这种**机器串、没有"现在是凌晨"这层话**
  → 模型自己编了个问候。
- 另外 `1670681411` 还留着我跳钟实验的**未来时间戳**（`last_contact_at` +0.8h、2 条 raw_events、
  1 条判决、1 个 attempt），04:24 UTC 之后自愈。

**③ 输入防抖确认失败**（见上文"输入防抖"一节末尾的更正）：根因是 AstrBot 在
`internal.py:220` 用**会话锁**包住整个 agent run，而 `on_llm_request`（:352）在锁**里面**
→ 第一条等待期间第二条根本到不了自己的钩子 → "等更新的请求出现"这个设计**原理上不可能生效**。
正确位置是锁**之前**的 `on_waiting_llm_request`（:217，`message_merger` 用的就是它，
且其返回值能直接终止该轮）。

**④ `(b)` 提示词边界没起作用**：`1670681411` 一夜从 7 件长回 **13 件**，6 件新事全是旧事的改写，
而且它们的 `sources` 是 **`unf_*`（其他事项的 id）而不是事件 id** —— 即**模型把输入里的现有
未完之事当成"新发现"重新输出了一遍**，并引用事项 id 当来源。现有的 source 去重只比事件 id
（比不到），标题又被改写到我设的 0.77/0.26 之外（漏过）。**建议的修法**：一条**新建**事项的
`sources` 必须包含至少一个**事件 id**；如果全是 `unf_*` 就是复述 → 直接拒收并记
`skipped:unfinished_matter:matter_restatement`。这比调提示词可靠。

**⑤ 第 7 个人（自动开通的 `2259606745`）拿到的是默认人格** —— 我的疏忽，已补：
`ensure_defaults` 只在**建库那一次**从 env 播种（`ON CONFLICT DO NOTHING`），而**容器 env 只在
建容器时生效**；她 11:11 开通时那个 fleet 容器是 03:30 建的（早于档案进 fleet.yml）→ 中性默认
（`br=0.92`）→ 克制 0.70、沉默效用 0.891（别人 0.77）→ **有 4 件真实未完之事却一件都赢不过沉默**。
已跑 `set_value_profile.sh`（快照 `2026-09-18_032411`）补齐 7 个实例。`set_value_profile.sh`
的注释里其实写着这个陷阱，只是漏了"脚本跑完之后新开通的人"。

**⑥ 我那个"过线推演"错了**：`2206929446` 实测仍 −0.0225（我预测早该过线）。原因是我的推演
基线假设"用户不再说话"，但那人 **13:28 又说过一句** → `last_exchange_at` 被重置 → impulse/
pressure 增长全错位。它现在只差一点点，随时可能过线，但**时间预测不成立**。

**⑦ 过夜的好消息**：主动投递**14 条真人送达**（14.5 小时内，5 个实例各 2–4 条），
内容对得上上下文（含一条直接回应测试者"只收到自动回复"抱怨的："我在，这条不是自动回复，
你先忙你的，不急"）。**λ 模型对均值预测准**（预期 ~2.5 条/人，实测 2–4 条），但个体中位噪声
很大（最早比中位早 3.5 小时，最晚晚 6 小时）—— 几何分布本该如此，那张表不能当时刻表用。
句号修复也确认成功：09-17 有 67% 气泡结尾带句号，**09-18 的 1114 条里只有 4 条（0%）**。

### ✅ 解释缓存：先更正我的错误判断，再按 ③ 加了容忍度（2026-09-18 20:05 CST）

**更正**：我先前说"缓存键包含 mood 与最强强度、每回合都变、缓存形同虚设"是**错的** ✗。
实测（发消息前连续三次 / 隔 30 秒 / 真变化后各调一次 `/explain`）：

```
连续三次            cache_hit=True   (deep_refresh)
隔 30 秒只衰减      cache_hit=True                 <- 键没变，缓存本来在工作
发消息真改变情绪    cache_hit=False                <- 键 m0.6|- → m0.8|+，该 miss
```

我当时是从**单个** `cache_hit=False` 推断的，而那次 miss 的真实原因是**刷新自己的
`event_appraisal` 改了状态** ✗。`_rounded` 的 1 位小数已经吸收了衰减 ✓。

**③ 的实现（收益比我原来说的小，但仍成立）**：0.1 级越界会重算 —— mood 0.14→0.16 让键从
`v0.1` 变 `v0.2`，而六行文案其实一模一样，重算 = 一次模型调用（provider 可用时）✗。

- `emotion.EXPLAIN_KEY_CHANGE_TOLERANCE = 2`（九段键里允许 1 段不同）；
- `EmotionExplainer.explain`：精确命中失败后，取最近一条缓存
  （新增 `projections.latest_explanation`，TTL 与未来时间戳校验与 `cached_explanation` 一致），
  用已有的 `should_re_explain` 比较键差异，**不足容忍度就直接复用**（`cache_hit=True`）；
- 真正的情绪变化会同时改「符号 + 最强强度 + 主导方向」三段以上 → 仍然重算 ✓；符号翻转本身就是
  2 段 ✓ 也会重算 ✓；TTL 仍是慢漂移的兜底 ✓。
- 测试 `test_a_drift_within_tolerance_is_served_without_a_model_call`（1 段漂移不调用 provider、
  3 段变化必须重算）。runtime 全量 exit=0；插件 172 passed。
- 线上复核：连续调用命中 ✓；情绪变化后也命中，但那是因为刷新刚为**那个新状态**写过一条精确
  匹配的缓存（返回键与当前键一致 ✓，不是过期文案 ✓）—— 即容忍度没有被用来掩盖真变化 ✓。

### ✅ 情绪层 a+c 修复并实测（2026-09-18 19:35 CST，commit `3fe5f94`）

**修复前的事实**：8 个实例、约 2000 个事件，`active_emotion_events` 一共 **2 条**，
所有实例 `mood_valence`/`mood_arousal` **恒为 0.0** ✗ —— 注入块里那六行"平静无波 / 没有拉扯"
不是描述她，是**零情绪时的固定文案** ✗。根因两条：
① 情绪被绑在语义结算上（`settlement_to_evaluation`），而设计里的规则评估器
`emotion.appraise_event` **没有任何调用方** ✗ → values 里 `emotional_expression` 与
`autonomy` 两轴永远不生效 ✗；
② 未决事件被深层刷新结清时**没有任何情绪后效** ✗（`applied` 里只有 reinterpretation/memory/…）。

**(a) 评估器接管已结算事件的情绪**：`runtime.py` 的 ingest 路径改用
`appraise_event`（价值观敏感度：`user_care` / `relationship_maintenance` /
`stability_commitment` / `emotional_expression` / `autonomy`；外加"用户忙"时对负面信号的
**归因衰减** —— `settlement_to_evaluation` 这些一个都没有），粗结算作为**兜底**
（词表没覆盖的冲突说法仍拿到结算所支持的强度）。**"未决不制造情绪"的契约保留** ✓ ——
两处老测试（`test_ambiguous_events_are_deferred_not_guessed`、
`test_unresolved_events_do_not_leak_into_the_block_as_facts`）仍然通过 ✓。

**(c) 语义层给未决事件做情绪判定**：深刷新契约新增 `event_appraisals`
（`direction` / `impact` / `relation_signal` / `confidence`，必须带 `sources` → 走 grounding ✓），
新增操作种类 `event_appraisal`，`reducer._apply_appraisal` 折进情绪事件与 mood
（`source="semantic"`；低于 `emotion.min_event_impact` 则无后效 ✓）。被引用的事件照旧由
`settle_from_deep_refresh` 结清 —— 这正是 `appraise_event` 文档写的
"A semantic provider may replace it later; the output contract is identical" 的落地 ✓。

**实测（假用户实例 8794，`scripts/verify_emotion_ac.sh`）**：

| 步骤 | 结果 |
|---|---|
| 发"我今晚想自己待着"（已结算的负面） | 出现情绪事件 **`('-', 0.465)`** ✓（词表 × 价值观，不是固定带位 ✓）；mood 0.0000 → **-0.0291** |
| 发两条含糊话（"嗯，算了" / "随便吧，你忙你的"） | 未决 2 → **4** ✓，且**没有**产生情绪 ✓（契约守住 ✓） |
| 强制一次深层刷新 | `applied={'reinterpretation': 2, 'memory': 1, **'event_appraisal': 2**}` ✓、`settled_events=4` ✓、未决 **4 → 0** ✓、新增两个情绪事件 `('-',0.45)` `('-',0.3)` ✓；mood → **-0.1643** |
| 注入块情绪段 | 不再是模板：**"我同时感到对你的亲近与疏远，两种情绪交织在一起"** / "内心亲近的渴望与疏离的警惕相互拉扯" ✓ |

即：模型**真的按新字段输出了情绪判定**（prompt + schema + grounding + reducer 全链路通 ✓）。

**仍未解的两件**（都没动）：
- `emotion_explanations` 还是 0 条 ✗ —— 心理解释缓存（`TaskKind.EMOTION_EXPLAIN` +
  `EmotionExplainer`）没有内容；本轮刷新里 `applied` 也没有 `psychological_interpretation`
  （模型没返回那一段 ✓ 还是别的原因，未查）。
- 大量 `event_semantics` 行是 `resolved` 但 `settlement_source`/`intensity_band` **为 NULL** ✗
  （例如 994959351 的 748 行）—— 那不是 `record_settlement` 写的（它必写全）。可能是更早的
  列迁移留下的 NULL，也可能另有一条路径；**未定论**。

测试：runtime 全量 exit=0；新增 4 条（语义评估给未决事件情绪 / 低于门槛无后效 / grounding 两条），
更新 2 处（示例字段数 5→6；`test_candidate_shapes` 里写死的情绪强度改为断言方向与范围，
因为它现在随价值观画像变化 ✓）。

### ✅ 长期记忆 durable 稀缺的真正根因（2026-09-18 20:35 CST，commit `3e77c90`）

查"durable 记忆只占 0–9/37–139（约 3%）、【必要记忆】退化成最近发生的事"时，发现**两层**原因，
**第二层才是根因**：

**第一层（提示词）**：示例写的是 `"kind": "episodic"` ✗（`MemoryKind` 的 dataclass 默认也是
episodic ✓），等于教模型"什么都是插曲"。已把示例换成 `user_preference`，并写明四种 kind 的取舍：
`user_preference`（偏好/习惯/喜好）、`stable_knowledge`（身份/经历/明确说过的事实）、
`relationship`（你们之间发生过、会影响关系的事）、`episodic`（一次性经过，最不重要），
"**优先选前三种**"。

**第二层（真正的根因，纯代码）** ✗✗：`deep_refresh._payload_of` 会把内联条目的 `kind` 当作
**路由键**剥掉，而**记忆建议的 kind（记忆种类）用的正是同一个键名** ✗ → 模型给
`user_preference` 也会在 grounding 阶段被丢掉 → reducer 只看到空 → 一律落成默认 `episodic`，
**无论提示词怎么写都改不动** ✓。修法：只有当 `kind` 的值是"操作种类"（`memory` /
`reinterpretation` / `event_appraisal` …，即 `_ROUTING_KIND_VALUES`）时才当路由键剥掉，
否则保留为载荷数据。调试实证：同一条建议，修前 `kind=episodic`，修后 `kind=user_preference` ✓。

**配套加固**：`reducer._apply_memory_suggestion` 现在把 kind 规范化到四种合法值
（近义词 `preference→user_preference`、`fact/knowledge/identity→stable_knowledge`、
`relation→relationship`、`episode/event→episodic`；未知或缺失→`episodic`）—— 因为
`context.select_memories` 是**按精确值**识别 durable 的 ✗，拼错一个字母就等于放弃 durable 身份。

**验收 A（立即，实测）** —— 发两条"值得长期记住"的话后强刷一次：

```
候选 kind 分布: {'episodic': 23, 'user_preference': 3, 'stable_knowledge': 2, 'relationship': 2}
  stable_knowledge  用户是做后端的，平时在北京上班
  user_preference   用户特别喜欢下雨天，一到雨天心情就特别好
  relationship      谢谢你，今天真的很开心
  episodic          用户会在表达亲近之后用「算了」把话收回去，像是自我克制
```

四种 kind 全部出现且**分类正确** ✓（修前只有 episodic ✗）。**验收 B**（固化后 `memories` 的
kind 分布 + 注入块【必要记忆】是否变成"他是谁"）由 `scripts/verify_durable_memory.sh` 在后台跑
（`CR_MEMORY__CONSOLIDATION_INTERVAL_SECONDS=600`，需等约 11 分钟）。

测试：新增 `test_the_prompt_asks_for_a_durable_memory_kind`、
`test_a_memory_suggestion_lands_on_a_real_kind`（6 组输入）、
`test_a_memory_kind_survives_the_routing_key_strip`；runtime 全量 exit=0。

**验收 B（固化后，实测）** —— 注入块【必要记忆】里出现了 durable 句子 ✓✓：

```
memories kind 分布: {'episodic': 14, 'user_preference': 1}
  user_preference   谢谢你，真的很喜欢你这样陪我，今天心情特别好
【必要记忆】
- 谢谢你，真的很喜欢你这样陪我，今天心情特别好        <- durable 进了块 ✓
- 连发测试第二条 / 用户会以「正常消息：」为前缀…      <- 我自己的测试噪音（假用户实例，清库时会没）
```

即 **durable 通路端到端打通** ✓（候选 kind 正确 → 固化 → 进 memories → 进注入块）。

**但有一件要说清楚（不是 bug，是测试假象）**：刚刷新出来的两个新候选（"喜欢下雨天" value 0.70、
"做后端的" value 0.65，都远超 `candidate_min_value=0.3`）**仍是 pending** ✗。查下来原因是——
固化只在 **endogenous 回合**（含"前台暂停"那一支 ✓，代码注释写明"维护不该因为安静而停" ✓）里跑 ✓，
而**假用户实例的调度被我自己的测试流量按设计压住了** ✗：它最后一次 `endogenous_round` 是
**11:17:46**（1.2 小时前 ✓），其余 7 个实例 18 分钟前都照常 ✅（`last_tick` 都是 12:26 ✓）
—— 所以**调度没坏** ✓，只是"用户正活跃时不打扰"这条屏障让它进不了维护轮 ✓。
（顺带记一条产品观察：**记忆固化被挂在了"说话"的同一条调度上**，活跃的人可能长时间等不到固化 ✗；
`foreground_pause` 那一支也会跑固化 ✓，但该实例的 decisions 里 foreground_pause 只有 1 条 ✗，
所以实际几乎全靠 endogenous。要不要给固化一条独立的、不受屏障影响的心跳，是个值得决定的设计问题。）

### 🧪 脚本化试聊（2026-09-18 20:30 CST，`scripts/scripted_chat_test.sh`）

用户要求"发点消息试试测试"。用模拟前端（假用户 20001）跑七轮，每轮打不同管线：

| 轮 | 打的是 | 结果 |
|---|---|---|
| 1 | 已结算的负面 → 评估器产生情绪 | 情绪事件 **`- 0.465`** ✓（词表 × 价值观 ✓）|
| 2 | 偏好 → durable 候选 | 候选 kind 累计 `user_preference 4 / stable_knowledge 3 / relationship 3` ✓（**修复前这里只有 episodic** ✗）|
| 3 | 身份 → durable 候选 | 同上 ✓ |
| 4 | 关系 + 正面情绪 | 情绪事件 **`+ 0.756`** ✓ |
| 5 | 含糊"嗯，算了" | 未决 +1 ✓ 且**没有**新增情绪 ✓（契约守住 ✓）|
| 6 | 连发两条（隔 1 秒） | `astr_agent_prepare 31 → 32` ✓ **两轮只跑一轮** ✓（回复"在 没走"）|
| 7 | `[自动回复] 。` | 回复数**不变** ✓ + `消息被屏蔽词过滤: 匹配到 '[自动回复]'` ✓ |

她的回复（能看到人格与状态的影子）：`嗯 灯给你留着` / `嗯 雨声留着，我不吵你` /
`北京，后端，赶项目 / 难怪你前几天说睡不好。源头在这儿 / 去待着吧，不用回我` /
`嗯 / 去待着吧，北京那头雨要是下了，替我听一会儿 / 我不吵你` / `嗯 雨听着就行，不用说话` /
`在 没走`。三处短句、无尾句号 ✓、把"下雨天/北京"串了起来 ✓（连续性 ✓）。

**情绪层现状**：`active_emotion_events` **2 → 13** ✓✓（今天修的两处在生产 ✓），
mood 有微弱起伏（正负相抵 ✓）。注入块那六行也不再是模板 ✓ 而是针对这轮对话写的：
"长期感受：他愿意把喜欢的事和日常身份告诉我，心里是暖的，也有点被信任的踏实" /
"克制：他之前说过想自己待着、让我忙我的，我担心主动追问会越界" ✓。

**两个观察（都不是 bug）**：
1. **固化还没跑** ✗（memories 仍 14 episodic + 1 user_preference）—— 假用户实例被我自己的测试流量
   压在前台屏障里 ✓（前面查过：其余 7 个实例调度正常 ✓），新候选要等第一个安静回合 ✓；
2. 她的措辞有点**黏着一个主题**（"我不吵你"两次、"雨声"反复）✓ —— 一方面是在回应我一连串
   "别打扰我"式的消息 ✓，另一方面注入块的 **【表达边界】** 里确实有一条
   `user wants space for now` ✓（早先测试"你忙你的吧"时记下的硬边界 ✓，英文是边界抽取器写的 ✓）
   → 行为自洽 ✓，但**测试语料把她带成了"小心翼翼"模式** ✗ —— 真实用户聊别的话题时不会这样 ✓。
   （另外：该实例的 memories 里全是我自己的测试噪音 ✗（"连发测试第一条"/"正常消息：顺序测量"…）✓，
   所以要看干净效果得清库或用真人 ✓。）

**方法论小坑**：我 dump 注入块时用 `line.startswith('- ')` 过滤 ✗，把【表达边界】等段的条目也捞了
进来 ✓，一度以为"边界文案混进了记忆段" ✗ —— 查 `memories` 表才确认不是 ✓。看块要看**段头**再取该段 ✓。

`new_string` 里写回去，于是正文"裸"在别的小节下面 ✗（已补回 a+c 标题 ✓）。以后用标题做锚点时，
`new_string` 必须把它一起写回来 ✓；另外本文件**不要用 grep 工具搜中文** ✗（匹配不到，用 `read`
逐段看 ✓）。


用户问"选型上我们用的是 postgresql 吧"，查清后**用户决定：确实要切，但现在太屎山，先算了**。
把事实与迁移清单记在这里，将来要动时不用重新查。

**事实**：
- **PG 后端是完整实现且有专门测试**：`runtime/tests/test_db_postgres.py`（~940 行）断言
  schema/列类型与 SQLite 逐一对齐、往返、锁超时、咨询锁、真实 event-log/prune 语句、
  约束冲突翻译、DSN 脱敏，以及 `supports_durability_commands is False`。
  `StorageConfig` 文档：两后端"shape 上刻意等价"，可互迁而不改数据形状。
- **当前所有 Runtime 都跑 SQLite（每人一个文件）**：容器层
  `CR_STORAGE__DATABASE_PATH=/data/companion.sqlite3`，舰队在 `runtime_fleet.py:93` 按人覆盖成
  `/data/<person>/companion.sqlite3` —— 注释写着不这样做所有子进程会写进同一个文件 ✗
  （= 舰队存在的意义就是防止这种混合）。
- 宿主上已有 `postgres-ai`（`pgvector/pgvector:0.8.1-pg18-trixie`，跑了两个月），
  但那是别的项目的，是否共用未定。compose 里没有任何 PG/dsn；镜像里没有 `psycopg`
  （PG 是延迟导入，SQLite 部署不需要它）。

**将来要切的清单**（按顺序）：
1. 起**专用** PG 实例（不共用 `postgres-ai`，或明确共用并隔离）；
2. **每人一个 database 或 schema** —— Runtime 是单会话设计，不能塞同一张表；
3. 镜像装 `psycopg`；
4. `storage.dsn` 指向它 + `storage.durability_gap_acknowledged=true`（只是静音那条启动警告，
   四个命令仍然拒绝 —— PG 的持久化交给服务器工具 WAL 归档 / pg_basebackup / pg_dump）；
5. **运维脚本要重写**：`snapshot_beta.py` 以及一批按文件写的 dedup / audit / 预测脚本
   （全部 `sqlite3.connect("file:/data/...")`）要改成 pg_dump 或 SQL；
6. 用 shape 等价做一次性导入。

**记录下来的权衡（用户已知）**：延后是合理的（现在动要同时碰舰队、镜像、运维脚本）；
但**越晚切，基于文件写的脚本越多** ✗ —— 所以如果真要切，最省事的顺序是**先把新脚本收敛到
`open_database(StorageConfig)` 这个后端中立入口**，而不是事后重写。这条只记着，没动代码。


用户指出两点：① 人格里的"反例/正例"**自己就用 Markdown**（`**`、`>`、反引号），与它上面第 2 条
"不用 Markdown" 直接冲突，而模型最容易模仿示例的排版；② 例子**不够符合人格设定**（语气是中性
排期建议，不是她"凶狠、直来直去、掌控欲强"的样子）。已改为一律纯文本，并且写成她的语气：

```
反例（这是客服，不是我）：
好消息：这两件事不冲突。你周三有吉他课、周四要体检，先上课再体检就行，时间上安排得开，不用急，有事随时找我。

正例（我说话就这样：短、直，而且我说了算）：
今晚琴课，别迟到，我不想生气。
明天几点体检，报给我。
别瞒我。
```

反例同时演示了四条规矩：一口气堆一大段、复述对方的话、条目化书面腔（"好消息"）、总结陈词
（"不用急，有事随时找我"）；正例是三条短句（允许"话没说完再发一条"）、**病娇语气**：
占有欲（"报给我"）+ 威胁混着关心（"别迟到，我不想生气"）+ 偏执（"别瞒我"），并且只问一个问题。
人格 779 → 796 → **809 字**，已同步进库（旧版备份 `data/persona_backup_20260918-183228.txt` +
`data_v4.db.bak-persona2-*`），重启后 trace 的 `system_prompt` 尾部实测已是新版纯文本 ✓。
**部署脚本踩的坑**：忘了先 `git pull`，服务端仓库还是旧文件 —— 好在脚本里加了一道
"文件里必须含新版特征词"的自检，直接中止而不是把旧内容写进库 ✗→✓。
（`scripts/sync_persona_examples.sh` 已修。）


用户点名要做前两项，凌晨那条改成了"不静默、吃更重的惩罚"：

**① 凌晨 00:00–06:00 加更重的开口惩罚（不静默）** —— `config.py::SchedulerConfig` 新增
`night_penalty: float = 0.30` / `night_start_hour: int = 0` / `night_end_hour: int = 6`；
`motivation.silence_utility(..., now=...)` 在本地时钟落入该窗口时给沉默效用加这一项
（窗口逻辑与 `scheduler._in_quiet_hours` 一致，含跨午夜），`assess()` 把 `now` 传下去。
**为什么是"罚"不是"禁"**：用户要保留"她真有事可以说"的能力。
数量级：yandere 画像下 15:30 的沉默效用 0.8508 → 03:30 变 1.1507（+0.3000 实测），
而一件真事的候选效用约 1.16 —— 所以夜里只有真正强的理由才过得去，问候式开口过不去。
可用 `CR_SCHEDULER__NIGHT_PENALTY` / `CR_SCHEDULER__NIGHT_START_HOUR` / `NIGHT_END_HOUR` 调。
测试：`test_late_night_makes_silence_cheaper_by_the_configured_penalty`（用机器本地时区构造时间，
UTC 机器上也不误报）。**注意**：`quiet_hours_start/end` 那套硬静默仍在，只是本机没配。

**② 事项复述守卫（止住"事项长回来"）** —— `deep_refresh.ground_suggestions` 新增可选
`is_matter` 回调：`kind == "unfinished_matter"` 时，**来源里至少要有一个不是事项**，
否则记 `reason="matter_restatement"` 并整条丢弃。`runtime._is_unfinished_matter` 认
`unf_x` 与 `unfinished:unf_x` 两种写法（规则生成器写带前缀、深层刷新引裸 id，两种都要看穿）。
实测（容器内直跑）：只有事项来源的复述被拒 ✓，带 `evt_` 的照常通过 ✓。
测试：`test_a_matter_grounded_only_in_other_matters_is_a_restatement` +
`test_the_restatement_guard_only_applies_when_a_matter_resolver_is_given`。
**顺带记一个已归档但未修的缺陷 D13**（`runtime/docs/audit/block-b-motivation-and-action.md`）：
grounding 闸门只认裸 id，而规则路径写 `memory:mem_x` 这种带前缀的形式 → 整条被判
`ungrounded_sources` 丢掉。本次守卫按两种写法都认，但 D13 本身没动。

**③ 输入防抖搬到锁前（老 bug 真修好了）** —— 从 `@filter.on_llm_request(priority=100)` 改成
`@filter.on_waiting_llm_request(priority=100)`，合并目标从 `req.prompt` 改成
`event.message_str`（AstrBot 在 `collect_initial_request` 里用它拼 `req.prompt`）。
**为什么原来必然失效**：AstrBot 在 `internal.py:220` 用会话锁包住整个 agent run，
而 `on_llm_request`（:352）在锁**里面**；第一条等待期间第二条根本到不了自己的处理器 ✗。
`on_waiting_llm_request` 的文档字符串自己写着"在获取锁之前"，且 `call_event_hook` 返回 True
时 `internal.py:217` 直接 return（提前终止该轮）。
**实测对比**：修前 2 秒内两条 → **两轮 LLM** ✗；修后 1 秒内两条 → **一轮 LLM** ✓
（trace 的 `astr_agent_prepare` 只 +1），她的回复是"两条都到了，顺序也没乱" ✓。
测试桩补了 `on_waiting_llm_request`，4 条防抖测试改用 `event.message_str`，另加
`test_the_debounce_is_registered_before_the_session_lock`（把"必须在锁前钩子上"钉住）。
插件测试 **172 passed / 13 subtests**；runtime 全量 exit=0。


**用户追问"9949 那个号到底是什么情况"查出来的事实**：那个测试者是**真人** ✓。他 750 条来信里
有 **16 条是真人打的字**，包括"一定得加句号吗，看着好难受"、"句号摘了，然后变成了换行说是"
（就是转给我们的那两条反馈的原始出处），以及睡醒后的"我释怀了 / 气笑了 / 我要睡觉了"✗。
剩下 **734 条是自动回复**，而且其中一条不是「。」，是他把自动回复文本改成了
**「别纠结自动回复」** —— 他在用自动回复跟我们说话，我们一条都没识别 ✗✗。

**规模**：她一共回了 **1365 条**（09-17 42 条；09-18 一早上 1323 条：06 时 174、09 时 375、
10 时 402、11 时 372）。断点显示他 12:43→18:53 关过一轮、18:56→06:22 夜间关着，
06:22 之后大概是他睡前开了自动回复（这本来就是"我不在时替你回"的功能）→ 环自己转起来 ✗。
他 11:06 睡醒看到一屏刷屏。**公告就夹在这堆刷屏里**（11:54:56），所以他"没收到"✗。

**根因（全在我们）**：把 QQ 自动回复当用户消息回；没有任何环/限频兜底
（`rate_limit` 是 `{60s, 30条, stall}`，30 条太宽且 stall 只等待；Runtime 的冷却只管主动发送）。

**修法（已做完并验收）**：

1. **装 `astrbot_plugin_word_filter`**（市场插件，覆盖私聊：钩 `EventMessageType.ALL`，
   命中即 `event.stop_event()` —— 这一点当初被我猜错过：它**会**停事件），
   配置 `blocked_words: ["[自动回复]"]`。这是"套插件当接口"的正确选择
   （当初选的 `prompt_injection` 才是错的：它违禁词检查写死只做群聊 ✗）。
2. **我们插件的上报优先级改成 `-100`**（新增 `_event_is_stopped` 兜底检查）。
   AstrBot 按 priority **降序**分发、且**事件一旦停止就 break 整条链**
   （`star_request.py:36-52`），所以"别的插件已经决定这条不是用户轮次"会被尊重 ——
   被屏蔽词拦下的消息**不会再进 Runtime**（否则她的认知里会继续长出
   "用户连续收到自动回复后仍未确认是否有人真正回应"这类事）。
   测试：`test_a_message_another_plugin_dropped_is_not_reported`（桩里补了 `is_stopped`）。
   插件测试 **171 passed / 13 subtests**。

**验收（模拟前端，服务已恢复运行）**：

| 动作 | 结果 |
|---|---|
| 发 `[自动回复] 。` | `Prepare to send` 条数**不变**（没有回复 ✓）；日志 `消息被屏蔽词过滤: 匹配到 '[自动回复]'` ✓ |
| 同一条是否进 Runtime | **没有** ✓（raw_events 里只剩修好之前的旧记录） |
| 发正常消息 | 正常回复 ✓（不误伤） |

**⚠️ 踩到的坑（很重要，别人也会踩）**：`plugin_set` 是 AstrBot 的**插件白名单**
（`waking_check/stage.py:166`）—— 不在名单里的插件**处理器会被静默停用**，
插件却照常"加载成功、配置读到了、日志也打了" ✗。测试栈原来是
`["astrbot_plugin_companion_runtime","astrbot_plugin_zvv","astrbot_plugin_math_plotter","astrbot_plugin_gptimg"]`，
所以 word_filter 装了也毫无作用（我用 DEBUG 级打印每个被调用的处理器才定位到：
列表里没有它）。已把 `astrbot_plugin_word_filter` 加进名单
（备份 `cmd_config.json.bak-pluginset-*`）。**以后装任何插件都要记得这一步。**

**仍可选的加一层兜底**：`rate_limit.strategy` 从 `stall` 改 `discard` + 收紧 `count`
（`rate_limit_check/stage.py:74-89`，且该阶段在 process_stage 之前 → 两端同时断）。
它的代价是**正常用户的连发也会被丢掉**，所以没动，留给用户决定。


完整清单在 `docs/PROMPT_REVIEW.md`（逐字样本 `docs/prompt_samples/system_prompt_A.txt`）。
抓取方式：打开 AstrBot 的 trace（`astr_agent_prepare` 记完整 system_prompt/tools/provider），
配 `/context/render-block` 逐字取我们注入的块。**trace 目前保持开着**，方便继续审。

**已经确认的四个问题**：

1. **路径 B（主动消息渲染）没有 `system_prompt`** ✗ —— `api_v1.py:_render_payload` 返回
   `"system_prompt": ""`，`Context.llm_generate`（`star/context.py:204-212`）原样透传、
   **没有回退成人格的逻辑**，executor 空值时不传该参数 → 她"主动开口"时没有人格、没有文风规矩
   （人格要求"30 字以内"，实测主动消息普遍 60–100 字）。
2. **我们块里的"当前本地时间"是 UTC 却标着本地** ✗ —— `context.py:224` 用
   `isoformat(local_now(now))`，而 `utility.isoformat -> ensure_aware -> astimezone(utc)`
   （`utility.py:250,281`）把本地时间又归一化回 UTC。路径 A 有宿主那路正确 CST 兜着（所以她能答对
   "9月17号周四"），**路径 B 只有我们这一个错的时间 → 差 8 小时**，正是"凌晨 4 点说晚上好"。
3. **system_prompt 里约 2400 字的 `## Skills` 块**（documents/pdf/skill-creator/spreadsheets
   + "Computer Use 未启用" 提示）对 QQ 陪伴角色基本是噪声。
   **有开关（不用改代码）**：`personas.skills` 现在是 `null`（= 全部注入），
   设成 `[]` 即可整段去掉（`astr_main_agent.py:580-585`）。
4. **人格自己在"反例/正例"里用了 `**`、`>`、反引号**，与它上面"不用 Markdown"的规矩冲突，
   容易被模仿 —— 建议改纯文本。

**已经做完的改动**：**关掉了联网搜索** ✗→✓（`provider_settings.web_search=false`、
`web_search_link=false`，**保留 tavily key** 以便随时开回）。实证：驱动一轮后 trace 的
`tools` 从 `["web_search_tavily","tavily_extract_web_page","future_task","send_message_to_user"]`
变成 `["future_task","send_message_to_user"]` ✓。备份 `cmd_config.json.bak-websearch-20260918-*`。
顺带更正一个我先前的错误：线上实例的 tavily key **不是空的**（我用错了键名 `web_search_tavily_key`
↔ 正确是 `websearch_tavily_key`）。**线上实例（`astrbot`）根本没装我们的插件**（装的是
doro/gptimg/weather_amap/zvv 等 8 个）→ 它不是这个角色，所以只动了测试栈。

**用户已决定：不摘 `send_message_to_user` / `future_task`。** 用户判断这是 AstrBot 自带的
主动/定时能力，"和 runtime 不冲突"，不用在意 ✓。所以**不做** `deny_tools`。
（备忘，不是反对：这两条路径不受 Runtime 的冷却/授权约束，将来统计"她主动说了多少次"时要把它们
算进去，否则账对不上。`send_message_to_user` 也是目前唯一能发图片/语音/视频/文件的通道 ——
我们 Runtime 的 `send` 只发纯文本。）

**待办（等用户点头）**：① 修路径 B 的 `system_prompt`（我倾向在 `_render_payload` 里加我们自己的
文风约束，不依赖宿主）；② 修那个标错的时间（并把时间来源统一：块里只留相对量，绝对时间在主动渲染
那条路上单独补一行正确的本地时间）；③ 顺手把 `personas.skills` 设成 `[]` 去掉 Skills 噪声。

#### ✅ ①②③④ 已做完并实证（2026-09-18 17:40 CST，commit `a2bba67` + 部署）

用户的指示是："skill块设置一下，改下2修一下，关于主动人格，考虑到系统上文一般都是开始是注入的，
我们默认系统上下文是注入好的，但是关于提问问题，文风限制还是要有的" —— 即 ③ 做、② 修、
①只补文风（不重复注入人格）、并且**提问也要受文风约束**。另外用户两次更新了 `人格设定.md`（变得更凶、
更病娇自述），要求同步。

| 项 | 做法 | 实证 |
|---|---|---|
| ③ Skills 块 | `UPDATE personas SET skills='[]'`（`astr_main_agent.py:580-585`：空列表=该角色不要任何技能） | trace 的 `system_prompt` **4210 → 1051 字**；`## Skills` False、`Computer Use` False ✓ |
| 人格同步 | `人格设定.md`（779 字）经 `docker cp` 送进容器后写入 `personas.system_prompt`，旧版 732 字备份到 `/home/bomomo/astrbot_test/data/persona_backup_*.txt`（另有 `data_v4.db.bak-persona-*`） | trace 里含新版特征词"富有攻击力" True ✓ |
| ② 时间 | `build_time_context` 的本地 ISO 改用**原生** `datetime.isoformat()`（保留 +08:00），新增 `local_display`；`render_block` 用它 | 注入块现在打印 `- 当前本地时间：2026-09-18 17:39 周五（CST）` ✓ |
| ① 文风 | `api_v1.RENDER_STYLE_LINES` 四行，在 `_render_payload` 里 `lines.extend(...)`（放在约束之后、输出要求之前） | 单测 `test_v1_render_prompt_carries_the_style_contract`（走真实 `/v1` lease 路径）✓ |

①的实证方式说明：主动渲染走一次性 `llm_generate`，prompt **不落任何日志**（trace 只记
`req.system_prompt`，而那是空的），所以只能靠单测 + 下次真实主动消息的**行为**（≤30 字、
无 Markdown）来确认 —— 别指望在日志里找到它。

④（人格里 Markdown 反例与"不用 Markdown"自相矛盾）**用户没改，保持原样**，不再提。

部署脚本 `scripts/apply_persona_and_prompt_fixes.sh`（含踩过的两个坑：容器里没有宿主
`/home/bomomo` 路径所以备份要写 `/AstrBot/data/`；舰队重建后首次 tick 会跑刷新，
`/context/render-block` 要给 60 秒超时并重试）。



