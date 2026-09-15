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
| Runtime 离线测试 | 828 passed |
| 插件离线测试 | 143 passed + 13 subtests |
| 高仿真故障恢复 | 335/335 |
| 用户黑盒仿真 | **70 / 70**（退出码 0，连跑两次一致） |
| 版本 | Runtime 0.2.0；插件 0.1.0 |
| 许可证 | GPL-3.0-or-later |

> **换机器复现记录**（Linux / Python 3.14.7 / 全新 venv，2026-09-15）：上表四行**逐条复现**——
> `828 passed`、`143 passed, 13 subtests passed`、`335/335`、`70/70`，四条退出码全 0。
> 另跑一次注错确认断言会咬人：`--fault leak` → `checks failed: 3`、退出码 1，与下表 `leak` 行一致。
> 复现过程中发现的两个环境缺口（三个额外依赖、目录布局）已补进第 1、2 节。

**已验证的"检查真的会咬人"**（`--fault` 注错，每条都让对应断言失败）：

| 注错 | 黑盒失败的检查数 |
|---|---|
| `leak`（把内部标记/密钥塞进用户可见文本） | 3 |
| `duplicate`（每条主动消息发两次） | 5 |
| `topic`（主动消息只聊被禁话题） | 9 |
| `guilt`（加追责话术） | 8 |
| `cross_session`（把私聊内容发到群聊） | 4 |
| `default_session`（全部发到默认会话） | 5 |

**接下来值得做的（按我的排序）**：

1. 插件市场发布（`metadata.yaml` 已按规范校准；发布入口 <https://cloud.astrbot.app/>，
   需要 AstrBot Cloud 账号，我这边没有）。
2. 加 CI（需要给令牌 `workflow` 权限，或在网页上直接建 `.github/workflows/`）。
3. **渲染 prompt 里曾有两行同名指令**（已修复；这类形状值得记住）：背景块自己也有
   `- 想做的事：…`，内容是 Runtime 当前持有的意图——对一条主动消息来说往往是**上一次**
   想说的那件事——而指令区又有一行同名的。任何读者取第一行就会照旧的写，
   "群聊里的体检提醒被写成考试"就是这么来的。现在背景块那行改标为
   `- 之前想做的事（背景，不是现在的任务）`，并且在渲染主动消息时整行剔除
   （`api_v1._drop_intent_lines`）；黑盒仿真有一条正向断言守着这个性质。
4. `committed != sent` 与"平台已发出 / 结果已上报"之间的崩溃窗口（需要平台回执或宿主持久化幂等日志）。

```bash
python scripts/blackbox_user_simulation.py --base-dir ./bb        # 70/70，退出码 0
python scripts/blackbox_user_simulation.py --base-dir ./bb --fault leak   # 注错：证明检查会咬人
```

发布状态：

- AstrBot **插件市场尚未提交**（两个 README 的安装说明都是"克隆"，市场里搜不到）；
  要发布就到 <https://cloud.astrbot.app/> 提交插件仓库地址，`metadata.yaml` 已按
  [市场 JSON 规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html) 校准。
- **没有 CI**：GitHub 拒绝推送 `.github/workflows/` 下的文件，除非令牌带 `workflow` 权限。
  想加 CI 就给令牌加权限，或直接在网页上新建该文件。

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
3. 改动涉及跨时间行为（调度、未结事项、回复闭环、投递）时，**必须**再跑一次黑盒仿真——
   这次的 0.2.0 就是靠它抓到两个单测完全没覆盖的缺陷（已了结的义务被重新打开、跨会话投递）；
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
| `scripts/` | 验证与运维脚本（两个仿真、基准、备份） |
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
