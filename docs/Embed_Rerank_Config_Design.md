# Embed / Rerank 配置化设计（独立配置区 · 方案 A）

> 状态：**草案，待 CTO 确认**（渐进明细 + 待确认疑问）。CTO 已定方向 A：embed/rerank
> 各做独立 WebUI 可配区（预置云端 + secret_store 加密 key），不强塞进 LLM providers 体系。
> 目标：从"默认指向自建服务、只能改 .env"→"配置式、WebUI 可视化、开箱即用走云端成熟服务"。

---

## 一、现状诊断（已核对代码）

**不是写死，是"可配但三缺陷"**：
- `config/settings.py KnowledgeConfig` 已有字段：`embed_base_url`(默认 ai.bit:8082/v1)、
  `embed_api_key`、`embedding_model`(BAAI/bge-m3)、`rerank_url`(默认 ai.bit:8081/rerank)、
  `reranker_model`(BAAI/bge-reranker-v2-m3)、`embed_batch_size`、`rerank_score_threshold` 等。
- 缺陷①：默认值指向自建服务（ai.bit:8082/8081），别的用户开箱不可用。
- 缺陷②：无 WebUI 配置入口，只能改 .env / 环境变量。
- 缺陷③：key 走明文 .env，未走 secret_store 加密（违背"敏感配置加密"原则）。

**调用链现状**：
- embed：`knowledge/embed_client.py`（统一客户端，OpenAI 兼容 /embeddings）→
  `preprocess._embed_texts` 4 级降级（专用服务 → sentence-transformers → 本地网关 → OpenAI 兼容 → 随机向量兜底）。
- rerank：`knowledge/reranker.py rerank_documents` **已有 2 层适配**：
  ① 专用服务 `{query,texts}→[{index,score}]`（自建格式）
  ② **回退 SiliconFlow/OpenAI 兼容 `/rerank` `{model,query,documents,top_n}`（云端标准，已写好！）**
  → embeddings 相似度兜底 → 本地排序兜底。
  **关键发现：rerank 云端适配已实现一半，只是绑死读 cfg.model.siliconflow_api_key、无 WebUI 入口。**

## 二、目标（方案 A）

设置面板「知识库 / 检索」区新增两块独立可配：
1. **Embedding 接入点**：预置下拉（SiliconFlow bge-m3 / OpenAI text-embedding-3 / 智谱 / 自定义）+ base_url + model + API Key（secret_store 加密）。
2. **Rerank 接入点**：预置下拉（SiliconFlow rerank / Cohere rerank / 自建 {query,texts} 格式 / 自定义）+ base_url + model + API Key + **格式适配类型**。

保存即落库 + reload，与 LLM provider 配置体验一致。

## 三、设计要点（渐进明细）

### 3.1 配置存储
- 非敏感（base_url/model/format/batch/threshold）→ db 配置表（沿用现有 config 落库机制）。
- 敏感（embed_api_key/rerank_api_key）→ **secret_store 加密**（key 名 `kb_embed_api_key`/`kb_rerank_api_key`）。
- KnowledgeConfig 读取时：key 优先从 secret_store 取，回退 .env（向后兼容）。
- **默认值保持当前**：embed_base_url=ai.bit:8082/v1、rerank_url=ai.bit:8081/rerank（本部署已搭好的服务）。
  本改造只加 WebUI 可配能力，不改默认；其他使用者按需在 WebUI 改成自建/云端。

### 3.2 预置 catalog（开箱即用）
新增 `GET /api/kb/embed-rerank/catalog`（或复用 model-providers catalog 思路）：

| 类型 | 预置 | base_url | 格式 |
|---|---|---|---|
| embed | SiliconFlow | https://api.siliconflow.cn/v1 | OpenAI /embeddings |
| embed | OpenAI | https://api.openai.com/v1 | OpenAI /embeddings |
| embed | 智谱 | https://open.bigmodel.cn/api/paas/v4 | OpenAI /embeddings |
| embed | 自定义 | 手填 | OpenAI /embeddings |
| rerank | SiliconFlow | https://api.siliconflow.cn/v1 | openai_rerank `{model,query,documents,top_n}` |
| rerank | Cohere | https://api.cohere.ai/v1 | cohere_rerank |
| rerank | 自建 | 手填 | simple `{query,texts}→[{index,score}]` |

### 3.3 rerank 格式适配层
reranker.py 已有 2 种格式，抽象成 `rerank_format` 字段（simple / openai_rerank / cohere_rerank），按配置选适配器。embed 统一 OpenAI /embeddings（无需适配）。

### 3.4 后端改造
- `KnowledgeConfig` 加 `embed_format`(默认 openai)、`rerank_format`(默认 simple/openai_rerank)、`rerank_api_key`。
- `embed_client._endpoint()` / `reranker.rerank_documents` 改读新配置（key 走 secret_store）。
- 新增 `GET/PUT /api/kb/embed-rerank` 配置端点 + catalog 端点。

### 3.5 前端
设置面板加「Embedding 接入点」「Rerank 接入点」两区（复用 provider 行的 UI 风格：预置下拉 + 字段 + key 输入 + 保存）。

## 四、关键决策（CTO 已拍板）

1. **向量维度变更** → ✅ 切 embedding 模型导致维度变化时，**明确提示"需重新预处理所有项目"，用户手动决定**（不自动跑重活）。检测 collection 维度与新模型维度不一致 → UI 警示 + 引导去预处理。
2. **默认值** → ✅ **保持当前配置不变**（embed=ai.bit:8082/v1、rerank=ai.bit:8081/rerank）。
   澄清：本部署的"开箱即用"= 我们自己搭好了 embed/rerank 服务，默认指向它即可。
   本次改造**不改默认值**，只是把这套配置**做成 WebUI 可视化可配 + key 加密**——
   别的使用者可通过 WebUI 改成自己的自建服务或云端成熟服务（SiliconFlow/OpenAI 等）。
3. **禁用 rerank 开关** → ✅ **不加**（rerank 有本地排序兜底，无需显式关闭）。
4. **复用已有 provider key** → ✅ 支持，但**必须检查严谨**：embed/rerank 选某预置（如 SiliconFlow）
   且 LLM providers 已配同名 provider 有 key 时，提供"复用该 provider key"选项。
   **检查要点**（防 key 错配）：
   - 只在 base_url 同源（同一 provider）时才允许复用，避免把 A 家的 key 发给 B 家端点；
   - 复用是"读取时回退"语义：embed/rerank 自己的 key 为空 + 标记了 reuse_provider=<id> →
     从该 provider 取 key；自己有 key 则优先用自己的；
   - UI 明确标注"复用自 LLM 接入点 <id> 的 Key"，不静默；
   - 该 provider 不存在/无 key 时降级提示，不静默失败。

## 五、施工顺序（确认后）
- 批1：后端配置模型（KnowledgeConfig 加字段 + secret_store key + catalog）+ 单测。
- 批2：embed/rerank 调用点改读新配置 + rerank 格式适配层 + 单测。
- 批3：配置端点 GET/PUT /api/kb/embed-rerank + catalog 端点。
- 批4：WebUI 两个配置区（预置下拉 + 字段 + 保存即 reload）。
- 批5：维度变更提示 + 重新预处理引导（按疑问1的决定）。
- 批6：E2E 验证（配 SiliconFlow embed → 预处理 → 检索命中；配云端 rerank → 检索重排）。
