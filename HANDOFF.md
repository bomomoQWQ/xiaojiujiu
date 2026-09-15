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
cd runtime && .venv/bin/python -m pytest              # 期望 828 passed

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
| Runtime 离线测试 | 914 passed / 14 skipped（接 `CR_TEST_PG_DSN` 时 PG 专项不再跳过） |
| 插件离线测试 | 143 passed + 13 subtests |
| 高仿真故障恢复 | 335/335 |
| 用户黑盒仿真 | **77 / 77**（退出码 0，连跑多次一致） |
| 记忆质量仿真 | **25 / 25**（`scripts/e2e_memory_simulation.py`，见第 6 节） |
| 版本 | Runtime 0.3.1；插件 0.1.0 |
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

### 接下来值得做的

1. `committed != sent` 与"平台已发出 / 结果已上报"之间的崩溃窗口
   （需要平台回执或宿主持久化幂等日志）。
2. framework/ 与 scripts/ 两个仿真脚本的整合（见 §7.1 的分工说明；
   目前两者互不依赖，这是有意的，整合前先想清楚要合并什么）。
3. 聊天窗口还没做的部分：多行输入、跨会话历史、全屏 curses 版本、
   回复的 Markdown 着色（清单见 `framework/README.md` §8）。

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
| `runtime/README.md` | 运维手册：配置项、API、降级、蓝屏恢复 |
| `framework/` | **外接测试框架**：可控虚拟时钟 + OpenAI 兼容 mock 端点 + 变量日志 + `cf` 命令行。不改原程序，见 `framework/README.md` |
| `scripts/` | 验证与运维脚本（三个仿真、基准、备份） |
| `archive/` | 已放弃的本地模型路线（留档，不参与构建，包名是历史遗留） |
| `RECOVERY.md` | 备份 / 恢复 / 权重位置 |

### 7.1 framework/ 与 scripts/ 的区别

两者都在测 Runtime，但定位不同，别搞混：

* `scripts/` 下那两个仿真脚本是**为固定剧本写死的验收**（黑盒 12 阶段 70 项、韧性 335 项检查），
  跑一次给一个是/否，改断言前先跑 `--fault` 注错确认它还会咬人。
* `framework/` 是**可交互的实验台**：起一个 harness，然后用命令行在运行中拨时间、灌输入、看变量、
  给假端点注错。适合"我想知道改成这样会发生什么"，而不是"发布前必须全绿"。

框架自带 102 个测试，其中包含用子进程跑 `cf run` 再拿客户端命令驱动它的验收测试：

```bash
cd framework
PY="$(cd ../runtime && pwd)/.venv/bin/python"   # 绝对路径，避免 sys.prefix 噪音警告
"$PY" -m pytest tests                            # 102 passed
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

**#3 + #5 用户提问进长期记忆 / 提问式记忆挤占 4 个名额**（用户已定口径，**决策已进代码库，实现未做**）
- **决策现在有可执行的验收标准**：`runtime/tests/test_question_memories.py`（3 条
  `xfail(strict=True)`：①提问存成命题、框架从 summary 里消失；②框架**移到记录里**（从 summary 消失
  但可从 `memory.structured` 取回——两半一起断言，因为单看"记录里有"对现状已经成立、是空验收）；
  ③提问式记忆不得占用那 4 个名额）。三条今天都不成立，一旦实现会 XPASS 并报错逼人摘标记。
- **为什么还没实现（不是懒，是范围）**：`MemoryCandidate` **没有 `structured` 字段**，而候选是**要落库**的
  （`MemoryProjection.upsert_candidate`）。所以"summary 存命题 + 原句留档"必须把**一个字段穿透四层**：
  候选类型（`typing.py`）→ 候选持久化（`projections.py`）→ 巩固时候选转记忆（`memory.consolidate`）
  → 提示词选择（`context.select_memories`/`build`）。半做会让原句彻底丢掉，而这正是你明确不要的
  （"框架要留"）。我选择留下验收测试而不是留下半截实现。
- 实现要点见下（原样保留）：
- 现象：8 条记忆的 summary 就是提问句（如"我喜欢你这件事情，你还记得我说过吗？"）；10 次探针里
  **4 次**的记忆区被提问式记忆占满，而被问起的那条披露**召回引擎确实返回了**
  （`recalled=true`、排名 4/10）**却输给预算**，只进 9/10 次。
- **用户口径（已确认，别再问）**：**命题进记忆**（剥掉疑问框架），**疑问框架本身作为独立的
  "关系证据"保留**（用户在检查你是否记得，这本身是关系信号），**且不得占用那 4 个名额**。
- 实现要点：`memory.propose_from_event` 的 `summary=summarize_text(text)` 目前存原句；
  需要剥离 `你还记得…吗？` / `…你还记得我说过吗？` / `你还记得我跟你讲过它吗？` 这类框架并
  产出通顺命题（剥不干净就**原样保留**，绝不产出半句话）；证据存成什么（新 `MemoryKind`、
  交互观察、或 `structured` 字段）需你定夺，若加新 kind，记得 `typing.py` 里
  `kind_importance`、durable 段成员判定、检索与 API 面都要处理；名额排除要落到
  `context.select_memories` / `context.build`（`durable` 来源）。

### 本轮的方法论教训（重要）

- 我派了三个子代理做 #1/#2+#3/#4，**它们在 8 小时内没有产出任何文件改动**（工作树始终干净），
  最后我收回工单自己改。**异地继续时不要重复这个做法**：这四条里 #2/#4 是"改一处 + 加测试"的
  小活，直接做更快。
- 另：提交前对**全仓**扫一次 `git grep -n "MUTATION\|PROBE"`——我在 `6f2b146` 里误带了子代理
  正在树里的临时变异（`GET /schedule` 又被加回推进时钟的 tick），已用 `79e66ee` 更正。

