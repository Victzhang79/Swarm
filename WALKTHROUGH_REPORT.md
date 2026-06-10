# Swarm 三大业务域走读报告

> 只读走读，不改代码。报告覆盖 `knowledge/`、`memory/`、`project/` 三模块，具体到 文件:行号。

---

## 一、知识库 Layer A-D 各层写入/检索路径

### Layer A — 结构索引（符号/文件/依赖图）

**存储**: PostgreSQL `kb_file_index` + `kb_symbol_index` + `kb_dependency_graph`

**写入路径**:

| 场景 | 入口 | 关键行号 |
|------|------|----------|
| 预处理全量 | `preprocess._phase_index` → `_run_codegraph` → `_save_symbol_index` / `_save_dependency_graph` | `project/preprocess.py:348-447`, `:703-803` |
| 预处理文件扫描 | `preprocess._phase_scan` → `_save_file_index` | `project/preprocess.py:280-341`, `:703-734` |
| 增量更新(ADDED/MODIFIED) | `updater.KnowledgeUpdater._index_file` → `StructureIndexer.upsert_file` / `upsert_symbols_batch` | `knowledge/updater.py:358-376` |
| 增量更新(DELETED) | `updater._process_change` → `StructureIndexer.delete_file` | `knowledge/updater.py:335-338` |
| 增量更新(RENAMED) | `updater._process_change` → 先删旧再索引新 | `knowledge/updater.py:343-356` |
| AST 符号抽取(Python) | `_extract_symbols_python_ast` | `knowledge/updater.py:681-739` |
| 正则兜底符号抽取 | `_extract_symbols_simple` | `knowledge/updater.py:742-873` |

**检索路径**:

| 层次 | 入口 | 关键行号 |
|------|------|----------|
| Brain 统一检索 Layer A | `retriever.SwarmRetriever._retrieve_layer_a` | `knowledge/retriever.py:148-154` |
| 关键词提取 | `_extract_keywords` | `knowledge/retriever.py:143` |
| 依赖图扩展 | `_expand_dependency_files` | `knowledge/retriever.py:160-175` |
| 文件路径收集 | `_collect_file_paths` | `knowledge/retriever.py:157` |

**核心组件**: `StructureIndexer` (`knowledge/structure_index.py`)

---

### Layer B — 语义索引（向量嵌入）

**存储**: Qdrant 集合 `swarm_kb`（由 `DatabaseConfig.qdrant_collection` 指定）

**写入路径**:

| 场景 | 入口 | 关键行号 |
|------|------|----------|
| 预处理全量嵌入 | `preprocess._phase_embed` → `_embed_texts` → `_store_vectors_qdrant` | `project/preprocess.py:454-548`, `:880-1004` |
| Qdrant 不可用跳过 | `_check_qdrant` → 返回 skipped | `project/preprocess.py:472-484` |
| 增量语义索引(正常) | `updater._index_file` → `SemanticIndexer.index_source_file` | `knowledge/updater.py:378-389` |
| 增量语义索引(降级) | `updater._index_file` → catch → `_defer_embedding_retry` | `knowledge/updater.py:390-397` |
| 重试队列暂存 | `_defer_embedding_retry` → INSERT `kb_pending_embeddings` | `knowledge/updater.py:399-425` |
| 重试队列消费 | `retry_pending_embeddings` | `knowledge/updater.py:427-499` |

**嵌入函数链**: `_embed_texts` 三级回退:
1. `sentence_transformers.SentenceTransformer("BAAI/bge-m3")` — 本地模型 (`preprocess.py:888-893`)
2. HTTP API `http://localhost:3000/api/embeddings` (`preprocess.py:899-910`)
3. OpenAI-compatible API (`preprocess.py:912-921`)
4. 最终回退: 随机向量 (`preprocess.py:923-930`) ⚠️ 见问题列表

**检索路径**:

| 层次 | 入口 | 关键行号 |
|------|------|----------|
| Brain 语义检索 | `retriever._retrieve_layer_b` | `knowledge/retriever.py:180-189` |
| Reranker | `reranker.rerank_documents` / `_rerank_via_embeddings_fallback` | `knowledge/reranker.py:15-117` |

**核心组件**: `SemanticIndexer` (`knowledge/semantic_index.py`)

---

### Layer C — 项目规范（规范/约定）

**存储**: PostgreSQL `kb_norms`

**写入路径**:

| 场景 | 入口 | 关键行号 |
|------|------|----------|
| 预处理自动提取 | `preprocess._phase_extract_norms` → `norms_extractor.extract_norms_from_project` → `NormsStore.add_norms_batch` | `project/preprocess.py:555-593` |
| 删除旧 auto 规范再插(幂等) | `NormsStore.delete_norms_by_tag(project_id, "auto")` | `project/preprocess.py:582`, `norms_store.py:251-259` |
| 单条添加 | `NormsStore.add_norm` | `norms_store.py:95-109` |
| 批量添加 | `NormsStore.add_norms_batch` | `norms_store.py:111-128` |

**提取源** (7种配置文件): `.editorconfig` / `pyproject.toml` / `.ruff.toml` / `setup.cfg` / `.eslintrc` / `.prettierrc` / `pom.xml`
→ `knowledge/norms_extractor.py:29-53`

**检索路径**:

| 层次 | 入口 | 关键行号 |
|------|------|----------|
| Brain 规范检索 | `retriever._retrieve_layer_c` | `knowledge/retriever.py:198-206` |
| 按项目全量读取 | `NormsStore.get_all_norms` | `norms_store.py:161-200` |
| 按 tag 查询 | `NormsStore.get_norms_by_tag` | `norms_store.py:204-230` |

**核心组件**: `NormsStore` (`knowledge/norms_store.py`) + `extract_norms_from_project` (`knowledge/norms_extractor.py`)

---

### Layer D — 行为索引（修改日志/共现/MR 历史）

**存储**: PostgreSQL `kb_modification_log` + `kb_co_occurrence` + `kb_mr_history`

**写入路径**:

| 场景 | 入口 | 关键行号 |
|------|------|----------|
| 增量更新修改日志 | `updater._update_layer_d` → `BehaviorStore.log_modifications_batch` | `knowledge/updater.py:501-516` |
| 共现关系更新 | `BehaviorStore` 内部自动更新 | `knowledge/behavior_store.py` |
| MR 历史同步 | `mr_history.sync_mr_history_from_gitlab` | `knowledge/mr_history.py:34-114` |

**检索路径**:

| 层次 | 入口 | 关键行号 |
|------|------|----------|
| Brain 共现分析 | `retriever._retrieve_layer_d` | `knowledge/retriever.py:208-215` |
| MR 历史查询 | `mr_history.query_mr_history_for_files` | `knowledge/mr_history.py:117-155` |

**核心组件**: `BehaviorStore` (`knowledge/behavior_store.py`) + `mr_history` (`knowledge/mr_history.py`)

---

### 完整检索流水线

`SwarmRetriever.retrieve_for_brain` (`knowledge/retriever.py:108-263`):

```
1. 加载项目摘要 & 预处理统计   → _load_project_meta       (L134-140)
2. 提取关键词                   → _extract_keywords        (L143)
3. Layer A: 结构索引精确定位     → _retrieve_layer_a        (L148-154)
4. A→依赖图扩展                 → _expand_dependency_files (L160-175)
5. Layer B: 语义扩展             → _retrieve_layer_b        (L180-189)
6. Layer C: Harness 规范         → _retrieve_layer_c        (L198-206)
7. Layer D: 共现分析             → _retrieve_layer_d        (L208-215)
8. L5/L6: 记忆检索               → memory.query_mistakes/successes (L218-246)
9. Rerank + Hybrid Fusion        → _rerank + _apply_hybrid_fusion (L248-250)
```

---

## 二、Memory L0-L6 各层职责与衰减逻辑

### 总览

| 层 | 名称 | 存储 | 持久性 | 核心文件 |
|----|------|------|--------|----------|
| L0 | 会话元数据 | 内存（BrainState） | ephemeral | `memory/session.py` |
| L1 | 用户画像 | PG `mem_user_profile` | 持久 | `memory/store.py:215-239`, `memory/profile.py` |
| L2 | 近期任务摘要 | PG `mem_task_summary` | 持久（滚动窗口50条） | `memory/store.py:241-300`, `memory/task_digest.py` |
| L3 | 滑动窗口上下文 | LangGraph State | 任务级 | `memory/sliding_window.py` |
| L4 | 知识库 A-D | PG + Qdrant | 持久 | 见第一章 |
| L5 | 错题集 | PG `mem_mistakes` + pgvector | 持久 + 衰减 | `memory/store.py:302-467`, `memory/decay.py` |
| L6 | 成功模式集 | PG `mem_successes` + pgvector | 持久 + 衰减 | `memory/store.py:469-671`, `memory/decay.py` |

### 各层职责详解

**L0 — 会话元数据** (`memory/session.py:1-45`)
- 构建一次会话信息: client/platform/python_version/timezone/git_branch
- 仅写入 BrainState，绝不持久化
- 入口: `build_session_metadata()` (:29)

**L1 — 用户画像** (`memory/profile.py` + `memory/store.py:215-239`)
- 回退链: 项目专属 → 用户全局 → 旧版 project_id → 代码默认 (`profile.py:22-57`)
- 写入: `MemoryStore.set_user_profile` (`store.py:227-239`)
- 读取: `resolve_user_profile` (`profile.py:22-57`) — 含 `_enrich_profile` 旧版补全 (:60-68)
- 格式化: Brain 版 (`profile.py:109-149`) / Worker 版 (`profile.py:152-180`)
- 加载入口: `load_profile_prompts` (`profile.py:183-195`)

**L2 — 近期任务摘要** (`memory/task_digest.py` + `memory/store.py:241-300`)
- 滚动窗口 50 条 (`store.py:111`, `L2_ROLLING_WINDOW = 50`)
- 写入后自动 DELETE 超出窗口的旧条目 (`store.py:259-271`)
- 加载: `load_recent_task_summaries` (`task_digest.py:15-30`)
- 格式化: `format_recent_tasks_for_brain` (`task_digest.py:33-63`)

**L3 — 滑动窗口** (`memory/sliding_window.py:1-152`)
- 任务执行期上下文压缩
- 优先级: P1(用户原始需求) > P2(Worker 产出) > P3(Brain 过程)
- 超预算时按优先级+时序 evict，evicted 内容摘要化 (:67-64)
- 入口: `compress_context_log` (:67-100)
- 辅助: `append_context_event` (:33-53), `truncate_text_to_tokens`

**L5 — 错题集** (`memory/store.py:302-467`)
- 写入: `write_mistake` (:304-339) / `write_mistake_with_vector` (:341-363)
- 检索: `query_mistakes` (:365-424) — pgvector 余弦距离 + decay_weight > 0.05 过滤 + 排除 archived/dismissed
- 重遇加分: `increment_mistake_occurrence` (:426-438) — occurrence_count + 1, decay_weight = LEAST(+0.1, 1.0)
- 人工标记: `dismiss_mistake` (:566-580) — 设置 status=dismissed, decay_weight=0

**L6 — 成功模式集** (`memory/store.py:469-671`)
- 写入: `write_success` (:471-504)
- 检索: `query_successes` (:506-550) — pgvector 余弦距离 + 排除 archived/dismissed
- 重用计数: `increment_success_reuse` (:552-564)
- 核心标记: `mark_success_core` (:582-596) — metadata.core_rule

### 衰减逻辑

**L5 衰减** (`memory/decay.py:80-198`)
- 每日执行: `decay_l5()` 或 `decay_l5_batch_sql()`
- 公式: `new_weight = old_weight * decay_factor ^ (1 / occurrence_count)` (occurrence_boost=True)
  - 默认 decay_factor = 0.9（每天衰减 10%）
  - occurrence_count > 1 时有效因子更温和
- 逐条衰减: `decay.py:110-133`
- 批量 SQL: `decay_l5_batch_sql` (:159-198) — ⚠️ 批量路径**忽略了 occurrence_boost**，直接用 `decay_weight * decay_factor`
- 删除阈值: decay_weight < 0.05 (delete_threshold)

**L6 衰减** (`memory/decay.py:202-311`)
- 每日执行: `decay_l6()` 或 `decay_l6_batch_sql()`
- 公式: `new_weight = old_weight * l6_decay_factor ^ (1 / (reuse_count + 1))`
  - 默认 l6_decay_factor = 0.95（每天衰减 5%，比 L5 温和）
  - reuse_count 越高衰减越慢
- 批量 SQL: `decay_l6_batch_sql` (:273-311) — 正确包含 `POWER(factor, 1.0/(reuse_count+1))`
- 删除阈值: 同 L5

**每日调度** (`memory/decay.py:315-359`)
- `start_daily_decay(hour=3, minute=0)` — 简易 asyncio 循环，非 cron
- 依次执行 L5 + L6

---

## 三、预处理 CodeGraph 构建流程 (scan/index/embed/analyze)

### 总入口

`preprocess_project(project_id, project_path)` (`project/preprocess.py:100-204`)

### Phase 1: SCAN — 文件结构扫描

**入口**: `_phase_scan` (`preprocess.py:280-341`)

**流程**:
1. 同步扫描 `_scan_sync` (:211-277) — `os.walk` 遍历，排除 `EXCLUDED_DIRS` + 隐藏目录
2. 收集: file_count / dir_count / language_breakdown / line_counts / file_list (含 rel_path/abs_path/language/lines/hash)
3. 模拟逐步进度 (batch_size = max(100, total//10)) (:301-311)
4. 保存文件索引到 `kb_file_index` via `_save_file_index` (:313-316)
5. 更新进度 + project.file_count (:318-341)

**排除规则**:
- 目录: `.git`, `node_modules`, `__pycache__`, `.venv` 等 (`preprocess.py:28-33`)
- 扩展名: `.pyc`, 图片, 视频, 压缩包, 数据库文件等 (`preprocess.py:35-43`)
- 文件大小: ≥10MB 不计算 hash (:258)

### Phase 1.5: NORMS EXTRACT — 规范提取

**入口**: `_phase_extract_norms` (`preprocess.py:555-593`)

**流程**:
1. 调用 `norms_extractor.extract_norms_from_project` — 7 种配置文件扫描
2. 先删旧 `tag="auto"` 的规范再插新（幂等）(:575-586)
3. 失败不阻断预处理 (:590-592)

### Phase 2: INDEX — CodeGraph 索引

**入口**: `_phase_index` (`preprocess.py:348-447`)

**流程**:
1. 检查 codegraph CLI 是否安装 — `_check_codegraph` (:362) → `codegraph --version`
2. 未安装则跳过，设置 `index_stats.skipped=True` (:364-379)
3. 已安装则执行:
   - `_run_codegraph(project_path)` → `codegraph.run_codegraph_full` (:397)
   - `codegraph.run_codegraph_full` 内部: `codegraph init && codegraph index` → 解析 SQLite DB → `CodegraphResult` (`codegraph.py`)
   - 符号写入 `kb_symbol_index` via `_save_symbol_index` (:409-412)
   - 依赖写入 `kb_dependency_graph` via `_save_dependency_graph` (:414-418)
4. 更新 project.graph_status = "INDEXED" (:433-439)

**CodeGraph 解析** (`project/codegraph.py`):
- `find_codegraph_db` (:96) — 定位 `.codegraph/codegraph.db` 或 `data.db`
- `_parse_new_codegraph` — colbymchenry 版: nodes 表 + edges 表
- `_parse_old_codegraph` — nicolo-ribaudo 版: symbols/references 表
- 符号类型的 node kinds: function/method/class/interface/struct/... (`_SYMBOL_NODE_KINDS`, :24-28)
- 依赖边类型: imports/calls/extends/implements/uses/... (`_DEPENDENCY_EDGE_KINDS`, :31-34)

### Phase 3: EMBED — 向量嵌入

**入口**: `_phase_embed` (`preprocess.py:454-548`)

**流程**:
1. 检查 Qdrant 是否在线 — `_check_qdrant` (:472) → `GET /collections`
2. Qdrant 不可用则跳过 (:472-484)
3. 从 PG `kb_symbol_index` 读取符号 — `_read_symbols_for_embed` (:487)
4. 构建嵌入文本 — `_build_embed_texts` (:511) — 签名|文档|位置
5. 生成嵌入 — `_embed_texts` (:512) — 三级回退 + 随机兜底
6. 存入 Qdrant — `_store_vectors_qdrant` (:525-532):
   - 删除该项目旧向量 (:957-964)
   - 批量 upsert (batch_size=100) (:966-1003)
   - 向量维度: `VectorParams(size=dim, distance=COSINE)` (:952)

### Phase 4: ANALYZE — LLM 项目摘要

**入口**: `_phase_analyze` (`preprocess.py:599-659`)

**流程**:
1. 构建分析输入 — `_build_analysis_input` (:617):
   - 目录树(max_depth=3) / README(≤5000字符) / 核心入口文件 / 语言分布 / 统计
2. 调用本地 LLM — `_call_local_llm` (:629) → `_call_local_llm_impl` (:1094-1175):
   - 尝试 1: OpenAI-compatible → MiniMax-M2.7-Pro (`:1125-1140`)
   - 尝试 2: SiliconFlow → Pro/zai-org/GLM-5.1 (`:1147-1162`)
   - 回退: 统计信息生成基本摘要 (`:1167-1175`)
3. 清理 thinking 标签 — `_clean_llm_summary` (`:645-648`, `:1178-1188`)
4. 保存摘要到 project.description — `_save_analysis_summary` (`:646-648`, `:1191-1198`)

---

## 四、增量更新 PG 队列消费 + Embedding 降级路径

### 完整增量更新链路

```
触发源 ─→ hooks.py ─→ scheduler.py ─→ updater.py ─→ 各层索引
```

#### 4.1 触发源

| 触发方式 | 入口 | 文件:行号 |
|---------|------|----------|
| 任务 accept 后 | `incremental_update_from_task` | `knowledge/hooks.py:78-98` |
| 后台非阻塞 | `schedule_incremental_update` | `knowledge/hooks.py:101-132` |
| Git push webhook | `handle_git_push_webhook` | `knowledge/hooks.py:135-159` |
| 一致性修复 | `repair_project_consistency` → `enqueue_kb_update` | `knowledge/consistency.py:97-139` |

#### 4.2 事件入队

**入口**: `enqueue_kb_update(event)` (`knowledge/scheduler.py:47-50`)

1. 获取共享单例 `KnowledgeUpdater` — `get_shared_updater()` (:16-22)
2. 事件级去重 — `dedupe_event(event)` (`updater.py:85-92`) → `dedupe_changes` (:71-82)
   - 同 file_path 保留最后一次变更
3. 序列化为 JSON 写入 PG `kb_update_events` — `updater.enqueue_event` (`updater.py:520-551`)
   - payload 包含: project_id / task_id / commit_hash / author / changes[] / metadata
   - 状态初始为 `pending`

#### 4.3 队列消费

**调度器**: `start_kb_update_scheduler` (`knowledge/scheduler.py:25-44`)
- API startup 时创建 asyncio 后台任务 (`api/app.py:654-657`)
- 每 5 秒轮询一次

**消费逻辑**: `KnowledgeUpdater.process_pending_events` (`updater.py:553-627`)

1. **SELECT + FOR UPDATE SKIP LOCKED** 批量拉取 ≤10 条 pending 事件 (`:566-580`)
   - 原子标记为 `processing`，避免并发重复消费
2. **按 project_id 分组** (`:585-590`)
3. **同项目多事件合并** — `_merge_project_events` (`updater.py:95-136`)
   - 收集所有 FileChange
   - `dedupe_changes` 同路径保留最后状态
   - 合并 metadata / task_id / commit_hash / author
4. **逐项目执行** `handle_event(merged)` (`:599`)
5. 成功 → 批量标 `done` (`:601-609`)
6. 失败 → 批量标 `failed` + 记录 error_message (`:611-625`)

**handle_event 详解** (`updater.py:293-329`):

```
对每个 FileChange:
  DELETED  → Layer A delete + Layer B delete
  RENAMED  → 删旧 + 索引新
  ADDED/MODIFIED → _index_file
    → Layer A: FileInfo upsert + AST/正则符号抽取 + upsert_symbols_batch
    → Layer B: 先删旧 chunk → index_source_file (或失败降级)

Layer D: 记录修改日志 + 共现
```

#### 4.4 Embedding 降级路径

**降级触发** (`updater.py:378-397`):
```
layer B 索引期间:
  try:
    semantic.delete_by_file → semantic.index_source_file
  except:
    ⚠️ Layer A 已成功，不回滚
    → _defer_embedding_retry: INSERT kb_pending_embeddings
```

**`kb_pending_embeddings` 表结构** (`updater.py:206-216`):
- PK: (project_id, file_path)
- 字段: change_type / language / retry_count / last_error / created_at
- UPSERT 语义: ON CONFLICT 更新 change_type/language/created_at

**重试消费** — `retry_pending_embeddings` (`updater.py:427-499`):
1. 加锁 (`self._lock`) — 与轮询/入队串行化
2. 查询 pending embeddings 按 created_at ASC, LIMIT 50 (`:443-453`)
3. 对每条:
   - 查项目路径 → 读最新文件内容
   - 尝试 `semantic.delete_by_file + index_source_file`
   - 成功 → DELETE from kb_pending_embeddings (`:481-485`)
   - 失败 → retry_count + 1 + 记录 last_error (`:488-496`)

**⚠️ 缺陷**: `retry_pending_embeddings` **没有自动调度**，需手动调用或外部定时触发。没有看到在 scheduler.py 或 app startup 中注册。

---

## 五、发现的问题

### 5.1 死代码 / 未使用代码

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 1 | `updater.py:631-644` `start_polling()` 方法从未被调用 — 实际使用的是 `scheduler.py` 中的轮询循环，此方法为遗留代码 | `knowledge/updater.py:631-644` |
| 2 | `event_bus.py` 的 `publish_kb_event()` 函数在项目中无任何调用方，属于死代码 | `knowledge/event_bus.py:16-33` |
| 3 | `store.py:341-363` `write_mistake_with_vector()` 无调用方 — 所有写入走 `write_mistake()` | `memory/store.py:341-363` |
| 4 | `decayer.py:406-427` `project_decay_curve()` 纯静态方法，无运行时调用方，可视化为调试辅助但从未被使用 | `memory/decay.py:406-427` |

### 5.2 逻辑矛盾 / 一致性问题

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 5 | **L5 批量衰减忽略 occurrence_boost**: `decay_l5_batch_sql()` (:159-198) 直接用 `decay_weight * decay_factor` 不考虑 occurrence_count；而逐条模式 `decay_l5()` (:110-133) 用 `decay_factor ^ (1/occurrence_count)`。批量路径与逐条路径衰减速率**不一致**，相同数据走不同路径结果不同 | `memory/decay.py:159-198` vs `:110-133` |
| 6 | **L6 批量衰减正确考虑 reuse_count**: 同上 L6 的 `decay_l6_batch_sql()` (:273-311) 用了 `POWER(factor, 1.0/(reuse_count+1))`，逻辑一致。L5 的批量实现遗漏了这一公式 | `memory/decay.py:273-311` |
| 7 | **预处理嵌入 vs 增量嵌入使用不同嵌入函数**: 预处理 Phase 3 使用 `_embed_texts` (`preprocess.py:880-930`)；知识检索 service 通过 `_shared_embed` 设置 `SemanticIndexer` 的嵌入函数 (`service.py:64-69`)。增量更新 `KnowledgeUpdater` 使用 `SemanticIndexer` 自带的嵌入。三处嵌入来源可能不一致（不同模型/不同端点） | `project/preprocess.py:880` vs `knowledge/service.py:64-69` vs `knowledge/semantic_index.py` |
| 8 | **`_phase_extract_norms` 在 Phase 1.5 执行时 norms_store 每次都新建连接** (:579-588)，与共享 updat er 的单例模式不一致，资源浪费 | `project/preprocess.py:579-588` |

### 5.3 TODO / 未完成功能

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 9 | `retry_pending_embeddings()` 无定时调度 — 只能手动调用。文档(`README.md:281`)声明「恢复后补处理」，但代码中无自动触发机制 | `knowledge/updater.py:427-499` |
| 10 | `start_daily_decay()` 为简易实现，文档注释「精确调度应使用外部 cron / APScheduler」(:323-324)，但未接入任何外部调度 | `memory/decay.py:315-359` |
| 11 | `_extract_symbols_simple` 对 C/C++/Rust/Swift 等语言无专门分支，走通用正则 (`:859-871`)，覆盖率低 | `knowledge/updater.py:859-871` |
| 12 | `kb_pending_embeddings` 无最大重试次数限制，失败无限累加 retry_count | `knowledge/updater.py:488-496` |

### 5.4 异常吞没

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 13 | `_defer_embedding_retry` 中暂存失败时用 `logger.debug` (:425) 降级了异常等级，embedding 重试队列本身写入失败应该用 warning | `knowledge/updater.py:424-425` |
| 14 | `hydrate_event_changes` 中文件读取失败 (`OSError`) 时直接 `continue` (:179-180)，跳过的文件不出现在 hydrated 列表中，调用方无法感知有文件被跳过 | `knowledge/updater.py:178-180` |
| 15 | `_save_file_index` / `_save_symbol_index` / `_save_dependency_graph` 全部用 `logger.warning` 后静默返回 (:733-734, :775-776, :801-802)，预处理报告无失败文件记录 | `project/preprocess.py:733-734, 775-776, 801-802` |
| 16 | `_call_local_llm_impl` 的三次回退用 `logger.warning` + `pass` (:1141-1144, :1163-1164)，无结构化错误上报 | `project/preprocess.py:1141-1144, 1163-1164` |
| 17 | `Service.retrieve_knowledge` 检索整体失败时返回空 context + warning (:112-124)，Brain 无法感知检索系统是否有故障 | `knowledge/service.py:112-124` |
| 18 | `profile.py:54` `resolve_user_profile` 捕获所有异常回退到默认画像 (:54)，用户画像查询失败完全无感知 | `memory/profile.py:54-55` |

### 5.5 疑似 Bug

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 19 | **随机向量嵌入回退**: `_embed_texts` 最终回退使用 `random.gauss(0,1)` 生成向量 (:928-930)，`random.seed(42)` 保证可复现，但这些向量**语义上是垃圾**，存入 Qdrant 后检索结果毫无意义。且只在日志 warning 中提示，不会在预处理进度中标记 | `project/preprocess.py:923-930` |
| 20 | **Qdrant point ID 碰撞风险**: `_store_vectors_qdrant` 使用 `hash(point_id) & 0x7FFFFFFFFFFFFFFF` 作为 Qdrant point ID (:993)，Python `hash()` 在不同进程间不为同一字符串生成相同值（PYTHONHASHSEED 随机化），同项目重复预处理会因 point ID 不同导致旧数据无法覆盖（旧向量未被删除则永久残留） | `project/preprocess.py:993` |
| 21 | **L5 批量衰减无 occurrence_boost 导致批量更激进**: 当 `project_id=None` 时走 `decay_l5_batch_sql` 路径，衰减速度比逐条路径快，多次出现的错题在批量模式下衰减过快可能被误删 | `memory/decay.py:102-104` |
| 22 | **`_phase_embed` 读符号来源与 `_phase_index` 写入来源不一致**: embed 阶段从 `kb_symbol_index` PG 表读取符号 (:487-488)，但 index 阶段写的符号来自 codegraph 解析结果 (:409-412)。如果 codegraph 没安装或跳过，`kb_symbol_index` 可能为空，embed 阶段也跳过。但如果 index 阶段部分成功部分失败，embed 读取的是部分数据 | `project/preprocess.py:487-497` vs `:409-418` |
| 23 | **`_embed_texts` 每次调用都重新加载模型**: 尝试 1 每次创建新 `SentenceTransformer("BAAI/bge-m3")` (:890-891)，对大批量符号嵌入会导致模型反复重新加载，性能极差 | `project/preprocess.py:889-892` |
| 24 | **`mark_success_core` 使用 f-string 拼接 JSON** (`memory/store.py:588-592`) — SQL 注入安全风险虽因参数化而可控，但 JSON 用 f-string 拼接而非 `Jsonb()` 序列化，如果 flag 值来自非硬编码则可能产生畸形 JSON | `memory/store.py:588-592` |
| 25 | **事件去重在入队时执行两次**: `scheduler.enqueue_kb_update` 先 `dedupe_event` (`scheduler.py:50`)，然后 `updater.enqueue_event` 又调用一次 `dedupe_event` (`updater.py:523`)，重复工作 | `knowledge/scheduler.py:50` vs `knowledge/updater.py:523` |
| 26 | **`retrieve_for_brain` 在 L5/L6 查询时同时修改数据**: 查到 mistakes/successes 后立即 `increment_mistake_occurrence` / `increment_success_reuse` (`retriever.py:226-239`)，检索操作有副作用，违反 CQRS 原则。更严重的是，每次检索都会增加权重，同一查询反复调用会人为推高权重 | `knowledge/retriever.py:226-239` |

### 5.6 与设计不符

| # | 问题描述 | 文件:行号 |
|---|---------|----------|
| 27 | **EVENT_QUEUE_DDL 中 `kb_pending_embeddings` 与队列表混定义**: `EVENT_QUEUE_DDL` 常量 (`updater.py:190-217`) 同时包含 `kb_update_events` 和 `kb_pending_embeddings` 两张表的 DDL。语义上一个是事件队列、一个是重试队列，应分离。`init_db.py` 通过 `EVENT_QUEUE_DDL` 创建表 (`:98-104`) 时两者不可分割 | `knowledge/updater.py:190-217` |
| 28 | **Memory L4 (知识库 A-D) 不在 memory 模块内管理**: `layers.py:10` 定义 `MEMORY_L4_KNOWLEDGE = "L4"` 指向知识库，但 L4 的全部实现散布在 `knowledge/` 模块，`memory/store.py` 的 `MemoryStore` 不包含任何 L4 逻辑，仅有 L1/L2/L5/L6 | `memory/layers.py:10` vs `memory/store.py` |
| 29 | **`SwarmRetriever` 同时负责 L5/L6 检索**: retriever 设计为「知识库检索器」，但 `retrieve_for_brain` 内部直接操作 L5/L6 (`retriever.py:218-246`)，跨域职责过重 | `knowledge/retriever.py:218-246` |
| 30 | **`_phase_extract_norms` 阶段名为 1.5 但实际在 scan 后、index 前执行**: 代码注释与进度上报的 phase 名称不一致 — 进度上报用 `phase="scanning"` (`preprocess.py:561-564`)，但实际是独立的规范提取步骤 | `project/preprocess.py:555-593` |

---

## 附录: 文件清单

### knowledge/ (11文件)

| 文件 | 行数 | 职责 |
|------|------|------|
| `updater.py` | 893 | 增量更新主逻辑 + 事件去重 + AST 符号抽取 + embedding 降级 |
| `retriever.py` | 743 | 4层+L5/L6 统一检索 + Rerank + Hybrid Fusion |
| `semantic_index.py` | ~460 | Layer B 向量索引（Qdrant CRUD） |
| `structure_index.py` | ~500 | Layer A 结构索引（PG CRUD） |
| `norms_extractor.py` | 470 | Layer C 规范自动提取（7种配置文件） |
| `norms_store.py` | 259 | Layer C 规范 PG 存储 |
| `consistency.py` | ~180 | 一致性检查 + 修复入队 |
| `scheduler.py` | 59 | 共享 Updater 单例 + PG 队列轮询启动 |
| `behavior_store.py` | ~310 | Layer D 行为索引（修改日志 + 共现） |
| `service.py` | 309 | Brain/Worker 共享检索入口 + Prompt 格式化 |
| `hooks.py` | 159 | 任务生命周期触发的 KB 副作用 |
| `event_bus.py` | 33 | Redis Stream 事件发布（未使用） |
| `mr_history.py` | 155 | Layer D MR 历史索引 |
| `readiness.py` | 67 | 知识库就绪评估 |
| `reranker.py` | 117 | Cross-encoder 重排序 |

### memory/ (7文件)

| 文件 | 行数 | 职责 |
|------|------|------|
| `store.py` | 687 | L1/L2/L5/L6 PG+pgvector 统一存储 |
| `decay.py` | 427 | L5/L6 指数衰减（逐条/批量） |
| `sliding_window.py` | 152 | L3 滑动窗口上下文压缩 |
| `pattern_extractor.py` | 203 | L5/L6 写入门槛 + 结构化抽取 |
| `session.py` | 45 | L0 会话元数据 |
| `profile.py` | 195 | L1 用户画像读取 + 格式化 |
| `task_digest.py` | 63 | L2 近期任务摘要读取 + 格式化 |
| `layers.py` | 17 | L0-L6 + V1-V3 常量定义 |

### project/ (4文件)

| 文件 | 行数 | 职责 |
|------|------|------|
| `preprocess.py` | 1198 | 4阶段预处理编排 (scan/norms/index/embed/analyze) |
| `store.py` | 1272 | PG 持久化 (Project/TaskRecord/PreprocessProgress CRUD) |
| `codegraph.py` | 341 | CodeGraph CLI 封装 + SQLite 解析 |
| `diff_apply.py` | ~80 | Unified diff 解析 |
| `models.py` | 126 | Pydantic v2 数据模型 |
