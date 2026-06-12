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


class ModelConfig(BaseSettings):
    """模型路由配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_MODEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Brain 层
    brain_primary: str = "Pro/zai-org/GLM-5.1"
    brain_fallback: str = "moonshotai/Kimi-K2.6"

    # Worker 层
    worker_primary: str = "MiniMax-M2.7-Pro"
    worker_local: str = "qwen3:27b"          # 本地 Ollama
    worker_fallback: str = "Qwen3.5"

    # API 端点
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
    # 流式无 chunk 看门狗：两次 parsed chunk 间隔超此秒数即中断，触发 fallback。
    # 默认 45s（远端 vLLM/网关偶发 stall，越早中断 fallback 越快接管；过小会误杀慢首 token）。
    stream_chunk_timeout: float = 45.0


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
    # 留空的语言回退到 default_template + 运行时 setup_commands。
    # 用户已构建：go/node/java/rust/python 各一个镜像。
    template_python: str = "tpl-8fa882f5d775429cad1530c9"
    template_node: str = "tpl-530d6aa6162b41e38a790b30"
    template_java: str = "tpl-d3098a499a25492282284a76"
    template_go: str = "tpl-edf1a5aec16343249304abe3"
    template_rust: str = "tpl-d480ef3bd69f49c2b07930af"
    # 热沙箱池（默认关闭，SWARM_SANDBOX_POOL_ENABLED=true 开启）
    pool_max_idle_per_template: int = 2
    pool_max_total: int = 8
    pool_ttl_seconds: int = 600
    pool_idle_seconds: int = 300
    pool_reap_interval: int = 60

    def template_for_language(self, language: str) -> str:
        """语言 → 预建模板 ID。未知语言或未配置则回退 default_template。

        让 worker 按子任务语言起预装工具链的镜像（避免运行时 setup 慢/脆）。
        """
        lang = (language or "").lower()
        mapping = {
            "python": self.template_python,
            "node": self.template_node,
            "java": self.template_java,
            "go": self.template_go,
            "rust": self.template_rust,
        }
        return mapping.get(lang, "") or self.default_template


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
    chunk_size: int = 512
    chunk_overlap: int = 50
    retrieval_top_k: int = 20
    rerank_top_k: int = 5
    index_update_timeout: int = 30    # 秒


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

    # 子配置
    db: DatabaseConfig = Field(default_factory=DatabaseConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


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
