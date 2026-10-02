# Actual-action post-repair review（HEAD `1fd3743`）

## 结论

**PF-001 不应判为完全关闭；最多是 `partial / runtime_v2 path closed, langchao/live + learnability open`。T07 也未关闭。**

修复已把最终渲染文本的可审计 witness 从 render 绑定到 send，并在 `runtime_v2/live` 成功 ACK 后写入 exposure action；但训练编码明确不使用这些文本事实，所以“同一计划、不同最终措辞”在拟合矩阵中仍完全不可区分。更严重的是 `langchao/live` 的 ACK exposure 路径仍传 `ack.action`（计划动作），没有合并 `ack.actual_action_witness`，导致该模式再次丢失实际文本 hash/revision。

## 全链核查

| 环节 | 判定 | 证据/说明 |
|---|---|---|
| render → send witness | 通过（有限） | `actual_action_v21.py:97-145` 计算 NFC 文本 SHA-256 与绑定 attempt/render-outbox/send-outbox、renderer/template/encoder 的 revision；`reducer.py:1650-1663,1735-1743` 写入 send payload。 |
| plan → render scope gate | 部分通过 | 仅当外部 semantic review 为 `approved` 才检查 asks_reply/pressure/commitment/completion（`actual_action_v21.py:148-177`）。缺 review 时为 `UNATTRIBUTABLE` 且直接放行，不构成语义 T07 的普遍门禁。完成声明另有 capability witness gate，但不等于覆盖全部实际动作语义。 |
| runtime_v2 ACK → exposure | 通过（有限） | `runtime_v2.py:847-888` 用 `actual_action_for_exposure` 把 witness、revision、attribution 合入 action；PostgreSQL adapter 将 action 与 feature snapshot JSON 持久化（`user_model_v2_service_repository.py:220-239`）。因此 hash/revision **在 runtime_v2 新成功发送路径可持久、可重读**。 |
| feature → label → fit | 不满足“训练区分” | `encode_features_v2` 只编码 13 个既有计划/上下文字段（`user_model_v2_features.py:241-259`）；测试还断言不同实际文本不改变向量（`test_actual_action_v21.py:74-87`）。fit 只取 `features.values`（`user_model_v2_service.py:280-320`）。两个相同计划、不同最终文本虽有不同 exposure provenance，却产生相同 design row，模型无法学习措辞差异。 |
| post-treatment leakage | 当前文本 witness 未泄漏进模型，但需限定表述 | 最终文本/hash/semantic review 未进入向量，故没有由这些字段造成的 post-treatment feature leakage；这是以牺牲文本可学习性换来的隔离。代码中的 `forbidden` 检查是对 feature-name 常量的未来防护，并非对输入 action 的拒绝，但当前显式 encoder 确实忽略 witness。预测/expectation 在 ACK exposure 事务内生成，context 也是 ACK 时调用，若将其宣称为“发送前预测”则时序口径仍需谨慎。 |
| langchao/live ACK → exposure | **失败** | composition 已接上 exposure repository/user model（`composition_v2.py:261-279`），但 `LangchaoLiveRunner.after_legacy_send_ack` 在 `langchao_live_wiring.py:95-112` 传入的是 `action=ack.action`，忽略同时存在的 `ack.actual_action_witness`。API 明明在 `api_v1.py:1496-1508` 提供了 witness。故浪潮 live exposure/labels 已创建，但实际文本 provenance 丢失，训练与审计退回计划动作。 |

## PF-001 / T07 判定

- **PF-001：reopen / partial。** 若狭义定义只是“runtime_v2 exposure 留存实际文本 hash/revision”，该子项可算关闭；若原义包含 actual-action→exposure→feature/label/fit 全过程和所有 live engine，则不能关闭：
  1. `langchao/live` 未携带 witness；
  2. fit 对不同最终文本完全不可区分；
  3. 现有测试只验证内存 witness、send payload 和向量相同，没有 PostgreSQL round-trip，也没有 langchao/live ACK exposure 断言。
- **T07：保持 blocked。** postrepair 现有清单本身仍将 canonical `LC-PRA-02` / T07 标为 blocked。当前 gate 依赖 approved semantic review；unknown/rejected 不会触发范围漂移阻断，不能证明所有最终发送文本都不会伪造实践、产物或反馈。若这里沿用旧 T07“计划—最终动作范围偏移”含义，结论仍是部分关闭而非关闭，理由相同。

## 只读测试

执行：

```text
python -m pytest -q runtime/tests/test_actual_action_v21.py runtime/tests/test_langchao_live.py runtime/tests/test_user_model_v2_service.py runtime/tests/test_user_model_v2_service_repository.py
```

结果：`25 passed`。这些通过项未覆盖上述 langchao/live witness 丢失，也未证明拟合能区分最终文本。

## 最小后续验收条件

1. `LangchaoLiveRunner.after_legacy_send_ack` 与 runtime_v2 使用同一个 actual-action 合并函数，并新增成功 ACK → PostgreSQL exposure round-trip 测试，断言 text SHA/revision/attempt/outbox identities 全部一致。
2. 明确产品合同二选一：若模型必须学习最终措辞，需设计**发送前同定义、可用于预测**的文本/模板特征；不能把发送后才知道的字段直接塞入预测特征。若只保留审计 provenance，则不得声称“训练区分不同文本”。
3. 增加成对 fit 测试：同 plan/context、不同 final text 的样本应按预先声明的安全特征定义可区分；否则 PF-001 只能按“审计归因”子项关闭。
4. T07 需 fail-closed 的语义审查覆盖或可机械验证的完整声明 witness，并跑真实 PostgreSQL/live canary；unknown review 不应被当作已证明安全。
