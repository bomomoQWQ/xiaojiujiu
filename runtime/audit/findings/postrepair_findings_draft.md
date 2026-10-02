# 浪潮修复后 findings 对比草案

> **性质**：当前 HEAD + 工作区审查草案，不是最终报告，不构成上线授权。  
> **HEAD**：`94ee1628b0ba5f3947161432e1fffe8cf7b476d4`（相对原审查基线 `32bd8a2...`）。  
> **工作区**：无 tracked 修改；有与本审查无关的 untracked 项。  
> **规则**：只有“代码实现/接线 + 本轮实际测试”才可标 `closed/passed_limited`；未生产接线或未跑 PostgreSQL 的保持 `open`。

## 本轮实际执行

```text
python -m pytest -q \
  runtime/tests/test_actual_action_v21.py \
  runtime/tests/test_capability_witness.py \
  runtime/tests/test_pf002_social_wiring.py \
  runtime/tests/test_langchao_user_outcomes.py \
  runtime/tests/test_composition_v2.py \
  runtime/tests/test_privacy_deletion.py \
  runtime/tests/test_pf011_goal_lifecycle.py \
  runtime/tests/test_langchao_permission_and_no_send.py \
  runtime/tests/test_langchao_attention_recipe.py \
  runtime/tests/test_langchao_exploration.py \
  runtime/tests/test_langchao_live.py \
  runtime/tests/test_langchao_runtime_adapter.py
```

结果：**99 passed, 1 skipped**。

PostgreSQL audit 文件单独执行结果：**8 skipped**。因此本草案没有修复后 PostgreSQL T，也没有 R。

## 12 findings 汇总

| ID | 修复后状态 | 验证 | 判定摘要 |
|---|---|---|---|
| PF-001 | **closed** | passed_limited | actual-action witness 已绑定 render/send/exposure；本轮正负测试通过 |
| PF-002 | **open** | passed_limited | social composition 代码与隔离测试有，但默认开关关闭、无 PG production smoke |
| PF-003 | **open** | passed_limited | ACK→exposure 与 label→actual/correction 已实现；无 PG 全闭环及下一轮 reward 证明 |
| PF-004 | **closed** | passed_limited | 默认 all-one 被明确冻结为 non-informative；可选 recipe 有校验测试，不声称已动态启用 |
| PF-005 | **open** | passed_limited | 非零竞争仅 recipe/消融；生产默认仍 `competition_gain=0` |
| PF-006 | **open** | passed_limited | standalone shadow route 已修；无 PG authority truth-table/持久化计数 |
| PF-007 | **open** | passed_limited | deletion coordinator/API/work queue 有隔离测试；fake DB，PG 跨表传播未跑 |
| PF-008 | **open** | passed_limited | exact artifact witness gate 有代码/测试；无 PG registry + live claim 全链 |
| PF-009 | **open** | passed_limited | 狭义完成声明会要求 witness；缺 reducer 集成正负测试、PG 重验和完整声明类型覆盖 |
| PF-010 | **closed** | passed_limited | approved semantic drift 会阻断，等价低压正控可达；本轮 reducer 测试通过 |
| PF-011 | **open** | passed_limited | 生命周期纯模块有测试，但未生产调用/持久化接线 |
| PF-012 | **closed** | passed_limited | canonical ID/crosswalk 与冲突校验已修；沿用已有执行证据 |

合计：**closed 4 / open 8**。关闭项均只在隔离代码入口和执行过的测试范围内成立。

## 关键判定说明

### 可关闭

- **PF-001 / PF-010**：`actual_action_v21.py`、`reducer.py`、`runtime_v2.py`形成实际文本 witness、scope-drift gate 和 exposure provenance；`test_actual_action_v21.py`本轮执行覆盖不同最终文本、unknown→UNATTRIBUTABLE、压力漂移阻断、低压正控及send outbox绑定。
- **PF-004**：整改退出条件允许“明确冻结为非信息常量”。当前默认仍是 all-one，且新增可选 recipe 的严格校验；关闭的是能力口径/冻结不变量，不是动态 attention 上线。
- **PF-012**：canonical registry、namespaced aliases 和 fail-closed validator 已有实现及历史执行证据；不重新解释历史裸 Txx。

### 仍开放

- **PF-002/PF-003/PF-006/PF-007/PF-008**：虽然代码和隔离测试明显改善，但都依赖 PostgreSQL composition、持久化或跨 repository 行为；本轮 PG 测试全部 skip，不能关。
- **PF-009**：有 `rendered_completion_requirement` 与 reducer fail-render 接线，但测试只直接覆盖 parser/validator，未覆盖 reducer 真实完成声明正负流；完成声明识别还是有限正则。
- **PF-011**：虽新增 `langchao_goal_lifecycle_service.py` 持久化 application boundary，但未找到 composition/event producer 调用，也没有 service/PG 测试，故不能把纯规则测试写成生产生命周期关闭。
- **PF-005**：默认 production recipe 仍无竞争，整改要求只能维持 capability limitation。

## 与 T01–T32 的影响

详见 `runtime/audit/postrepair_results_draft.json`。总原则：

1. 原本已由真实隔离测试支持的项目可维持 `passed_limited`；
2. 新修复对应的部分 oracle可升级，但未覆盖完整 scenario fixture 时仍 `evidence_insufficient/open`；
3. PG 相关项目不因 schema/test 文件存在升级；
4. T31/T32 的授权真实运行和 external canary 继续 `blocked`；
5. 不声称 T01–T32 全部通过，不声称可上线。

## 草案结论

修复后机械门禁与治理原语显著增强，但当前仍应维持 **`hold_not_authorized / do_not_launch`**：8 个 finding 保持 open，修复后 PostgreSQL 执行为 0，且无授权真实运行 R、无 external Langchao live canary。
