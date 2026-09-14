# tests/fixtures

本目录存放**可选的**夹具数据文件，默认留空。

## 为什么不把 fixture.jsonl 提交进仓库

测试**不依赖**这里的任何文件。所有测试都通过
`qboss_training.fixtures.build_fixture_records()` 在 `tmp_path` 里
现生成夹具（见 `tests/conftest.py` 的 `fixture_records` / `fixture_path` 夹具）。

这样做的原因：

* 夹具是**生成物**，提交它等于把派生数据当源码，容易与实际生成逻辑不一致
  （改了 `fixtures.py` 却忘了重新生成，测试就会用旧数据，掩盖回归）；
* 测试现生成能保证夹具与生成逻辑**永远同步**；
* 夹具本身句式极有限，**不能当训练数据**，放进仓库容易被误用。

## 需要固定夹具时（人工排查 / 跨机对比）

如果要在多台机器之间比对同一份夹具，或做人工排查，可以显式生成：

```bash
python -m qboss_training.fixtures make \
  --output tests/fixtures/fixture.jsonl \
  --event-eval 30 --emotion-explain 30

# 混入故意违规的样本，用于确认校验器真的能抓到问题
python -m qboss_training.fixtures make \
  --output tests/fixtures/fixture_invalid.jsonl \
  --event-eval 30 --emotion-explain 30 --include-invalid
```

生成是**确定性**的（固定 `--seed`，默认 1234），所以同样参数在任何机器上
都会得到逐字节相同的结果。

`.gitignore` 已忽略 `data/fixtures/`；本目录下的文件请按需自行决定是否提交。
