# 浪潮修复后结果草案

> 非最终报告；不构成上线授权。HEAD=`94ee1628b0ba5f3947161432e1fffe8cf7b476d4`。

## 判定

- **release decision**：`do_not_launch`
- **findings**：12 项中 `closed=4`、`open=8`
- **T01–T32**：`passed_limited=17`、`evidence_insufficient=8`、`blocked=7`、无无条件 pass
- **本轮隔离测试**：99 passed / 1 skipped
- **本轮 PostgreSQL audit**：0 passed / 8 skipped
- **R 证据**：0

## T01–T32 修复后状态

| T | Canonical | 状态 | 修复后说明 |
|---|---|---|---|
| T01 | LC-MAT-01 | evidence_insufficient | 缺完整事实投影→预测→决定fixture与S |
| T02 | LC-MAT-02 | passed_limited | 作用域隔离入口维持通过 |
| T03 | LC-MAT-03 | passed_limited | unknown纯函数/组件范围通过 |
| T04 | LC-MAT-04 | blocked | witness组件已修，但PF-008无PG/live claim全链 |
| T05 | LC-MAT-05 | passed_limited | social来源哈希/失效局部通过 |
| T06 | LC-PRA-01 | evidence_insufficient | scope-drift gate通过；完整scenario/PG不足 |
| T07 | LC-PRA-02 | blocked | 完成声明witness部分修；PF-009仍open |
| T08 | LC-PRA-03 | passed_limited | 内部终态幂等，不等于外部平台exactly-once |
| T09 | LC-PRA-04 | passed_limited | actual-action attribution由failed升级；限隔离测试 |
| T10 | LC-PRA-05 | evidence_insufficient | permission revision/no-send改善；无PG生命周期 |
| T11 | LC-AGY-01 | passed_limited | synthetic/isolated可达，无external live |
| T12 | LC-PRA-06 | passed_limited | pending/no-blind-redelivery局部通过 |
| T13 | LC-CON-01 | evidence_insufficient | 消融仍只支持synthetic mechanics |
| T14 | LC-HIS-01 | passed_limited | elapsed-time隔离数值核 |
| T15 | LC-HIS-02 | passed_limited | 限已测CAS路径 |
| T16 | LC-HIS-03 | passed_limited | 局部social失效；非全域删除 |
| T17 | LC-HIS-04 | evidence_insufficient | fixture/oracle仍不完整 |
| T18 | LC-PUR-01 | passed_limited | 登记存在，生产指标消费有限 |
| T19 | LC-PUR-02 | passed_limited | actual/correction算术通过；PF-003仍open |
| T20 | LC-PUR-03 | passed_limited | standalone shadow route改善；无PG计数 |
| T21 | LC-PUR-04 | passed_limited | synthetic/isolated表达可达 |
| T22 | LC-AGY-02 | evidence_insufficient | 缺生产恢复轨迹 |
| T23 | LC-AGY-03 | blocked | exploration纯模块未生产接线 |
| T24 | LC-AGY-04 | evidence_insufficient | lifecycle规则/application boundary有，缺生产event producer与PG测试 |
| T25 | LC-MAT-06 | evidence_insufficient | canonical oracle双评未执行 |
| T26 | LC-CON-02 | blocked | taxonomy有实现，无统一PG全链 |
| T27 | LC-PRA-07 | passed_limited | actual-action provenance补强；无PG/send R |
| T28 | LC-MAT-07 | blocked | deletion/permission原语有，跨存储PG传播无 |
| T29 | LC-CON-03 | passed_limited | 冻结数值fixture |
| T30 | LC-VER-01 | passed_limited | 仅offline synthetic |
| T31 | LC-VER-02 | blocked | 无授权真实效果R |
| T32 | LC-PRA-08 | blocked | 无external live canary与恢复 |

## 状态变化要点

- **T09**：从 `failed` 升为 `passed_limited`。原因是实际动作 witness、不可归因状态及 exposure provenance 已有代码且本轮实际测试通过；不代表生产/PG通过。
- **T27**：从 `evidence_insufficient` 升为 `passed_limited`，范围仅限 render/send/exposure 的隔离 provenance。
- **T06**：机械 scope-drift 修复可验证，但完整反馈归因 scenario 不足，所以仍不升为通过。
- **T04/T07/T10/T23/T26/T28**：虽有新原语/测试，因对应 finding open、未生产接线或未PG运行，保持 blocked/evidence_insufficient。
- **T31/T32**：授权和真实运行条件无变化，继续 blocked。

## 上线门禁

仍不得上线，至少因为：

1. 8 个 finding 保持 open，含 P0；
2. 修复后 PostgreSQL 测试全部 skip；
3. 无授权真实运行 R；
4. 无 external Langchao live canary；
5. social、deletion、artifact registry、finite lifecycle、exploration等部分能力仍只有隔离模块或开关代码。
