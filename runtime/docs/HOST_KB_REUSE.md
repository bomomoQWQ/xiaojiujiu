# 复用 AstrBot 自带知识库：实测契约

这份文档记录**在真实实例上验证过**的事实，供"记忆检索是否复用宿主知识库"这个决定使用。
验证环境：AstrBot **4.28.1**（容器 `astrbot-test`），embedding provider = SiliconFlow
`BAAI/bge-m3`（1024 维），插件进程内调用 `Context.kb_manager`。

**结论先行**：这条路可行，而且语义命中确实比我们的词法兜底强；但有三处必须知道的行为，
其中第三处会直接决定接口设计。

---

## 1. 插件**不能**在 `initialize()` 里使用知识库

实测时间戳（同一次启动）：

| 时刻 | 事件 |
|---|---|
| `18:47:27.564` | 插件 `initialize()` 运行 → `context.kb_manager` 已在，但 `get_kb_by_name()` 抛异常 |
| `18:47:28.048` | `provider.manager` 加载 `openai_embedding(siliconflow/BAAI/bge-m3)` |
| `18:47:28.082` | `knowledge_base.kb_mgr: KnowledgeBase database initialized: …/kb.db` |
| `18:47:28.797` | `core_lifecycle: AstrBot started.` |

正确做法是用 `@filter.on_astrbot_loaded()`（v3.4.34+ 提供），实测在
`core_lifecycle: hook(on_astrbot_loaded) -> <plugin> - <handler>` 之后才回调，
此时 provider 与 KB 都已就绪。**懒加载（首次用到再建）同样是安全的。**

## 2. 知识库必须绑定 embedding provider，否则连稀疏检索都用不了

`kb_helper.initialize()`：没有 `embedding_provider_id` 直接 `init_error`，`retrieve()` 返回前
就把该库标为不可用（`kb_mgr.retrieve` 会把不可用的库放进 `unavailable_kbs` 并抛错）。

provider 的配置形状（4.28.1 顶层 `provider` 数组，与 `provider_sources` 合并机制并存）：

```json
{
  "id": "siliconflow/BAAI/bge-m3",
  "type": "openai_embedding",
  "provider": "openai",
  "provider_type": "embedding",
  "enable": true,
  "embedding_api_key": "<环境变量或配置内；不要进仓库>",
  "embedding_api_base": "https://api.siliconflow.cn/v1",
  "embedding_model": "BAAI/bge-m3",
  "embedding_dimensions": 1024,
  "embedding_dimensions_mode": "never"
}
```

`embedding_dimensions_mode` 只接受 `auto | always | never`：`always` 会带上 `dimensions`
参数，`auto` 只对 `text-embedding-3-*`（OpenAI）与 `qwen*`（api.siliconflow.cn）生效。
**bge-m3 不接受该参数，因此必须用 `never`**，维度由 `embedding_dimensions` 告诉 KB。

## 3. `score` 是"结果集内归一化的融合分"，不是相似度 —— 不能拿来卡阈值

`rank_fusion.py` 的流程是：稠密相似度与每路 BM25 各自 **min-max 归一化** →
加权融合（`fusion = w_dense·norm_dense + w_sparse·norm_sparse`）→ 再按 RRF 排序 →
返回的 `score` 是那个融合分。后果是**每个查询的第一名都会是 1.0**：

| 查询 | 第 1 名 | 第 2 名 | 第 3 名 |
|---|---|---|---|
| 他平时喝什么咖啡？ | **1.0000** 手冲咖啡那条 ✓ | 0.1508 面试 | 0.0000 边界 |
| 面试的事有结果了吗 | **0.9000** 面试那条 ✓ | 0.5858 「别再提面试」 | 0.0000 咖啡 |
| 他喜欢什么颜色的车 | **1.0000** 咖啡那条 ❌ | 0.1586 面试 | 0.0000 边界 |

第三行是关键：**完全无关的查询，第一名照样 1.0**。所以：

* 想做"相关就注入、不相关就不注入"，**不能**用 `score > 阈值`；
* 可选的三条路：(a) 给 KB 配 `rerank_provider_id`（此时 `result.score` 会被换成
  rerank 的 `relevance_score`，那是真正的相关性分，可以卡阈值）；(b) 只取 `top_m_final`
  的前 1–2 条，并接受"总会召回点东西"；(c) 在调用侧再叠一层我们自己的词法/规则门。
* 第一行同时说明**收益是真的**：「他平时喝什么咖啡？」与「用户喜欢手冲咖啡…」词面几乎
  不重合，仍然被排到第一 —— 这正是词法兜底会漏掉的情况。

## 4. 可用的调用形状（进程内，插件侧）

```python
helper = await context.kb_manager.get_kb_by_name("companion_memory")     # -> KBHelper | None
helper = await context.kb_manager.create_kb(
    "companion_memory", embedding_provider_id="siliconflow/BAAI/bge-m3",
    chunk_size=256, chunk_overlap=32, top_m_final=5,
)
await helper.upload_document(                # 直接灌文本块，不经过文件解析器
    file_name="memories.txt", file_content=None, file_type="txt",
    pre_chunked_text=["记忆 1", "记忆 2"],
)
result = await context.kb_manager.retrieve(query="…", kb_names=["companion_memory"], top_m_final=3)
# -> {"results": [{"chunk_id","doc_id","kb_id","kb_name","doc_name","chunk_index",
#                  "content","score","char_count"}], "context_text": …}
```

## 5. 也有 HTTP API（Runtime 若要走网络，用它）

`openspec/openapi-v1.yaml`（即 `docs.astrbot.app/scalar.html` 渲染的那份）里有：

* `POST /api/v1/knowledge-bases/{kb_id}/retrieve` —— 检索，需要 **`kb` scope** 的 API key；
* `POST/GET/DELETE /api/v1/knowledge-bases…`、`…/documents`、`…/chunks`、`…/stats` —— 建库、灌文档、看块。

鉴权是 API key（header 或 `?api_key=`，库里存 pbkdf2 hash）+ scope（`require_scope(request, "kb")`）。
**注意**：Runtime 走 HTTP 就多了一个对外依赖（地址 + key），而进程内路径不需要 key。

## 6. 本次为验证而做的改动（测试实例 `192.168.1.15`，现网那台未动）

* 在 `astrbot-test` 的 `cmd_config.json` 里新增 embedding provider（备份
  `cmd_config.json.bak-embedding`）；
* 创建知识库 `companion_memory` 并灌入 3 条探针"记忆"；
* 一次性探针插件 `data/plugins/zz_kb_probe/`（`main.py` + `metadata.yaml`）：
  它只做上面这些并把结果写到 `/AstrBot/data/kb_probe_report.json`，**确认集成方案后应删除**。

## 7. 实测中的其他注意点

* 插件在启动后写的日志记录不一定进 AstrBot 控制台（`initialize()` 阶段的能进，
  之后的会被重配），所以**探针/诊断要落文件**，别只依赖 `docker logs`。
* 上传时 AstrBot 会加载 jieba 词典做中文分词（首次约 0.8s），这是稀疏检索那一路的成本。
