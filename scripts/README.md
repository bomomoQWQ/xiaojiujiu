# scripts

公开目录只保留两类内容：**当前部署/运维工具**与**不含真人数据的纯仿真/回归工具**。
封测期间针对具体用户、具体路径、具体私网地址写的一次性脚本已经退出公开树；它们不是产品接口，
也不应靠换几个账号数字来假装完成脱敏。

## 当前部署与运维工具

| 脚本 | 用途 |
|---|---|
| `fleet_admin.py` | `list` / `routes` / `add` / `check` / `remove`；开通、体检、下线的主入口 |
| `fleet_roster.py` | scopes 来源解析：命令行 → 环境变量 → 人员名单 → 控制面 |
| `fleet_naming.py` | 会话 → slug → schema 的唯一规则 |
| `dispatch_authority.py` | 持久化外发权威：`show` / `freeze` / `shadow` / `restore` |
| `rolling_restart.py` | 逐 scope 冻结、排空、更新、健康检查、恢复；不带 `--confirm` 只干跑 |
| `runtime_fleet.py` | 每人一个 Runtime 子进程的监督器与控制面 |
| `build_runtime_fleet.py` | 从基础服务定义生成人员/端口/Runtime fleet compose |
| `fleet_runtime_usage.sh` | fleet 资源占用快照 |
| `fleet_blackbox_probe.py` | 只读黑盒验收：`/health` `/state` `/schedule` `/outbox`，不动状态、不投递，输出匿名化报告 |
| `napcat_notice.py` | AstrBot 停掉之后仍然能发一条 QQ 通知：临时开一个不外发布的 OneBot 接口，发完还原配置 |
| `pause_service.sh` | 彻底下线测试栈：先收号码 → 停 AstrBot 与 fleet → 发通知 → 停测试 NapCat → 核对 |
| `relaunch.sh` | 受控重启入口 |
| `backup.ps1` | Windows 工作区快照 |
| `check_versions.py` | 检查 CHANGELOG / Runtime / 插件版本一致性 |

工具在 fleet 容器中的典型用法：

```bash
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py list
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py check default:FriendMessage:1000000001
```

## 门禁

```bash
python -m pytest runtime/tests/test_ops_scripts_are_portable.py -q
python -m pytest runtime/tests/test_public_tree_is_sanitized.py -q
python3 scripts/check_versions.py
```

机器相关值必须是参数、环境变量，或从脚本位置推导；公开树禁止出现作者 home、真实私网主机、
封测昵称/健康信息与真实 roster。完整流程见 [`docs/部署与运维.md`](../docs/部署与运维.md)。
