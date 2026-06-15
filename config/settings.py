"""Swarm 配置管理 — pydantic-settings"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent


class DatabaseConfig(BaseSettings):
    """数据库连接配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_uri: str = "postgresql://swarm:swarm@localhost:5432/swarm"
    redis_uri: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "swarm_kb"


class ProviderConfig(BaseSettings):
    """单个模型接入点（云端 API 或本地推理服务）。

    成熟 agent 的做法：接入点是一等公民，用户可配任意多个云端/本地端点，
    每个模型显式声明归属哪个 provider —— 不再靠"模型名含 / 就是云端"这种脆弱启发式。
    """
    id: str = ""                 # 唯一标识，如 siliconflow / deepseek / local
    label: str = ""              # 展示名（前端用），留空回退 id
    kind: str = "cloud"          # cloud | local —— 决定默认重试/超时策略
    base_url: str = ""
    api_key: str = ""
    # 本地推理服务通常 no_retry（取消后绝不重发，避免占 GPU）；云端可重试。
    # 留空(None)则按 kind 推导：local→0 重试，cloud→max_retries。
    max_retries: int | None = None

    def display(self) -> str:
        return self.label or self.id


# 预置云端接入点目录（base_url 来自 Hermes-Agent 源码，OpenAI 兼容端点）。
# 前端"添加接入点"时可一键选用，自动填 base_url/label/kind，用户只填 API Key。
# 仅作模板供选择，不自动启用——用户选了并填 key 才进 providers 配置。
KNOWN_PROVIDERS: list[dict] = [
    {"id": "openrouter",   "label": "OpenRouter（聚合 300+ 模型）", "kind": "cloud", "base_url": "https://openrouter.ai/api/v1",                              "key_hint": "OPENROUTER_API_KEY"},
    {"id": "siliconflow",  "label": "SiliconFlow 硅基流动",         "kind": "cloud", "base_url": "https://api.siliconflow.cn/v1",                            "key_hint": "SiliconFlow"},
    {"id": "deepseek",     "label": "DeepSeek 深度求索",            "kind": "cloud", "base_url": "https://api.deepseek.com/v1",                              "key_hint": "DEEPSEEK_API_KEY"},
    {"id": "minimax",      "label": "MiniMax（国际）",              "kind": "cloud", "base_url": "https://api.minimax.io/v1",                                "key_hint": "MINIMAX_API_KEY"},
    {"id": "minimax_cn",   "label": "MiniMax（国内）",              "kind": "cloud", "base_url": "https://api.minimaxi.com/v1",                              "key_hint": "MINIMAX_CN_API_KEY"},
    {"id": "moonshot",     "label": "Moonshot / Kimi（国内）",      "kind": "cloud", "base_url": "https://api.moonshot.cn/v1",                               "key_hint": "KIMI_API_KEY"},
    {"id": "zhipu",        "label": "智谱 GLM / Z.AI（国内）",      "kind": "cloud", "base_url": "https://open.bigmodel.cn/api/paas/v4",                     "key_hint": "GLM_API_KEY"},
    {"id": "dashscope",    "label": "阿里百炼 Qwen（国内）",        "kind": "cloud", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",        "key_hint": "DASHSCOPE_API_KEY"},
    {"id": "xai",          "label": "xAI Grok",                    "kind": "cloud", "base_url": "https://api.x.ai/v1",                                      "key_hint": "XAI_API_KEY"},
    {"id": "openai",       "label": "OpenAI",                      "kind": "cloud", "base_url": "https://api.openai.com/v1",                                "key_hint": "OPENAI_API_KEY"},
]


class ModelEntry(BaseSettings):
    """模型条目 —— 把模型按【接入点 × 规模】两个正交维度归类。

    location(local/cloud) 不单独存，从 provider.kind 推导；size 用户标注，
    供前端按"本地小/本地大/云端小/云端大"分组展示与按成本选型。
    """
    name: str = ""               # 模型名，如 Pro/zai-org/GLM-5.1
    provider_id: str = ""        # 归属的 provider.id —— 显式路由依据
    size: str = "large"          # large | small —— 规模维度（大模型/小模型）


class ModelConfig(BaseSettings):
    """模型路由配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_MODEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 多接入点（providers）+ 模型归属 ────────────────────────
    # providers 为空时，由 _effective_providers() 从下方老扁平字段(siliconflow+local)
    # 自动合成两个 provider —— 向后兼容，老 .env 零迁移即可工作。
    providers: list[ProviderConfig] = Field(default_factory=list)
    # 模型名 → provider_id 显式映射（覆盖一切猜测）。前端配置模型归属时写这里。
    model_providers: dict[str, str] = Field(default_factory=dict)
    # 模型规模标签：模型名 → "large"/"small"（仅供前端分组展示与选型提示，不影响调用）
    model_sizes: dict[str, str] = Field(default_factory=dict)

    # Brain 层
    brain_primary: str = "Pro/zai-org/GLM-5.1"
    brain_fallback: str = "moonshotai/Kimi-K2.6"

    # Worker 层
    worker_primary: str = "MiniMax-M2.7-Pro"
    worker_local: str = "qwen3:27b"          # 本地 Ollama
    worker_fallback: str = "Qwen3.5"

    # API 端点（兼容字段：providers 为空时合成默认的 siliconflow + local 两个接入点）
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_api_key: str = ""
    local_base_url: str = "http://ai.bit:3000/api"
    local_api_key: str = ""

    # 子任务路由分层
    routing_trivial: str = "Qwen3.6-27B-Saka"       # 简单任务首选（改CSS/修typo）
    routing_trivial_fallback: str = "Step-3.7-Flash" # 简单任务备选
    routing_medium: str = "MiniMax-M2.7-Pro"         # 中等任务首选（加API/修bug）
    routing_medium_fallback: str = "Qwen3.5-122B-A10B-NVFP4"  # 中等任务备选
    routing_complex: str = "Pro/zai-org/GLM-5.1"     # 复杂任务首选（架构重构/跨模块）
    routing_complex_fallback: str = "moonshotai/Kimi-K2.6"    # 复杂任务备选
    routing_multimodal: str = "Step-3.7-Flash"       # 多模态首选（看图/UI截图）
    routing_multimodal_fallback: str = "MiniMax-M2.7-Pro"     # 多模态备选

    # 参数
    brain_temperature: float = 0.1
    worker_temperature: float = 0.2
    max_retries: int = 2
    timeout_seconds: int = 120
    # worker 单次响应输出上限（token）。防止 worker 改大文件时全文重写输出撑爆 context
    # （实测 Qwen3.5-122B 改 877 行 StringUtils 时输出 38086 token，叠加输入 27451 超 65536
    # 上下文上限 → 400 报错 → 子任务失败）。worker 应用 patch_file 做最小改动，输出本不该
    # 这么大；限上限既强制增量编辑、又留 fallback 接管空间。0 表示不限制（向后兼容）。
    worker_max_tokens: int = 8192
    # 流式无 chunk 看门狗：两次 parsed chunk 间隔超此秒数即中断，触发 fallback。
    # 默认 45s（远端 vLLM/网关偶发 stall，越早中断 fallback 越快接管；过小会误杀慢首 token）。
    stream_chunk_timeout: float = 45.0

    # ── I1 模型能力分级（Brain 编排约束随模型能力调整）──────────────
    # tier_enabled 默认 False = 永远 standard = 现有硬编码约束上限，行为零变化（安全闸门）。
    # 显式开启后，按 Brain 主模型能力 tier 调整 clarify/design_reject/elaborate_resplit 上限：
    # 强模型收紧（少澄清/打回/拆分=降延迟），弱模型放宽（多兜底）。
    # tier 取值 ""（自动从 brain_primary 模型名推断）/ "strong" / "standard" / "weak"（手动覆盖）。
    # 对应 env：SWARM_MODEL_TIER_ENABLED / SWARM_MODEL_TIER。
    tier_enabled: bool = False
    tier: str = ""

    # ── 接入点解析 ────────────────────────────────────────────
    def _resolve_api_key(self, provider_id: str, env_fallback: str) -> str:
        """provider 的 api_key：优先从 db secret_store 解密读，回退 .env 明文值。

        敏感信息加密存 db（用户需求）；db 没有该项时无缝回退 .env，保证向后兼容、
        渐进迁移。延迟 import 避免与 secret_store 循环依赖。
        secret key 命名约定：provider_api_key:<provider_id>。
        """
        try:
            from swarm.config import secret_store

            val = secret_store.get_secret(f"provider_api_key:{provider_id}")
            if val:
                return val
        except Exception:  # noqa: BLE001
            pass
        return env_fallback

    def _effective_providers(self) -> list[ProviderConfig]:
        """返回生效的 provider 列表。

        providers 显式配置则用之；否则从老扁平字段合成 siliconflow(cloud) + local(local)
        两个默认接入点 —— 保证老 .env 不改也能工作。
        每个 provider 的 api_key 优先从 db secret_store 解密读取（回退 .env 明文）。
        """
        if self.providers:
            # 显式 providers：每个的 key 优先从 db 读（回退该 provider 自带的 .env 值）
            resolved: list[ProviderConfig] = []
            for p in self.providers:
                key = self._resolve_api_key(p.id, p.api_key)
                if key != p.api_key:
                    resolved.append(p.model_copy(update={"api_key": key}))
                else:
                    resolved.append(p)
            return resolved
        synthesized: list[ProviderConfig] = []
        if self.siliconflow_base_url:
            synthesized.append(ProviderConfig(
                id="siliconflow", label="SiliconFlow", kind="cloud",
                base_url=self.siliconflow_base_url,
                api_key=self._resolve_api_key("siliconflow", self.siliconflow_api_key),
            ))
        if self.local_base_url:
            synthesized.append(ProviderConfig(
                id="local", label="本地推理", kind="local",
                base_url=self.local_base_url,
                api_key=self._resolve_api_key("local", self.local_api_key),
            ))
        return synthesized

    def provider_for_model(self, model_name: str) -> ProviderConfig | None:
        """模型 → 接入点。优先显式映射(model_providers)，否则启发式兜底。

        启发式（仅兜底）：含 '/' 视为云端(取第一个 cloud provider)，否则本地。
        显式映射存在即权威，彻底摆脱'靠名字猜'。
        """
        providers = self._effective_providers()
        by_id = {p.id: p for p in providers}
        # 1) 显式映射
        pid = self.model_providers.get(model_name)
        if pid and pid in by_id:
            return by_id[pid]
        # 2) 启发式兜底（向后兼容老行为）
        if "/" in model_name:
            for p in providers:
                if p.kind == "cloud":
                    return p
        else:
            for p in providers:
                if p.kind == "local":
                    return p
        # 3) 实在没有就第一个
        return providers[0] if providers else None

    def models_in_use(self) -> list[str]:
        """用户模型策略里实际会用到的模型名集合（去重，保序）。

        = brain(primary+fallback) + worker(primary+local+fallback)
          + routing 四档(trivial/medium/complex/multimodal 各 primary+fallback)。
        探测只需覆盖这些 —— 云端聚合接入点可能列出几十上百模型，全探既花钱又无意义。
        """
        candidates = [
            self.brain_primary, self.brain_fallback,
            self.worker_primary, self.worker_local, self.worker_fallback,
            self.routing_trivial, self.routing_trivial_fallback,
            self.routing_medium, self.routing_medium_fallback,
            self.routing_complex, self.routing_complex_fallback,
            self.routing_multimodal, self.routing_multimodal_fallback,
        ]
        seen: dict[str, None] = {}
        for m in candidates:
            if m and m not in seen:
                seen[m] = None
        return list(seen.keys())

    def models_in_use_for_provider(self, provider_id: str) -> list[str]:
        """在用模型里、归属指定 provider 的那些（探测某接入点时的精确目标集合）。"""
        result: list[str] = []
        for m in self.models_in_use():
            pc = self.provider_for_model(m)
            if pc and pc.id == provider_id:
                result.append(m)
        return result


class WorkerConfig(BaseSettings):
    """Worker 容器和执行配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_concurrent: int = 4
    max_execution_time: int = 600     # 10 分钟
    max_iterations: int = 50          # Agent 最大 Tool 调用轮次
    max_fix_rounds: int = 3           # 编码内循环最大修复轮次
    memory_limit: str = "2g"
    disk_limit: str = "5g"
    command_whitelist: list[str] = Field(default_factory=lambda: [
        "mvn compile", "mvn test", "npm build", "npm test",
        "python -m py_compile", "python -m pytest",
        "tsc --noEmit", "eslint", "javac",
    ])
    # 安全审计阻断级别：critical/high=发现该级别漏洞则阻断交付；none=仅报告不阻断。
    # 满足"阻断交付 + 仅报告"双模式(用户决策)。
    security_block_severity: str = "critical"


class SandboxConfig(BaseSettings):
    """CubeSandbox / E2B 远程沙箱配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_SANDBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 默认留空 = 不启用远程沙箱（Worker 走本地执行降级）。真实部署在 .env 配置。
    # 注意：不要把私有开发 IP 硬编码为默认值——会导致 CI/他人环境探测到不可达甚至
    # 误连，且把内网地址泄露进仓库。
    api_url: str = ""
    api_key: str = ""
    proxy_base: str = ""
    sandbox_domain: str = "cube.app"
    verify_ssl: bool = False
    default_template: str = ""
    dev_sidecar_path: str = "test/sandbox/dev_sidecar.py"
    use_for_worker: bool = True
    sandbox_first: bool = True
    sandbox_remote_workdir: str = "/workspace"
    # 按语言预建的沙箱模板 ID（各装好对应工具链，避免运行时 setup 慢/脆）。
    # 区分两类（方案 B，按子任务性质选）：
    #   - exec(2c2g)：agent 写代码用，轻量。
    #   - verify(4c4g)：带完整环境+依赖缓存，重编译/集成验证用。
    # 运行时优先读 db(sandbox_templates 表，系统级 WebUI 可配)，db 空则用下面默认值。
    # 留空的语言回退到 default_template + 运行时 setup_commands。
    # exec 默认暂用旧轻量镜像（待补 2c2g 专用镜像 ID）；verify 默认为新 4c4g 带缓存镜像。
    template_python: str = "tpl-8fa882f5d775429cad1530c9"
    template_node: str = "tpl-530d6aa6162b41e38a790b30"
    template_java: str = "tpl-d3098a499a25492282284a76"
    template_go: str = "tpl-edf1a5aec16343249304abe3"
    template_rust: str = "tpl-d480ef3bd69f49c2b07930af"
    # 验证镜像默认（4c4g，带依赖缓存，warmup 好 .m2/node_modules/go mod/cargo）
    verify_template_python: str = "tpl-7bdf0d757d68421ab45320bb"
    verify_template_node: str = "tpl-5084cf67e28d4f14b16e0f33"
    verify_template_java: str = "tpl-431f89c0ced647919e673e05"
    verify_template_go: str = "tpl-c2a769763f804eefa53de627"
    verify_template_rust: str = "tpl-57e62a8b3af74a409655aaca"
    # 热沙箱池（默认启用，预热复用省冷启动；SWARM_SANDBOX_POOL_ENABLED=false 可关闭）
    pool_enabled: bool = True
    pool_max_idle_per_template: int = 2
    pool_max_total: int = 8
    pool_ttl_seconds: int = 600
    pool_idle_seconds: int = 300
    # A2 批2：跨项目隔离。False（默认）=按 template 复用沙箱（高复用率，靠 clean_workspace
    # 清理防泄漏）；True=池按 project+template 分桶，跨项目绝不复用同一沙箱（高隔离，
    # 牺牲复用率）。生产敏感项目可开启。
    isolate_per_project: bool = False
    pool_reap_interval: int = 60
    # 沙箱健康防护（修死循环烧资源）：
    # - 借/建沙箱后做 envd 健康探活，不健康则弃用换新（最多换 sandbox_health_retries 次）
    # - 运行中连续基础设施失败(5xx/连接)达 sandbox_fail_threshold 即熔断中止子任务
    sandbox_health_check: bool = True
    sandbox_health_retries: int = 2
    sandbox_fail_threshold: int = 5
    # 项目级定制沙箱（docs/Project_Scoped_Sandbox_Design.md）：
    # 预处理时按项目真实环境构建专属沙箱镜像（方案 B：自带完整源码），executor 优先用
    # project.config.sandbox_template。默认 True（通用主流程：所有有构建文件的项目都精准
    # 构建专属沙箱）。需沙箱机 SSH 凭据在 secret_store；无凭据/构建失败自动回退通用池。
    # 设 False 可全局关闭，所有项目用旧通用池。
    project_scoped_enabled: bool = True
    # 启动时清扫"残留孤儿沙箱"（12.2）。默认 True 保持单机部署行为（启动这一刻远端
    # 任何存活沙箱都是上一进程残留，安全清扫）。⚠️ 共享 CubeSandbox 集群部署务必设
    # False：本实例无差别 kill 服务器上所有沙箱会误杀其他实例/用户的沙箱。
    # 根治方案（按实例标签过滤）见 B 事项，落地后此开关可退役。
    sweep_orphans_on_startup: bool = True

    def template_for_language(self, language: str, purpose: str = "exec") -> str:
        """语言 + 用途 → 预建模板 ID。

        purpose='exec'(默认,写代码类子任务,2c2g) / 'verify'(重编译/集成验证类,4c4g)。
        优先读 db(sandbox_templates 表,系统级 WebUI 可配)，db 无则用 SandboxConfig 默认值。
        未知语言或未配置则回退 default_template。

        让 worker 按子任务语言+性质起合适镜像（执行省资源，验证用带缓存的完整环境）。
        """
        lang = (language or "").lower()
        # 1) 优先 db（落库的系统级配置）
        try:
            from swarm.config import sandbox_store

            db_val = sandbox_store.get_template(lang, purpose=purpose)
            if db_val:
                return db_val
        except Exception:  # noqa: BLE001
            pass
        # 2) 回退 SandboxConfig 默认值
        if purpose == "verify":
            verify_map = {
                "python": self.verify_template_python,
                "node": self.verify_template_node,
                "java": self.verify_template_java,
                "go": self.verify_template_go,
                "rust": self.verify_template_rust,
            }
            val = verify_map.get(lang, "")
            if val:
                return val
            # verify 未配则回退 exec 同语言（保证有可用镜像）
        exec_map = {
            "python": self.template_python,
            "node": self.template_node,
            "java": self.template_java,
            "go": self.template_go,
            "rust": self.template_rust,
        }
        return exec_map.get(lang, "") or self.default_template


class KnowledgeConfig(BaseSettings):
    """知识库配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_KB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # 专用 embedding / reranker 服务端点（ai.bit 上独立部署，非 LLM 网关）。
    # embed 走 OpenAI 兼容 /embeddings；rerank 走 {query, texts} → [{index, score}]。
    # 留空则回退旧逻辑（sentence-transformers → local_base_url → SiliconFlow）。
    embed_base_url: str = "http://ai.bit:8082/v1"
    embed_api_key: str = ""
    rerank_url: str = "http://ai.bit:8081/rerank"
    rerank_api_key: str = ""
    # 格式适配（embed 统一 OpenAI /embeddings；rerank 三种：
    #   simple        = 自建 {query,texts} → [{index,score}]（默认，对应 ai.bit:8081）
    #   openai_rerank = SiliconFlow/OpenAI 兼容 /rerank {model,query,documents,top_n}
    #   cohere_rerank = Cohere /v1/rerank
    embed_format: str = "openai"
    rerank_format: str = "simple"
    # 复用 LLM provider 的 Key（只在 base_url 同源时生效，读取时回退；自己有 key 优先）。
    # 留空=不复用；填 provider id（如 "siliconflow"）=从该 provider 取 key。
    embed_reuse_provider: str = ""
    rerank_reuse_provider: str = ""
    chunk_size: int = 512
    chunk_overlap: int = 50
    retrieval_top_k: int = 20
    rerank_top_k: int = 5
    # 检索调优（局域网 embed/rerank 服务，调用便宜但仍按需控量）
    embed_batch_size: int = 32           # 服务端 batch 上限（bge-m3=32），分批避 422
    rerank_score_threshold: float = 0.0  # rerank 分数低于此值的结果丢弃（0=不过滤）
    semantic_score_threshold: float = 0.0  # 向量相似度低于此值丢弃（0=不过滤）
    priority_file_top_k: int = 3         # priority 文件内每个取几条
    max_priority_files: int = 5          # 最多在几个 priority 文件内细检索
    hybrid_bm25_weight: float = 0.3      # 混合检索 BM25 权重（0=纯向量，1=纯关键词）
    # 周期全量重预处理（增量更新由 KBScheduler 处理；这里是兜底全量刷新）
    # 0 = 关闭（默认，仅靠增量 + 手动触发）；>0 = 每 N 小时检查一次 stale 项目并重跑。
    auto_reprocess_hours: float = 0.0
    auto_reprocess_check_interval: int = 1800   # 调度器检查间隔（秒，默认 30 分钟）
    index_update_timeout: int = 30    # 秒


class ObservabilityConfig(BaseSettings):
    """OpenLIT/ClickHouse 可观测数据源（LLM/embed/rerank 调用 trace）。"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_OBS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 留空 host 则面板降级（前端显示"未配置"）。默认指向局域网 OpenLIT ClickHouse。
    clickhouse_http_url: str = "http://ai.bit:8123"
    clickhouse_user: str = "admin"
    clickhouse_password: str = ""
    clickhouse_database: str = "openlit"
    query_timeout: int = 15


class NotifyChannel(BaseSettings):
    """单个外部通知渠道（飞书/Slack/钉钉/通用 webhook）。

    设计为列表项，即便当前单用户也预留 user_id（空=全局，将来多用户按 user 过滤投递）。
    events 留空 = 订阅所有事件；否则只推送列表内的 event_type。
    """
    id: str = ""                          # 唯一标识（前端生成，如 ch1）
    type: str = "generic"                 # feishu | slack | dingtalk | generic
    label: str = ""                       # 展示名
    webhook_url: str = ""
    enabled: bool = True
    user_id: str = ""                     # 预留：空=全局；将来按用户投递
    events: list[str] = Field(default_factory=list)  # 空=全部事件


# 预置通知渠道类型目录（前端"添加渠道"下拉用）。payload 格式见 api/notify.py。
KNOWN_NOTIFY_TYPES: list[dict] = [
    {"type": "feishu",   "label": "飞书 / Lark 群机器人", "url_hint": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
    {"type": "dingtalk", "label": "钉钉群机器人",          "url_hint": "https://oapi.dingtalk.com/robot/send?access_token=xxx"},
    {"type": "wecom",    "label": "企业微信群机器人",      "url_hint": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"},
    {"type": "slack",    "label": "Slack Incoming Webhook", "url_hint": "https://hooks.slack.com/services/xxx"},
    {"type": "generic",  "label": "通用 HTTP POST (JSON)",  "url_hint": "https://your-endpoint/webhook"},
]

# 所有系统通知事件类型（前端可选订阅；空订阅=全部）。与 store.create_notification 的 event_type 对齐。
NOTIFY_EVENT_TYPES: list[dict] = [
    {"type": "task_created",    "label": "任务创建"},
    {"type": "task_updated",    "label": "任务更新"},
    {"type": "task_completed",  "label": "任务完成"},
    {"type": "task_failed",     "label": "任务失败"},
    {"type": "awaiting_review", "label": "等待审核"},
    {"type": "task_approved",   "label": "审核通过"},
    {"type": "task_revised",    "label": "提交修订"},
    {"type": "task_rejected",   "label": "审核拒绝"},
]


class AppConfig(BaseSettings):
    """全局配置 — 聚合所有子配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Swarm"
    debug: bool = False
    workspace_root: Path = Field(default=PROJECT_ROOT / "workspace")

    # LangSmith 追踪
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "swarm-dev"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # 日志系统
    log_level: str = "INFO"               # DEBUG/INFO/WARNING/ERROR
    log_file: str = "swarm.log"           # 相对项目根或绝对路径；空串=仅控制台
    log_json: bool = False                # true=结构化 JSON 行（便于日志聚合）
    log_max_bytes: int = 20_000_000       # 单文件上限，超过轮转（默认 ~20MB）
    log_backup_count: int = 5             # 保留轮转文件数
    log_console: bool = True              # 是否同时输出到控制台/stderr

    # API 安全：多用户 RBAC（默认开启；关闭则匿名 admin）
    rbac_enabled: bool = True
    bootstrap_admin_password: str = "swarm"
    bootstrap_reset_admin_password: bool = False  # true 时每次启动重置 admin 密码
    # 遗留单 Key（非空且与 user token 匹配时视为 admin）
    api_key: str = ""
    max_task_tokens: int = 500_000  # 单任务 token 估算硬上限（P1）
    context_max_tokens: int = 80_000   # L3 滑动窗口总预算
    context_reserve_tokens: int = 16_000  # 预留给模型输出

    # 外部通知渠道（SWARM_NOTIFY_CHANNELS，JSON list）。系统每产生一条通知即推送到
    # enabled 且事件匹配的渠道。空列表=不推送外部（仅应用内铃铛）。
    notify_channels: list[NotifyChannel] = Field(default_factory=list)

    # 子配置
    db: DatabaseConfig = Field(default_factory=DatabaseConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


# 全局单例
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def reload_config() -> AppConfig:
    global _config
    _config = AppConfig()
    return _config
