# 理解痞老板 · 2B 事件评价 / 情绪解释 训练工程

为 `内源主动型长期陪伴AI_Runtime` 训练本地小模型（目标 **Qwen/Qwen3.5-2B，纯文本**）的完整工程：
数据生成 → 离线校验 → 切分 → 聊天 SFT 构建 → LoRA 训练 → 评测 → 合并 → GGUF CPU 导出 → 基准。

本工程**只负责训练侧**，不改动 `../AstrBot/`，也不修改 Runtime 设计文档。

---

## 0. 这个模型到底做什么

2B 模型在 Runtime 里只承担两件事，都来自架构文档：

| 职责 | 章节 | 输入 | 输出 |
| --- | --- | --- | --- |
| **事件评价** | §8 | 关键当前事件 + 少量上下文 + 背景心境 + 角色价值观 | `direction / impact / activation / uncertainty / relation_signal / responsibility / confidence / evidence` |
| **情绪解释** | §11 | 事件 + 背景心境 + 活跃情绪 + 冲动/节制 | `experience / focus / conflict / impulse / inhibition / expression` |

两条硬约束（本工程用代码而不是提示词来保证）：

* **§8.1** 事件评价**不输出最终情绪值**。2B 只回答"这件事是什么性质"，
  最终情绪由 Runtime 代码结合价值观/背景心境/用户模型/旧情绪状态计算。
  → schema 里**没有** `jealousy`/`valence`/`intensity` 这类字段，
  且不变量 `EV05_EMOTION_VALUE_LEAK` 会拦截任何越界输出。
* **§11.3** 情绪解释器：不得创造输入中不存在的事件、不得放大轻微情绪、
  不得压低/抬高底层情绪、不得决定行为、不得生成台词、不得修改状态。
  → 对应不变量 `EX01 / EX01B / EX02 / EX03 / EX06 / EX07 / EX08 / EX09`。

---

## 1. 快速开始

### 1.1 环境

```powershell
# 核心依赖（数据管线 + 全部离线测试；不需要 GPU / 不需要模型）
pip install -r requirements.txt

# 训练依赖
pip install -r requirements-train.txt
# torch 请按 CUDA 版本单独装：
#   pip install torch --index-url https://download.pytorch.org/whl/cu121

# 推理/量化依赖
pip install -r requirements-infer.txt

# 安装本工程（提供 qboss 命令）
pip install -e .
```

### 1.2 设置 API key（唯一方式）

本工程**绝不接受** API key 参数，也**绝不**把 key 写入任何文件（配置、断点、日志、报告都没有）。
key 只从环境变量读：

```powershell
# PowerShell（仅当前会话有效）
$env:DEEPSEEK_API_KEY = "sk-..."

# bash
export DEEPSEEK_API_KEY=sk-...
```

`configs/generation.yaml` 里若出现 `api_key` / `token` / `secret` / `password`
等字段，配置加载器会**直接报错拒绝**，防止有人图省事把 key 写进仓库。

### 1.3 先跑一遍离线自检（不联网、不用模型）

```powershell
# 1) 生成合成夹具数据
python -m qboss_training.fixtures make --output tests/fixtures/fixture.jsonl

# 2) 校验 schema 与不变量
qboss validate --input tests/fixtures/fixture.jsonl --strict

# 3) 切分
qboss split --input tests/fixtures/fixture.jsonl --output-dir data/splits

# 4) 构建聊天 SFT（不加载 tokenizer 也能跑）
qboss sft --split-dir data/splits --output-dir data/sft --no-tokenize

# 5) 评测管线自检（gold 回放，应全部 100%）
qboss eval --input data/splits/test.jsonl --backend echo

# 6) CPU 基准格式自检
qboss bench --input data/splits/test.jsonl --backend echo --repeat 2

# 7) 全部测试
pytest
```

### 1.4 完整流水线

```powershell
# ① 生成数据（两种任务分别跑）
qboss gen --task event_eval       --target 400
qboss gen --task emotion_explain  --target 300

# ② 校验（CI 用 --strict：有失败即非零退出）
qboss validate --input data/raw/event_eval.jsonl data/raw/emotion_explain.jsonl --strict

# ③ 切分 + 防泄漏
qboss split --input data/raw/event_eval.jsonl data/raw/emotion_explain.jsonl `
            --output-dir data/splits --stratify-by task,direction

# ④ 构建 SFT jsonl（completion-only 掩码 + token 统计）
qboss sft --split-dir data/splits --output-dir data/sft `
          --model-path Qwen/Qwen3.5-2B --preview-masking

# ⑤ 冒烟测试（4 步，验证数据/掩码/adapter/保存全链路）
qboss train --config configs/lora_bf16.yaml --smoke

# ⑥ 正式训练
qboss train --config configs/lora_bf16.yaml

# ⑦ 评测（adapter 直接挂载）
qboss eval --input data/splits/test.jsonl --backend transformers `
           --model-path Qwen/Qwen3.5-2B --adapter-path outputs/lora-event-eval

# ⑧ 合并 adapter
qboss merge --adapter-path outputs/lora-event-eval --output-dir outputs/merged

# ⑨ 导出 GGUF（先看量化矩阵）
qboss gguf --matrix
qboss gguf --model-dir outputs/merged --output-dir outputs/gguf `
           --quants Q8_0,Q4_K_M,Q3_K_M --llama-cpp-dir E:\llama.cpp

# ⑩ CPU 基准（线程扫描 + 质量检查）
qboss bench --input data/splits/test.jsonl --backend llama_cpp `
            --gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf --threads 2,4,6

# ⑪ 量化后重新过门禁（低量化最容易坏的是结构化输出）
qboss eval --input data/splits/test.jsonl --backend llama_cpp `
           --gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf --gate
```

任一步骤都可重复执行：`gen` 幂等（断点续跑），`split` / `sft` 确定性（固定 seed）。

---

## 2. 目录结构

```text
training/
├── configs/
│   ├── generation.yaml          # 数据生成（含预算守卫、去重阈值）
│   ├── lora_bf16.yaml           # BF16 LoRA（首选）
│   └── qlora_fallback.yaml      # QLoRA 4bit（显存不足时）
├── schemas/                     # JSON Schema：模型输出的唯一事实来源
│   ├── event_eval.schema.json
│   ├── event_eval.input.schema.json
│   ├── emotion_explain.schema.json
│   └── emotion_explain.input.schema.json
├── src/qboss_training/
│   ├── contracts.py             # 任务契约：schema + 提示词 + 字段分类
│   ├── config.py                # 配置模型与加载（拒绝凭据字段）
│   ├── errors.py
│   ├── fixtures.py              # 合成夹具（离线测试用，不可当训练数据）
│   ├── inference.py             # 推理后端抽象（transformers / llama_cpp / echo）
│   ├── benchmark.py             # CPU 基准
│   ├── cli.py                   # 全部命令
│   ├── data/
│   │   ├── client.py            # OpenAI 兼容客户端（重试/预算/脱敏）
│   │   ├── generator.py         # 生成编排（断点续跑/去重/拒绝采样）
│   │   ├── prompts.py           # 教师提示词
│   │   ├── seeds.py             # 种子场景采样（覆盖度）
│   │   ├── splitter.py          # 确定性分层切分 + 防泄漏
│   │   └── io.py                # JSONL/JSON 原子读写
│   ├── validators/
│   │   ├── __init__.py          # schema 校验 + 批量报告
│   │   └── invariants.py        # 跨字段不变量 + 文本约束
│   ├── sft/format.py            # chat 模板 + completion-only 掩码
│   ├── eval/evaluator.py        # 评测指标
│   └── training/
│       ├── config.py            # 训练配置 + 冻结策略
│       ├── train.py             # LoRA/QLoRA 训练
│       ├── merge.py             # adapter 合并
│       └── export_gguf.py       # GGUF 导出与量化
├── scripts/                     # 便捷包装脚本
└── tests/                       # 450+ 项测试，全部离线
```

---

## 3. 两条任务的输出契约

### 3.1 事件评价（`event_eval`）

```json
{
  "direction": "-",
  "impact": 0.62,
  "activation": 0.44,
  "uncertainty": 0.71,
  "relation_signal": "slight_distance",
  "responsibility": "unclear",
  "confidence": 0.83,
  "evidence": "不知道，可能没时间"
}
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `direction` | `+` `-` `0` `+-` | 事件对角色整体是正向/负向/中性/混合 |
| `impact` | 0~1 | 影响**幅度**（与方向无关） |
| `activation` | 0~1 | 激活/紧绷/兴奋程度 |
| `uncertainty` | 0~1 | 含义与走向的不确定性 |
| `relation_signal` | `strong_approach` / `slight_approach` / `neutral` / `slight_distance` / `strong_distance` | 关系拉近还是拉远 |
| `responsibility` | `self` / `other` / `situation` / `shared` / `unclear` | 责任归属，不明确就用 `unclear` |
| `confidence` | 0~1 | 对本次评价的自信程度 |
| `evidence` | 2~60 字 | 支持的输入依据，**必须来自输入文本** |

`additionalProperties: false`：多一个字段就是不合法。
**没有**任何情绪名称或情绪强度字段 —— 这是 §8.1 的硬要求。

### 3.2 情绪解释（`emotion_explain`）

```json
{
  "experience": "有些失落，也有一点不确定。",
  "focus": "比较在意今晚的交流是否会就此中断。",
  "conflict": "想确认之后还会不会继续交流，但又不想显得太依赖。",
  "impulse": "想确认用户之后是否还会回来。",
  "inhibition": "不希望给用户增加压力。",
  "expression": "表达上会稍微显得舍不得，但整体仍然克制。"
}
```

每个字段 4~80 字、单句、**不含数字**、不含引号/台词/动作描写。
可选字段 `restraint_evidence`：当节制作用写在 `conflict` 而不是 `inhibition` 时，
用它显式给出节制依据（架构文档 §11.2 的示例就是这种写法）。

---

## 4. 不变量：真正的质量闸门

JSON Schema 只能表达"字段在不在、类型对不对、范围越没越界"。
字段之间的关系与 §11.3 的硬约束由 `validators/invariants.py` 检查。

### 4.1 事件评价

| 代码 | 严重度 | 触发条件 |
| --- | --- | --- |
| `EV01_DIRECTION_IMPACT` | warning | `direction=0` 但 `impact>0.25`；或有方向但 `impact<=0.05` |
| `EV02_DIRECTION_SIGNAL` | **error** | `direction` 与 `relation_signal` 矛盾（如 `+` 配 `strong_distance`） |
| `EV03_OVERCONFIDENT_HIGH_IMPACT` | warning | 高影响事件同时给出满分 confidence 与近零 uncertainty |
| `EV04_MIXED_LOW_UNCERTAINTY` | warning | `direction=+-` 却给出近零 uncertainty |
| `EV05_EMOTION_VALUE_LEAK` | **error** | 出现情绪值字段（§8.1 越界） |
| `EV06_EVIDENCE_UNGROUNDED` | warning | `evidence` 在输入里找不到依据 |
| `EV07_NEUTRAL_HIGH_AROUSAL` | info | 中性事件却给出极高 activation |

### 4.2 情绪解释（§11.3）

| 代码 | 严重度 | 对应硬约束 |
| --- | --- | --- |
| `EX01_AMPLIFY_LIGHT_EMOTION` | **error** | 不得放大轻微情绪（输入 intensity≤0.45 却使用强化表达） |
| `EX01B_DAMPEN_STRONG_EMOTION` | **error** | 不得压低底层情绪（输入 intensity≥0.8 却写得平淡） |
| `EX02_DIRECTION_FLIP` | **error** | 方向不得翻转（负向输入写成正向） |
| `EX03_UNSUPPORTED_NUMERIC` | **error** | 不得自行量化情绪（输出出现输入外的数字） |
| `EX04_IMPULSE_APPROACH_MISMATCH` | **error** | impulse 必须与 approach_drive 一致 |
| `EX05_INHIBITION_RESTRAINT_MISMATCH` | **error** | inhibition 必须与 restraint 一致 |
| `EX06_DIALOGUE_GENERATED` | **error** | 不得生成台词（引号/括号动作描写） |
| `EX07_DECIDES_ACTION` | **error** | 不得决定最终行为 |
| `EX08_FABRICATED_CONFLICT` | warning | 输入无冲突却虚构冲突 |
| `EX09_EVENT_IGNORED` | info | 输出脱离输入事件、泛泛而谈 |

### 4.3 中文否定与极性判断

极性判断（`tone_score`）不是简单的词频相减 —— 中文里否定会反转极性：

* `"不难过"` → 否定负向词 → **+1**（确实偏正向）
* `"不安心"` → 否定正向词 → **-1**（确实偏负向）
* `"完全没有冲突"` → 否定负向词 → **+1**

否定判定限制在**同一子句内**（遇 `，。！？` 等边界截断），并且
**刻意不收录单字 `非`** —— 它会匹配到 `非常/非但` 这类**加强**词，
把"非常开心"判成"被否定的正向"，导致极性完全反向。
同理 `无` 只收 `无比` 之外的多字形式。

---

## 5. 数据生成

### 5.1 断点续跑

生成器是**幂等且可续跑**的：

* 启动时读回已有 JSONL，重建去重器；
* 已完成的 `scenario_id` 直接跳过；
* 每批结束写入断点（计数、已用预算、种子游标、已完成 id）；
* 被 Ctrl-C 或预算中止后，重新运行**同一条命令**即从断点继续。

断点里只有计数与游标，**没有 API key**。

### 5.2 预算守卫

`budget` 节是四重上限，任一命中即优雅停止并保留断点：

```yaml
budget:
  max_requests: 400          # 请求数
  max_prompt_tokens: 900000  # prompt token
  max_completion_tokens: 600000
  max_total_tokens: 1400000  # 总 token
  max_usd: 3.0               # 金额（按可配置单价估算）
```

先用小额预算（如 `--max-usd 0.2 --target 20`）跑通再放大。

### 5.3 重试与退避

指数退避 + 抖动；`408/409/425/429/5xx` 可重试，其余状态码（如 401）
立即失败并给出可操作信息。网络异常同样重试。

### 5.4 JSON 抽取的鲁棒性

小模型/本地推理的脏输出都被处理：

* 前后有解释性文字；
* ` ```json ... ``` ` 包裹；
* 尾随逗号 `{"a":1,}`；
* 中文全角引号/括号 `｛“a”：1｝`；
* 字符串里含 `{` `}`、转义引号；
* 一次吐多个 JSON（按 schema 要求挑选合格的）。

抽取失败不是终点：会在同一批内**拒绝采样重试**
（`max_sample_attempts`，默认 3），仍失败才丢弃这条种子并记账。

### 5.5 去重

两层：

1. **精确**：输入 payload 的 SHA-256（语义规范化后），O(1)；
2. **近似**：字符 3-gram Jaccard，倒排索引加速。

近似阈值 `near_duplicate_threshold` **默认 0.90**，这是实测标定的结果
（60 条互相独立的样本、1770 个两两组合）：

| 阈值 | 把"本质不同"的样本误判为重复 |
| --- | --- |
| 0.80 | 1.58% |
| 0.85 | 1.36% |
| 0.88 | 1.02% |
| **0.90** | **0.34%** |
| 0.92 | 0.00% |

而真正近似重复的样本（只差一个标点）相似度约 **0.93**。
因此 0.90 是"几乎不误杀、仍能抓到近乎相同的样本"的拐点。
**调到 0.85 以下会开始丢弃真数据 —— 比漏去重更糟。**

另外一个容易踩的坑：近似的文本指纹**只取字符串值**，不做 canonical JSON。
若把字段名也计入，`current_event`/`background_mood` 这些固定键会让
"结构相同但内容无关"的两条输入拿到 0.8+ 相似度（结构噪声淹没语义信号）。

### 5.6 种子场景覆盖

`data/seeds.py` 按 **22 种事件类型 × 6 个心境区域 × 5 种价值观画像**
笛卡尔式采样，并保证每种事件类型轮转出现（避免长尾场景被随机采样饿死）。

同一事件在不同价值观下应有不同评价（架构文档 §10）：
`relatedness` 高 + 用户抽身 → 负向更明显；`autonomy/boundary` 高 → 负向更轻。
提示词里显式给出价值观画像，让教师模型产出这个差异。

---

## 6. 切分与防泄漏

```powershell
qboss split --input data/raw/*.jsonl --output-dir data/splits --stratify-by task,direction
```

* **确定性**：先按 id 排序再按 `seed:stratum` 洗牌，同输入同种子 → 同结果；
* **分层**：默认按 `task`，可叠加 `direction` / `event_kind`，
  避免小验证集里某一类完全缺失；
* **比例精确**：最大余数法分配，保证总和等于样本数（不丢样本）；
* **防泄漏**：切分后跨 split 做 Jaccard 检测，
  命中的 val/test 样本被**移回 train**（安全方向），并在 manifest 里报告。

输出 `train.jsonl` / `val.jsonl` / `test.jsonl` + `split_manifest.json`
（含计数、去重数、泄漏明细、各 split 的 id 指纹）。

---

## 7. 聊天 SFT 与 completion-only 掩码

```powershell
qboss sft --split-dir data/splits --output-dir data/sft `
          --model-path Qwen/Qwen3.5-2B --preview-masking
```

每条样本是三段消息：`system`（任务契约提示词）+ `user`（输入 JSON）+ `assistant`（输出 JSON）。

**为什么是 completion-only**：本任务的目标是"给定输入输出严格结构化 JSON"。
让模型去拟合输入段的 JSON 只是浪费容量，还会鼓励它复制输入。
因此 `input_ids` 中 system 与 user 段的 `labels` 全部置为 `-100`。

三个实现要点：

1. **掩码靠字符偏移定位**，不靠字符串分割 —— 用 `offset_mapping` 把
   token 映射回字符区间，再与 assistant 段区间求重叠。
2. **跨边界 token 算监督**（有重叠即可），否则 completion 的第一个
   token（同时含模板换行与内容首字符）会被整段掩掉，模型学不到开头。
3. **零宽 offset 的 token 不监督**：某些 tokenizer 给控制/特殊 token
   的 offset 是 `(n, n)`，它们来自提示词侧的模板标记，纳入 loss 会把模板串学进去。

**思维链掩码**：Qwen3 系模板可能在 assistant 段带 ` thinking...<｜end▁of▁thinking｜>`。
本任务是确定性结构化输出，思维链没有监督价值，因此
`enable_thinking=False`，且即使模板忽略了该参数，
`<｜end▁of▁thinking｜>` 之前的内容也会被掩掉。

**训练与推理必须用同一套序列化**：`pretty_json` 在训练（`sft`）与
推理（`inference.build_inference_messages`）两侧都默认开启，
且有测试断言两侧提示词逐字符一致。若不一致，会出现
"训练能过、推理不过"的诡异现象。

`--preview-masking` 会打印 `█`/`·` 对照，肉眼确认掩码只覆盖 assistant 段。

---

## 8. 训练

### 8.1 为什么是 BF16 LoRA（而不是全参 / QLoRA）

* 2B 参数 bf16 约 4GB 权重，加 LoRA 优化器状态与激活，
  单卡 8GB（RTX 4060 Laptop）可训；
* 任务是"严格结构化输出"，LoRA 容量足够，全参微调没有收益，
  反而更容易破坏基座的中文能力；
* **QLoRA 是 fallback**，只在显存不足时用。

### 8.2 冻结策略（硬性）

| 模块 | 默认 | 理由 |
| --- | --- | --- |
| **视觉塔**（visual / vision_tower / merger / projector / patch_embed / resampler） | **冻结 + 不加载** | 纯文本任务完全用不到；加载只会浪费显存，训练只会带上无用梯度 |
| **embedding**（`embed_tokens` / `wte` / `embeddings`） | **冻结** | Qwen 词表约 15 万 token，embedding 占参数比例很高；输出空间被 schema 严格约束，没有学新词表的必要 |
| **lm_head** | **冻结** | 同理；且 `tie_word_embeddings` 下与 embed_tokens 是同一份权重 |
| **norm** 层 | **冻结** | LoRA 的 scaling 已提供幅度调节；解冻 norm 会让训练更不稳定 |
| attention `q/k/v/o_proj` | 可训练（LoRA） | 默认目标模块 |
| MLP `gate/up/down_proj` | 默认不加 | 2B 上收益有限但显存涨得明显，需要时 `include_mlp: true` |

两道校验：

1. LoRA 注入**之前**按名字分类并冻结（`apply_freeze_policy`，输出分类统计）；
2. LoRA 注入**之后**再查一遍（`assert_freeze_invariants`），
   任何"该冻结却可训练"的参数都会让训练**直接报错退出** ——
   防止"配置写了但没生效"这种最危险的静默失败
   （LoRA 注入顺序、tie_weights、自定义实现都可能让它悄悄失效）。

```powershell
# 只看配置与冻结策略，不加载模型
qboss train --config configs/lora_bf16.yaml --plan-only
```

### 8.3 冒烟测试

```powershell
qboss train --config configs/lora_bf16.yaml --smoke
```

4 步、batch=1、`max_seq_length=512`，几分钟内验证：
数据能读 → 掩码正确 → adapter 注入 → 冻结生效 → loss 能降 → 能保存。
**在花几小时训练之前先跑这个。**

### 8.4 关键超参（为什么这样设）

| 参数 | 值 | 理由 |
| --- | --- | --- |
| `max_seq_length` | 1024 | 事件评价几百 token，情绪解释 512~1024（§74.2）；1024 足够覆盖两者，**不必开 8K/16K** |
| `learning_rate` | 8e-5 | LoRA 常规区间；再高容易在结构化输出上过拟合 |
| `num_train_epochs` | 2 | 数据量小时 2 epoch 足够；先看 eval loss 再决定加不加 |
| `gradient_accumulation_steps` | 8 | 等效 batch 16；小 batch + LoRA 梯度噪声大 |
| `gradient_checkpointing` | true | 换显存 |
| `packing` | **false** | packing 会跨样本拼接并**破坏 completion-only 掩码边界** |
| `bf16` | true | 与 LoRA scaling 配合稳定；2B 上比 fp16 更安全 |
| `save_total_limit` | 3 | 避免 checkpoint 撑满磁盘 |

### 8.5 断点续训

训练输出目录里存在 `checkpoint-*/trainer_state.json` 时会自动从最新断点继续。
也可显式指定：`qboss train --resume outputs/lora-event-eval/checkpoint-400`。

输出 `training_report.json`（含完整配置、冻结报告、可训练参数统计、
数据统计、metrics）与 `schema_fingerprint.json`（对齐数据集与 adapter 版本）。

### 8.6 QLoRA fallback

```powershell
qboss train --config configs/qlora_fallback.yaml
# 或对任意配置临时开启
qboss train --config configs/lora_bf16.yaml --qlora
```

开启后会强制：4bit nf4 量化、`paged_adamw_8bit`、且**强制冻结 embedding/lm_head**
（解冻它们会让量化省下来的显存白费）。

---

## 9. 评测

```powershell
qboss eval --input data/splits/test.jsonl --backend transformers `
           --model-path Qwen/Qwen3.5-2B --adapter-path outputs/lora-event-eval `
           --output-dir reports/eval --markdown reports/eval.md --gate
```

### 9.1 指标

| 指标 | 含义 |
| --- | --- |
| `extraction_rate` | 原始回复能抽出 JSON 的比例（纯格式遵循能力） |
| `schema_pass_rate` | 字段/类型/取值域全部合法（含 `additionalProperties: false`） |
| `invariant_pass_rate` | §8.1 / §11.3 硬约束通过率 |
| `text_constraint_rate` | 文本约束（长度、无换行、无数字、无台词、无括号动作） |
| 类别字段 `accuracy` | 精确匹配率 |
| 数值字段 `mae` / `within_tolerance_rate` | 平均绝对误差与误差命中率 |
| 文本字段 `accuracy` | 非空且长度达标比例（文本无唯一正解，不做字符串比对） |

字段口径在报告里显式区分（`数值` / `类别` / `文本`），
避免把浮点字段的"完全相等率"误读为"准确率"。

**缺失不计为 0 误差**：数值字段缺失或类型错误时按最大误差 1.0 计入 MAE，
防止"缺失即忽略"把分数美化。

### 9.2 上界基线与门禁

```powershell
# gold 回放：等价于"模型完全正确"的上界（也用于验证评测管线本身）
qboss eval --input data/splits/test.jsonl --backend echo
```

`--gate` 启用上线门禁（默认：抽取率 ≥99%、schema ≥98%、不变量 ≥95%），
不达标则非零退出 —— 可直接用于 CI 或"量化后必须复测"的流程。

### 9.3 量化前后对比

```powershell
qboss eval --backend transformers --model-path outputs/merged --output-dir reports/bf16
qboss eval --backend llama_cpp --gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf --output-dir reports/q4
```

两份 `eval_report.json` 可用 `compare_reports()` 生成对比表。

---

## 10. 合并与 GGUF CPU 导出

### 10.1 为什么必须先合并

llama.cpp 的 `convert_hf_to_gguf.py` **只认标准 HF 权重**，
不认识 `adapter_config.json` / `adapter_model.safetensors`。所以：

```text
LoRA adapter ──merge──> 合并后 HF 权重 ──convert──> f16 GGUF ──quantize──> Q4_K_M
```

```powershell
qboss merge --adapter-path outputs/lora-event-eval --output-dir outputs/merged
```

合并优先用 `peft.merge_and_unload()`；失败时回退到手写合并
（`W' = W + (alpha/r)·B@A`，多候选前缀匹配以兼容不同 peft 版本的命名）。
结束后自动检查产物：缺 `config.json`/权重/tokenizer，
或目录里**还留着 `adapter_config.json`**（说明其实没合并成功）都会报错返回。

### 10.2 导出与量化矩阵

```powershell
qboss gguf --matrix                     # 先看量化矩阵与内存估算
qboss gguf --model-dir outputs/merged --output-dir outputs/gguf `
           --llama-cpp-dir E:\llama.cpp --quants Q8_0,Q4_K_M,Q3_K_M
qboss gguf --model-dir outputs/merged --output-dir outputs/gguf --dry-run   # 只打印命令
```

| 类型 | 说明 | 约为 f16 体积 | 适用场景 |
| --- | --- | --- | --- |
| Q8_0 | 几乎无损 | 53% | 内存充裕，质量上界参考 |
| Q6_K | 质量接近无损 | 41% | 内存尚可 |
| Q5_K_M | 质量体积平衡好 | 36% | 一般 VPS |
| **Q4_K_M** | **§74.1「优先 Q4」，本工程默认** | **30%** | **弱 VPS 默认选择** |
| Q4_K_S | 比 Q4_K_M 更小 | 28% | 内存紧张 |
| Q3_K_M | §74.1「内存极限」 | 24% | 内存极限，需重点复测 schema |
| Q2_K | §74.1「最后生存模式」 | 18% | 只保可用性 |

内存估算（2B、2K 上下文、含 KV cache 与运行时开销）：

```powershell
python -c "from qboss_training.training.export_gguf import estimate_ram_mb; print(estimate_ram_mb(2.0,'Q4_K_M'))"
```

### 10.3 llama.cpp 准备

本工程**不打包** llama.cpp，只负责调用与校验产物。需要：

```powershell
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
```

导出脚本会兼容两种布局：仓库根目录的 `convert_hf_to_gguf.py`
（或 `scripts/` 下），以及 `build/bin/`、`build/bin/Release/` 下的
`llama-quantize(.exe)`。转换依赖见 `requirements-infer.txt`
（`gguf` / `sentencepiece` / `protobuf` 版本要与 llama.cpp 匹配）。

### 10.4 弱 VPS 部署要点（架构文档 §74）

* **可用性 > 推理速度**（§74 核心目标）；
* 2B worker **单并发**（§61），限制线程，`nice`/cgroup 降优先级，
  不要为了快几秒把整台服务器打死（§74.3）；
* 模型**常驻内存**，不要每轮重新加载 1GB+ 权重（§74.4）；
* **不要靠 swap 跑模型**（§74.5）—— 权重频繁换页会灾难性降速；
* 上下文保持短：事件评价几百 token，情绪解释 512~1024（§74.2），
  完全没必要开 8K/16K；
* 情绪解释有缓存：心理状态变化很小时复用旧解释（§12），
  这比把模型调快更有效。

---

## 11. CPU 基准

```powershell
qboss bench --input data/splits/test.jsonl --backend llama_cpp `
            --gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf `
            --threads 2,4,6 --repeat 5 --samples-per-task 5 `
            --output-dir reports/bench --markdown reports/bench.md
```

测四件事：

1. **延迟**：p50/p90/p99，区分冷启动与稳态（暖机轮不计入统计）；
2. **吞吐**：completion tokens/s；
3. **质量**：每次调用的 schema 合法率与不变量通过率 ——
   **低量化最先坏掉的通常不是速度而是结构化输出**，这是比延迟更致命的退化；
4. **线程扫描**：给出"建议线程数"，避免把 VPS 打死。

未指定 `--threads` 时用后端默认；多线程值会逐一轮询。
不达标（默认 p90 ≤8s、schema ≥98%）时非零退出。

---

## 12. 测试

```powershell
pytest                       # 全部（450+ 项）
pytest -m "not slow"         # 跳过慢测试
pytest tests/test_validators.py -v
pytest --cov=qboss_training --cov-report=term-missing
```

**全部测试在无网络、无大模型、无 GPU 的环境下通过。** 实现方式：

* **假服务端**：`httpx.MockTransport` 注入到真实的 `HttpxTransport`，
  因此**真实 HTTP 代码路径**（含 JSON 解析、状态码、重试）被完整覆盖，
  而不是只测一个假的协议实现；
* **合成夹具**：`fixtures.py` 生成 schema 合法的样本，
  并可通过 `--include-invalid` 混入**故意违规**的样本来验证校验器真的能抓到；
* **假 tokenizer**：字符级 tokenizer 模拟 Qwen 模板，
  使 completion-only 掩码可以**逐 token 精确断言**，不需要下载模型；
* **echo 后端**：gold 回放让评测/基准管线在无模型时可端到端自检。

重点测试覆盖：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_contracts.py` | schema 严格性、字段分类完整性、提示词含硬约束 |
| `test_utils.py` | JSON 抽取 12 种脏输出、去重标定、规范化 |
| `test_secrets.py` | 脱敏、**仓库内不存在真实密钥**、`.gitignore` 覆盖 |
| `test_validators.py` | 每条不变量的正例与反例（含放大/压低/翻转/数字/台词/行为/虚构冲突） |
| `test_splitter.py` | 确定性、比例精确、分层、防泄漏、任务覆盖 |
| `test_sft.py` | system/user 段**一个 token 都不能**参与 loss、思维链掩码、零宽 offset |
| `test_eval.py` | MAE/命中率/缺失按最大误差、门禁、报告 |
| `test_generator.py` | 断点续跑幂等、预算中止保留部分结果、重试、去重、**key 不落盘** |
| `test_config.py` | 凭据字段被拒、类型强制、YAML 配置可加载且冻结策略正确 |
| `test_training.py` | 冻结分类与生效、QLoRA 覆盖、GGUF 命令构造、基准统计 |

---

## 13. 安全约定

1. **key 只来自环境变量** `DEEPSEEK_API_KEY`；
2. CLI **不接受** key 参数；
3. 配置里出现 `api_key`/`token`/`secret`/`password` 等字段 → **加载即报错**；
4. **显式注入的 key 也会被登记脱敏**：即使服务端把 key 回显在错误信息里，
   异常文本与日志里也只会看到 `***REDACTED***`；
5. 断点、报告、摘要文件都不含凭据（有测试断言）；
6. `.gitignore` 覆盖 `.env*`、`*.key`、`*.gguf`、`outputs/`、`data/raw/`。

---

## 14. 常见问题

**Q: 不设 API key 能做什么？**
`validate` / `split` / `sft --no-tokenize` / `eval --backend echo` /
`bench --backend echo` / `train --plan-only` / `gguf --dry-run` 以及全部测试，
完全离线可用。只有 `gen` 需要 key。

**Q: 生成的输出老是被丢弃？**
看 `generation_summary.json` 的 `stats`：`rejected_schema` 高说明教师没遵循
schema（可开 `self_verify: true`）；`rejected_duplicate_*` 高说明种子场景多样性
不足或阈值太松。

**Q: 训练 loss 降了但评测不行？**
先查 `--preview-masking`：掩码把输入段也训进去了会让模型学会复述输入。
再查 `schema_pass_rate` 与 `invariant_pass_rate` 哪个低 ——
前者是格式能力，后者是语义一致性，两者需要不同的数据补充。

**Q: Q4 之后 schema 合法率掉了？**
正常现象，且这正是要做量化后复测的原因。先试 `Q5_K_M`，
或在 Q4 数据上再做一轮轻量 LoRA（但要注意与部署量化等级一致的验证）。

**Q: 显存不够？**
用 `configs/qlora_fallback.yaml`（4bit + paged optimizer），
或降低 `max_seq_length` / `per_device_train_batch_size`。
不要试图解冻 embedding/lm_head 来"省事"。

**Q: 想换成别的基座模型？**
改 `configs/lora_bf16.yaml` 的 `model.name_or_path`，
并确认 `lora.target_modules` 与目标模型的模块名一致
（不同实现可能用 `c_attn`/`query` 等命名）。同步更新
`sft` 的 `--model-path`（tokenizer 来源）。
