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
#                                                      # 期望 143 passed, 13 subtests passed

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
   所以既有数据库判定不变。验收 `tests/test_reply_length_baseline.py` 12 条 + 8 个变异。
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
剩下的一条（报告 §5 的第 4 项）是让"意图类型"这一维度本身有单一的权威语义，
下次加 type 时才不会再犯——那需要一次设计决定，**尚未做**。
**这三条现有测试一条都查不出来**（当时的 1096 + 四套仿真全绿），因为没有任何检查要求
"同义词必须等价"或"跨用户可比"。修复的顺序就是先补这类等价性检查、再动实现——
两个已修项各自的第一条测试都是等价性断言，而不是把某个数字钉死。

### 接下来值得做的

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
| `scripts/` | 验证与运维脚本（**四个**仿真：黑盒 / 韧性 / 记忆质量 / 关系递进，外加 `runtime_bench.py`、`backup.ps1`、`dead_code_inventory.py`、`mutation_design_conformance.py`） |
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

