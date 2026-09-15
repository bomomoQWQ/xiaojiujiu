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

需要一个真的 AstrBot 来跑集成时，另外克隆上游到 `AstrBot/`（该目录同样被忽略）：

```bash
git clone https://github.com/AstrBotDevs/AstrBot.git AstrBot
```

---

## 2. 环境准备

```bash
# Runtime（独立环境，不要污染 AstrBot 的环境）
cd runtime
python -m venv .venv            # 需要 Python 3.11 或 3.12
.venv/bin/pip install -e ".[test]"        # Windows: .venv\Scripts\pip.exe
```

插件测试只需要 AstrBot 桩（仓库自带 `tests/stubs/`），不装 AstrBot 也能跑。

**不需要任何模型、任何密钥、任何外网。** 默认 `SemanticProvider` 是 `disabled`，
Runtime 完全靠确定性代码工作；可选的远端语义 provider 的 key 只从环境变量
`CR_SEMANTIC_API_KEY` 读取，**永远不要写进任何文件**。

---

## 3. 四条验证命令（改完代码先跑这四条）

```bash
# 1. Runtime 离线测试
cd runtime && python -m pytest -q                    # 期望 824 passed（见文末"当前状态"）

# 2. 插件离线测试（在插件仓库里）
cd ../astrbot_plugin_companion_runtime
PYTHONPATH=tests/stubs:. python -m pytest tests -q   # 期望 143 passed + 13 subtests

# 3. 用户黑盒仿真：真实 uvicorn + 文件 SQLite(WAL) + 真插件钩子，只断言用户可见事实
cd ..
python scripts/blackbox_user_simulation.py --base-dir ./bb-run

# 4. 高仿真故障恢复：并发上报、租约过期、断网恢复、重启续跑、队列 >150 行
python scripts/e2e_resilience_simulation.py --base-dir ./res-run
```

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
| Runtime 离线测试 | 824 passed |
| 插件离线测试 | 143 passed + 13 subtests |
| 高仿真故障恢复 | 335/335 |
| 用户黑盒仿真 | **68 / 69**（1 项未过，见下） |
| 版本 | Runtime 0.2.0；插件 0.1.0 |
| 许可证 | GPL-3.0-or-later |

**接手的第一个任务（已知未完成项）**：黑盒仿真第 11 阶段有一条失败——
"每条主动消息都落在剧本里本该发生的窗口内"。现象是：私聊里说定的考试
（未尽之事 `unf_aa476b5651bd`，其来源事件确实属于 `webchat:FriendMessage:10001`）
仍有 4 条 send 行被投递到 `webchat:GroupMessage:20002`。

```bash
python scripts/blackbox_user_simulation.py --base-dir ./bb        # 复现，退出码 1
# 产物里 <base-dir>/scenario/runtime.sqlite3 可以直接查：
#   SELECT title,status,source_event_ids FROM unfinished_matters;
#   SELECT kind,status,conversation_id FROM outbox WHERE payload_json LIKE '%考试%';
# 已知：候选的 sources_json 入库/出库是完整的（projections._to_candidate 映射正确），
#       所以问题在"这些 commit 为什么让 Runtime._event_ids_behind() 返回空"，
#       从而退化成"谁最后说话就发给谁"的兜底。切入点：runtime.py 的 _commit_attempt /
#       _conversation_for，以及候选是从数据库重新载入还是当场生成的。

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
4. **凭据纪律**：API key、GitHub 令牌只走环境变量；不要 `git remote set-url https://<token>@...`
   （会把令牌写进 `.git/config`，随后被打进每一次备份 bundle）。
5. **`runtime/tests` 与插件测试是两套**：Runtime 的 pyproject 里配了 `pythonpath = ["src"]`，
   插件那套靠 `PYTHONPATH=tests/stubs:.`。在错误目录下跑 `pytest` 会去收集上游 AstrBot 的测试
   （表现为上百个 collection error），那不是你的改动坏了。
6. **别在 `runtime/` 之外的目录跑 `python -m pytest`**：同样会收集到 `AstrBot/` 的测试套件。

---

## 6. 改代码的推荐节奏

1. 先在 `runtime/tests/` 写一条**会失败的**测试（描述不变量的那句话），再改实现；
2. 跑 `python -m pytest -q`；
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
| `scripts/` | 验证与运维脚本（两个仿真、基准、备份） |
| `archive/` | 已放弃的本地模型路线（留档，不参与构建，包名是历史遗留） |
| `RECOVERY.md` | 备份 / 恢复 / 权重位置 |

文档里的 `F:\理解痞老板\...`、`E:\companion_runtime_backup\...` 是**作者本机的路径**，
不是代码依赖；换机器时所有脚本都用 `--base-dir` 指定输出位置即可。
