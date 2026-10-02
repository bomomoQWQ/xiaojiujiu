# 浪潮修复后结果正式候选

> 基线：用户指定 `1fd3743615fe12211acc42dc30270aa31ef65e9b`。本文件是审计正式候选，不构成上线授权。  
> 并发提示：执行期间其他会话推进了 HEAD；这里只归属推进前在 `1fd3743` 上取得的证据。

## 总判定

- **release decision**：`do_not_launch`
- **findings**：`closed=4 / open=8`
- **T01–T32**：`passed_limited=17 / evidence_insufficient=9 / blocked=6 / passed_unqualified=0`
- **focused tests**：113 passed / 1 skipped（114 tests）
- **PostgreSQL audit**：0 passed / 8 skipped
- **完整套件**：指定 HEAD 上 collection blocked（`test_user_model_v2_postgres_integration.py:449` SyntaxError），不是全绿证据
- **R / external live canary**：0 / 0

## 新纳入证据

1. **permission**：事件时间投影、摄入顺序无关、撤权无 claim/backlog、重新允许后旧轮仍失效，只有 fresh round 可 claim。
2. **attention**：默认 off/all-one；B3 必须 exact scope allowlist，非法 recipe fail closed；显式 signals、recipe audit/parameter versions、competition edges 有隔离正控。
3. **exploration**：只有完整且明确结束的 internal segment 才映射 `exploration.v1`；disabled/incomplete 不接纳；选中后保持 internal，不能调用 send bridge。
4. **lifecycle**：application service 对 explicit completed/cancelled 事件持久化 goal/candidate terminal revisions，拒绝旧 summary 重开，且没有 outbox/claim collaborator。

## T01–T32

| T | Canonical | 状态 | 本轮证据判定 |
|---|---|---|---|
| T01 | LC-MAT-01 | evidence_insufficient | 缺完整 fixture 与 S。 |
| T02 | LC-MAT-02 | passed_limited | 隔离入口范围。 |
| T03 | LC-MAT-03 | passed_limited | unknown 组件范围。 |
| T04 | LC-MAT-04 | blocked | permission/witness 局部有 T；PF-008 无 PG/live 全链。 |
| T05 | LC-MAT-05 | passed_limited | social 局部；全域删除未证。 |
| T06 | LC-PRA-01 | evidence_insufficient | scope-drift 正负控通过；完整 scenario/PG/S 不足。 |
| T07 | LC-PRA-02 | blocked | completion witness 仍缺 reducer 集成与 PG。 |
| T08 | LC-PRA-03 | passed_limited | 内部终态幂等，不是外部 exactly-once。 |
| T09 | LC-PRA-04 | passed_limited | actual-action witness 隔离 T；无 PG/send R。 |
| T10 | LC-PRA-05 | evidence_insufficient | 新 permission revoke→fresh-round T；无 PG 全生命周期。 |
| T11 | LC-AGY-01 | passed_limited | isolated/synthetic，无 external live。 |
| T12 | LC-PRA-06 | passed_limited | pending/no-blind-redelivery 局部。 |
| T13 | LC-CON-01 | evidence_insufficient | B3 正控改善，完整干预 oracle 不足。 |
| T14 | LC-HIS-01 | passed_limited | 数值核。 |
| T15 | LC-HIS-02 | passed_limited | 已测 CAS 路径。 |
| T16 | LC-HIS-03 | passed_limited | 局部失效，非全域删除。 |
| T17 | LC-HIS-04 | evidence_insufficient | 完整 fixture/oracle 不足。 |
| T18 | LC-PUR-01 | passed_limited | 登记存在，生产消费有限。 |
| T19 | LC-PUR-02 | passed_limited | 算术局部；PF-003 仍 open。 |
| T20 | LC-PUR-03 | passed_limited | shadow T 明确 protected outputs 全零；无 PG delta。 |
| T21 | LC-PUR-04 | passed_limited | isolated/synthetic。 |
| T22 | LC-AGY-02 | evidence_insufficient | 缺生产恢复轨迹。 |
| T23 | LC-AGY-03 | **evidence_insufficient** | **由 draft 的 blocked 降级**：已不再只是纯模块，composition/runner admission 和 internal no-send 有实际 T；但无真实 producer、PG ledger/轨迹，不能 pass。 |
| T24 | LC-AGY-04 | evidence_insufficient | service boundary T 增强；无生产 terminal-event producer/PG。 |
| T25 | LC-MAT-06 | evidence_insufficient | canonical S 未执行。 |
| T26 | LC-CON-02 | blocked | taxonomy/permission 局部 T；无统一 PG 审计链。 |
| T27 | LC-PRA-07 | passed_limited | 隔离 provenance；无 PG/external send。 |
| T28 | LC-MAT-07 | blocked | revoke T 增强；PF-007/PG 删除传播未解。 |
| T29 | LC-CON-03 | passed_limited | 冻结数值 fixture。 |
| T30 | LC-VER-01 | passed_limited | offline synthetic；B3 runtime 正控不等于用户效果。 |
| T31 | LC-VER-02 | blocked | 无授权 R。 |
| T32 | LC-PRA-08 | blocked | fixed artifact + FakeBridge blackbox 明确没有 transport，不是 external canary。 |

## 与 draft 的实质变化

仅 **T23** 从 `blocked` 调为 `evidence_insufficient`：新的实际测试证明 exploration 已接到 composition/runner 的受限 internal/no-send 路径，因此“纯模块未接线”不再准确；但没有生产事件生产者、PG process/artifact ledger 或真实运行，不满足 `passed_limited`。

PF 总数不变：新 permission/attention/exploration/lifecycle tests 提升证据强度，但没有越过各 PF 的生产/PG 退出条件。

## 门禁结论

继续 **`hold_not_authorized / do_not_launch`**。不得把 focused 113 pass 外推为 PostgreSQL、生产、外部发送、真实用户效果或上线授权。
