# scripts

这个目录里有两类东西，**用途完全不同**，混在一起看会浪费很多时间。

## 当前工具（部署与日常运维就用这些）

| 脚本 | 用途 |
|---|---|
| `fleet_admin.py` | **运维主入口**。`list` 谁在服务/能不能发；`add <session>` 开通一个人并验证（健康 + schema 版本）；`check [scope]` 逐条说明"她为什么不主动"；`remove <slug>` 下线（默认不删数据，`--purge-schema` / `--purge-data` 才删） |
| `dispatch_authority.py` | 主动外发的总开关：`show` / `freeze` / `shadow` / `restore`。权威是数据库里的 CAS 指针，改配置不生效 |
| `rolling_restart.py` | 逐 scope 滚动更新：冻结→等排空→重启→健康→schema→恢复。不带 `--confirm` 只干跑 |
| `runtime_fleet.py` | 监督进程：一台容器里每人一个 Runtime，控制面 `:8800`（`/fleet/status`、`/fleet/routes`、`/fleet/dashboard`、`/fleet/provision`、`/fleet/restart/<slug>`、`/fleet/deprovision/<slug>`） |
| `fleet_naming.py` | 会话 → slug → schema 的**唯一**定义。监督进程与运维工具都从这里取，避免两处规则漂移 |
| `fleet_roster.py` | "这次操作哪些 scope"的来源解析（`--scopes` → 环境变量 → 人员名单文件 → 控制面），拿不到就明确拒绝 |
| `build_runtime_fleet.py` | 生成 fleet 的 compose（人员、端口、服务定义） |

怎么跑：它们在 fleet 容器里执行（控制面与凭据都在容器内）。

```bash
docker exec -i xxj-runtime-fleet python /fleet/fleet_admin.py list
```

### 发版前的一致性检查

`check_versions.py` 不属于运维工具、不进容器，在仓库根目录跑即可。它检查版本号在**四处**
是否一致（`CHANGELOG.md` 顶部、`runtime/pyproject.toml`、
`runtime/src/companion_runtime/__init__.py`、插件仓库 `metadata.yaml`），不一致就退出码 1：

```bash
python3 scripts/check_versions.py
```

插件仓库不在本机时它会说明跳过了哪一项；而显式给的 `--plugin-repo` 写错了会**直接拒绝**，
不会悄悄去查另一个仓库（那会对着没人问过的东西报 "ok"）。

### 另一条门禁：当前工具不许写死作者的本机路径

`runtime/tests/test_ops_scripts_are_portable.py` 逐行扫描**上面那张表里的每个工具**，
出现 `/srv/companion`、`F:\…`、`E:\…` 这类路径就失败，并指出文件与行号。机器相关的值
要么做成参数，要么走环境变量，要么从脚本自己的位置推导（`backup.ps1` 现在就是这么做的）。

```bash
python -m pytest runtime/tests/test_ops_scripts_are_portable.py -q
```

`scripts/` 里那些**历史 `.sh`** 不在此列——它们是某次事故在某台机器上的记录，按上面的
约定原样保留。

> 给 `.ps1` 写注释时留个心眼：无 BOM 的 UTF-8 文件里，如果一行以全角标点（`，`、`。`）结尾，
> 在按 GBK 读文件的 PowerShell 上那个换行会被当成双字节字符的一部分吃掉，**下一行会并进注释**，
> 参数声明会凭空消失。`backup.ps1` 的注释因此写成纯 ASCII。

完整部署流程见 [`docs/部署与运维.md`](../docs/部署与运维.md)。

## 历史一次性脚本（保留作当时的证据，不要照着跑）

其余两百多个 `deploy_*.sh` / `probe_*.sh` / `diagnose_*.sh` / `check_*.sh` / `verify_*.sh`
等都是**针对作者那台机器、那次事故**写的一次性脚本：

- 里面写死了 `/srv/companion/astrbot_test/...` 这类路径和当时的 QQ 号；
- 它们记录的是"当时怎么定位问题的"，不是"现在该怎么部署"；
- 仓库自己的约定是这类脚本不该进仓库（见 `CHANGELOG` 里那条 `chore: 一次性补丁脚本不该进仓库`），
  但历史已经进来了，没有再回改。

要判断一个脚本属于哪类：**看它有没有写死路径或具体 QQ 号**，以及有没有出现在上面那张表里。
