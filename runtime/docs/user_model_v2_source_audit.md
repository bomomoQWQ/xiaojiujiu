# 用户模型 v2 源码核验与施工基线

日期：2026-09-30  
状态：M−1 / M0 已核验；生产 Runtime 已下线，真实 QQ 通道已切断，模拟 OneBot 已接管。

## 1. 已确认的施工决策

- v1 用户模型、标签、参数与动机效用弃用，不进入生产兼容双轨。
- AstrBot 插件使用的 HTTP `/v1` wire protocol **保留**；“弃用 v1”不指删除 `api_v1.py`，而是内部直达 v2 application service。
- Runtime 目标后端为 PostgreSQL-only，使用 PostgreSQL 原生类型与版本化迁移。
- 数值实现使用 NumPy + SciPy：MAP 优化、Hessian/Cholesky、区间与参考对照；不自写线代，不引入 JAX/PyTorch。
- Jev 仅保留 disabled 接口和测试替身，真实接入后置。
- 旧 SQLite 只作为冻结迁移输入；旧伪标签不直接进入 v2。

## 2. 服务下线证据

- 公告：从 `/fleet-data/people.json` 取 12 个在册对象，排除模拟号 `20001`，AstrBot Open API 11/11 成功；NapCat 日志逐条出现 11 条“发送 -> 私聊”。
- 已停止：`xxj-napcat-test`、`xxj-runtime-fleet`、`xxj-runtime-test`。
- 保持：`astrbot-test`。
- 模拟前端：`xxj-onebot` 已连接 `ws://astrbot:6199/ws`，`errors=0`；与 NapCat 不并存。
- 完整备份：`/home/bomomo/tmp_backups/v2-rebuild-20260930-185858`，包含 AstrBot bind mount、Runtime 数据卷、NapCat QQ profile、容器 inspect、Git HEAD/状态；`SHA256SUMS` 全部通过。

## 3. 阻断级源码事实

### 3.1 结果时特征泄漏

预测在 `runtime.py:1723-1730` 使用动作前 `_situation_context(stamp)`；训练却在回复/超时结算时重新计算当前 context：

- `runtime.py:737-747`：无回复结算；
- `runtime.py:2553-2563`：公开观察入口；
- `runtime.py:2719-2729`：自动回复归因。

`runtime.py:2824-2843` 会按结算时刻重算 busy、recent contacts、hours since contact、user active、permission、ever boundary。回复本身会使 `user_active_now=true`，也会使 busy 进入 replied_recently 分支；这是标签造成特征的未来信息泄漏。

**M−1 要求**：实际发送确认时冻结 action/context snapshot、feature version、context cutoff；所有标签只引用快照。

### 3.2 观察缺少 exactly-once

自动归因在 `runtime.py:2710` 有 `observation_for_attempt` 软守卫；公开 `observe_reply` 在 `runtime.py:2538-2563` 无守卫，对已关闭 attempt 仍可 observe（`:2564-2567`）。`interaction_observations.attempt_id` 无唯一约束（`db.py:142-155`）。

**M−1 要求**：数据库稳定幂等键 `(scope_key, exposure_id, target, label_version)`；只有首次插入者能应用模型更新；晚到/纠正走 superseding label + 重建。

### 3.3 缺失字段被当作 false

`BehaviourReaction` 的 replied/continued_topic/asked_back/explicit flags/boundary_touched 默认均为 false（`user_model.py:415-443`）；正常自动归因只填 replied/delay/length（`runtime.py:2711-2715`）。随后 `_target_rewards` 把缺失 continued/asked_back 当作负扣分，并制造 continue/boundary 标签（`user_model.py:1392-1412`）。

**M1 要求**：每目标独立状态和 mask；missing/pending/censored/unknown/unattributable 不更新对应头。

### 3.4 旧动机效用语义错误

- `motivation.py:330-359`：`bad=(1-pos)(1-cont)`；
- `motivation.py:362-387,467-477`：`1-uncertainty` 折扣坏成本，却不对称折扣正收益；
- 当前源码固定示例复算：uncertainty 0→1 时旧两项合计 `-0.16772 → +0.09700`。

**M3 要求**：改为可观察 R/C/N 加性效用；收益读下界，负面读上界；A 默认不进总体效用。

### 3.5 精度/区间不是合法 Laplace 近似

- `user_model.py:1469-1489`：缺 `p(1-p)`，且 `x=0` 特征也统一增加 `.25w` 精度；
- `user_model.py:1201-1223`：base precision 再乘 `1+class_count`；delta 没有方差；
- `tick_drift` 只衰减 base precision（`:1518-1578`），class_count 永久压低方差；
- recency floor、per-observation drift、time drift 三套老化并存。

**M2 要求**：版本化有效标签快照上做一次真实时间折扣；联合 MAP + Laplace 参考：`H=prior+Σw p(1-p) ξξᵀ`；每头输出 logit mean/variance 与概率区间。

### 3.6 当前数据模型无法表达 v2

`interaction_observations` 无逐目标状态、窗口、标签修订、预测快照或幂等键；`user_model_params` 无契约/特征/先验/估计器/参数版本及活动指针。当前后台任务协议也无 fit/wait settlement/label rebuild 的依赖 manifest。

**结论**：并行新表 + 旧事实重建；不在旧表上原位换含义。

## 4. 可复用基础

- raw event 来源链；
- 单写事务与 savepoint；
- PostgreSQL advisory lock；
- attempt 状态机与 transition log；
- outbox 租约/ack/发送幂等；
- 全局和话题硬边界在效用前剪枝；
- 候选池与沉默选项；
- hazard 使用真实 elapsed_seconds 积分；
- decision payload 保存全部候选预测/效用；
- 自动归因按 conversation 查最新 SENT attempt。

## 5. PostgreSQL-only 目标边界

当前 `db_postgres.py` 复用 SQLite 的 TEXT/REAL/INTEGER DDL，仅是兼容后端，不是目标态。v2 使用 PostgreSQL 原生 schema：

- `TIMESTAMPTZ`, `JSONB`, `DOUBLE PRECISION`, `BIGINT`, `BOOLEAN`；
- 所有用户态表显式 `scope_key`；
- 版本化 migration ledger；
- exposure、target label revision、prediction/expectation/wait、parameter snapshot/active pointer；
- PostgreSQL 外部 `pg_dump`/恢复验证替代 SQLite durability 命令。

当前线上一人一 SQLite 的隔离由部署保证。迁移进入共享 PostgreSQL 后，scope 必须成为数据库约束与每条查询的必要条件。

## 6. 测试基线

- 当前完整 Runtime suite 已执行通过，说明 legacy 内部一致，不说明符合 v2。
- 旧测试中 busy 软标签、bad 补集、uncertainty 折扣坏成本、聚合 effective count/uncertainty 等必须保留为 legacy 回放，而不是 v2 语义门禁。
- v2 按批准文档 §22 建独立测试矩阵；切换后只要求 `/v1` wire 兼容，不保留旧模型语义。

## 7. 更新后的施工顺序

```text
M−1  冻结发送时 action/context；观察 exactly-once；失败动作禁止训练
M0   PostgreSQL-only 基础、scope 决议、legacy 可复现基线
M1   四目标、窗口、状态与标签修订契约
M2   v2 原生 schema、参数快照/活动指针、重建器
M3   NumPy/SciPy MAP + Laplace、逐头区间、唯一 conservative readout
M4   新 R/C/N 效用、冷启动消费者、事项级重复成本、完整决策日志
M5   expectation/wait ledger 与情绪接口
M6   SQLite 事实重建、离线回放、模拟 OneBot、影子/灰度、恢复演练
M7   单独标定证据权重和 hazard；真实 QQ 恢复需另行放行
```
