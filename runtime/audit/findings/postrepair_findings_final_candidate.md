# 浪潮修复后 findings 正式候选

> **候选性质**：基于用户指定 HEAD `1fd3743615fe12211acc42dc30270aa31ef65e9b` 的 C+T 证据重算；不是上线授权。  
> **规则**：逐 PF 只按实际静态接线和本轮保存测试关闭；没有 PG、外部平台或真实用户证据时不外推。  
> **并发警告**：开跑时 HEAD 已验证为 `1fd3743` 且无 tracked 修改；执行期间其他会话把 HEAD 推进到 `c880b6a/fc53ee1`。本候选只把推进前取得的 focused/PG 结果计入 `1fd3743` 判定。

## 实际证据

- Focused postrepair tranche（加入新的 permission / attention / exploration / lifecycle tests）：**114 tests = 113 passed, 1 skipped, 0 failed/errors**。
- PostgreSQL audit：**8 skipped，0 passed**，因此没有新的 PG T。
- 静态：`python -m compileall -q src` 在并发推进前通过；`ruff` 未安装，未执行。
- 全套 `pytest tests` 在用户指定基线上收集时被 `test_user_model_v2_postgres_integration.py:449` 的未闭合字符串阻断，不能计作全套通过。
- R：**0**；external live canary：**0**。

保存证据：

- `runtime/audit/evidence/postrepair_focused_final_candidate.txt`
- `runtime/audit/evidence/postrepair_focused_final_candidate.junit.xml`
- `runtime/audit/evidence/postrepair_postgres_final_candidate.txt`
- `runtime/audit/evidence/postrepair_postgres_final_candidate.junit.xml`
- `runtime/audit/evidence/postrepair_full_final_candidate.txt`

## PF-001–PF-012

| ID | 状态 | 判定（仅实际证据） |
|---|---|---|
| PF-001 | **closed / passed_limited** | actual-action witness 的 render/send/exposure 正负测试实际通过；不外推 PG/外发/R。 |
| PF-002 | **open / passed_limited** | social composition 仅显式开关构造，默认关闭；无 PG production smoke 和完整 live 链。 |
| PF-003 | **open / passed_limited** | ACK/exposure 与 label outcome 隔离测试通过；无 PG 到下一轮 reward 消费闭环。 |
| PF-004 | **closed / passed_limited** | off 明确保持 all-one/non-informative；新增 B3 exact-scope allowlist、显式 signals、版本审计和 fail-closed 测试通过。关闭的是口径/门禁，不是默认动态启用。 |
| PF-005 | **open / passed_limited** | B3 隔离正控能传 `competition_gain=1.0` 与 edges；生产默认仍 off/0，无批准 scope PG/生产执行。 |
| PF-006 | **open / passed_limited** | shadow router/runner 隔离测试含零 protected outputs；无 PG authority truth-table/持久化 delta。 |
| PF-007 | **open / passed_limited** | deletion fake-repository 测试通过；无 PG 跨表传播、suppression、重试对账。 |
| PF-008 | **open / passed_limited** | exact witness 组件正负测试通过；无 PG registry + live claim 全链。 |
| PF-009 | **open / passed_limited** | completion parser/validator 局部通过；仍缺 reducer 级完成声明正负集成和 PG 重验。 |
| PF-010 | **closed / passed_limited** | 范围/压力漂移负控和等价低压正控实际通过；依赖 approved semantic review。 |
| PF-011 | **open / passed_limited** | 新 lifecycle service 测试证明完成/取消持久化边界且无 outbox/claim；静态搜索未找到生产 terminal-event producer 调用，且无 PG 事务测试。 |
| PF-012 | **closed / passed_limited** | canonical crosswalk/validator 沿用既有保存证据；本轮未重跑对应 manifest test，不扩大声称。 |

合计：**closed 4 / open 8**。与草案数量相同，但新证据强化了 PF-004 的 recipe 门禁、PF-005 的 B3 隔离正控，以及 PF-011 的 application boundary；这些证据仍不足以关闭生产/PG 缺口。

## 结论

保持 **`hold_not_authorized / do_not_launch`**：仍有 8 个 open findings（含 P0），PG 后修复执行为 0，完整测试套件在指定 HEAD 上未形成绿证据，无 R 和 external live canary。
