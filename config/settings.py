"""Swarm 配置管理 — pydantic-settings"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_logger = logging.getLogger(__name__)


def _coerce_model_list(v: object) -> list[str]:
    """env 值 → list[str]。兼容三种写法（NoDecode 关掉自动 JSON 解码后由本函数接管）：
    - 纯字符串单模型 'A'         → ['A']（向后兼容旧 .env）
    - 逗号链 'A,B,C'             → ['A','B','C']（多级兜底链，推荐写法）
    - JSON 数组 '["A","B"]'      → ['A','B']
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):
            import json
            return [str(x).strip() for x in json.loads(s) if str(x).strip()]
        return [x.strip() for x in s.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent


# audit A-P1-29：.env 写入必须原子化 + 串行化。
# 历史问题：多处直接 path.write_text(...)（截断后重写），并发或写到一半进程被打断
# → .env 被截断/损坏 → 路由/密钥全丢，服务起不来。
# 修复：写同目录临时文件后 os.replace 原子改名（同 FS rename 原子），并用全局锁串行化。
import os as _os
import threading as _threading

_ENV_WRITE_LOCK = _threading.Lock()


import contextlib as _contextlib


@_contextlib.contextmanager
def env_file_lock(env_path: "Path | str"):
    """D47c：.env【读-改-写】全程互斥锁（fcntl.flock 于 <env>.lock sidecar 文件）。

    atomic_write_env 只保证单次【写】原子；读改写序列无锁时两个写者读到同一旧内容、
    后写覆盖前写（last-write-wins 丢键）。写者跨线程（async 端点在事件循环、密钥迁移在
    executor 线程），threading 锁不够语义清晰且不防多进程——flock 跨线程/跨进程通用。
    锁 sidecar 而非 .env 本身：atomic_write_env 用 os.replace 换 inode，锁旧 inode 会失效。
    所有 .env RMW 调用点（PUT /api/config、PUT /api/routing、_persist_env_updates、
    密钥迁移清明文、sandbox pool toggle）须在本锁内完成 读→改→写（→失败回滚）。
    """
    env_path = Path(env_path)
    lock_path = env_path.with_name(env_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def atomic_write_env(env_path: "Path | str", content: str) -> None:
    """原子写 .env：同目录写 tmp → fsync → os.replace 改名；全局锁串行化并发写。

    content 应为完整文件内容（含结尾换行）。任一步失败会清理 tmp，绝不留下截断的目标文件。
    """
    env_path = Path(env_path)
    directory = env_path.parent
    with _ENV_WRITE_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = __import__("tempfile").mkstemp(
            prefix=".env.", suffix=".tmp", dir=str(directory)
        )
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_name, env_path)  # 原子改名（同 FS）
        except BaseException:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise


class DatabaseConfig(BaseSettings):
    """数据库连接配置"""
    model_config = SettingsConfigDict(
        env_prefix="SWARM_DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # D15：默认 DSN 自带 connect_timeout（libpq URI 参数）——兜住未显式传 kwargs 的直连点
    # （scripts/checkpointer 等）。显式 kwargs（infra.db.pg_connect_timeout_kwargs）优先级更高；
    # 用户经 SWARM_DB_POSTGRES_URI 覆盖 DSN 时建议同样带上 connect_timeout。
    postgres_uri: str = "postgresql://swarm:swarm@localhost:5432/swarm?connect_timeout=10"
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
    # ⚠️ 多云必读(A-P1-15)：配置了 2 个及以上 cloud provider 时，含 '/' 的模型名
    # 启发式只会取「第一个」cloud provider——不同厂商模型会全部静默路由到同一家。
    # 多云场景务必在此为每个模型配置显式映射，否则路由不可控。
    model_providers: dict[str, str] = Field(default_factory=dict)
    # 模型规模标签：模型名 → "large"/"small"（仅供前端分组展示与选型提示，不影响调用）
    model_sizes: dict[str, str] = Field(default_factory=dict)

    # Brain 层（云端大模型编排，符合范式）：主 GLM-5.2(1024K 超长上下文)，
    # 备 Kimi-K2.7-Code(256K)。旧 Kimi-K2.6 在 SiliconFlow 403 private 不可用——见 PROJECT_STATUS T2。
    brain_primary: str = "zai-org/GLM-5.2"
    brain_fallback: str = "moonshotai/Kimi-K2.7-Code"

    # Worker 层
    worker_primary: str = "MiniMax-M2.7-Pro"
    worker_local: str = "qwen3:27b"          # 本地 Ollama
    worker_fallback: str = "Qwen3.6-27B-Saka-NVFP4"  # 大窗口本地（122B-A10B 64K 已排除）

    # API 端点（兼容字段：providers 为空时合成默认的 siliconflow + local 两个接入点）
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_api_key: str = ""
    local_base_url: str = "http://ai.bit:3000/api"
    local_api_key: str = ""

    # 子任务路由分层（worker 全部用【本地小模型】，云端只给 Brain——见 PROJECT_STATUS T2）。
    # primary 单模型；*_fallback 为【多级兜底链】(list)，主→次→兜底逐级降级，全本地。
    # 差异化分档让 4 个并发 worker 槽天然命中不同本地模型，分散推理负载。
    # fallback 字段用 NoDecode 关掉 pydantic JSON 自动解码，env 支持 'A,B,C' 逗号链写法。
    routing_trivial: str = "Qwen3.6-27B-Saka-NVFP4"   # 简单任务首选(改CSS/修typo)，轻快(112K)
    routing_trivial_fallback: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["MiniMax-M2.7-Pro", "Qwen3.6-40B-Claude-4.6-NVFP4"])
    routing_medium: str = "MiniMax-M2.7-Pro"          # 中等任务首选(加API/修bug)，196K
    routing_medium_fallback: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Qwen3.6-40B-Claude-4.6-NVFP4", "Qwen3.6-27B-Saka-NVFP4"])
    routing_complex: str = "Qwen3.6-40B-Claude-4.6-NVFP4"  # 复杂任务首选(架构/跨模块)，最强本地(256K)
    routing_complex_fallback: Annotated[list[str], NoDecode] = Field(
        # 用户编排(2026-06-18)：complex primary=40B 挂了先上 27B-Saka(轻快112k 先顶) →
        # 再另一台大的 MiniMax(196k 保上下文) → 最后 Step-Flash(256k 但 20t/s 慢，最终垫底)。
        # 真实机器在 .env 配同款链；此默认值是无 .env 环境(CI/他人)的策略落点，须与编排一致。
        # 全本地大窗口模型；122B-A10B 仅 64K 上下文，已排除出 worker 列表（易撑爆、拖累预算）
        default_factory=lambda: [
            "Qwen3.6-27B-Saka-NVFP4", "MiniMax-M2.7-Pro", "stepfun-ai/Step-3.7-Flash-FP8"])
    routing_multimodal: str = "Qwen3.6-40B-Claude-4.6-NVFP4"  # 多模态首选(看图/UI截图)，mm✓256K
    routing_multimodal_fallback: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Qwen3.6-27B-Saka-NVFP4", "stepfun-ai/Step-3.7-Flash-FP8"])

    @field_validator(
        "routing_trivial_fallback", "routing_medium_fallback",
        "routing_complex_fallback", "routing_multimodal_fallback",
        mode="before",
    )
    @classmethod
    def _normalize_fallback_chain(cls, v: object) -> list[str]:
        return _coerce_model_list(v)

    # 参数
    brain_temperature: float = 0.1
    worker_temperature: float = 0.2
    max_retries: int = 2
    # B9（2026-07-09 登记册）语义澄清：本值传给 openai 客户端的 timeout——对【流式】调用
    # （本系统恒 streaming=True）它是【连接/字节间隔超时】而非总时长；总时长由
    # progress-aware 双限（软限+硬顶，_invoke_llm_abortable）与 swarm_wallclock_budget
    # （模型级第三腿）另行把守。改大本值不会放宽总预算，改小会误杀慢首包。
    timeout_seconds: int = 120  # SWARM_MODEL_TIMEOUT_SECONDS（流式=字节间隔，非总超时）
    # worker 单次响应输出上限（token）。防止 worker 改大文件时全文重写输出撑爆 context
    # （实测 Qwen3.5-122B 改 877 行 StringUtils 时输出 38086 token，叠加输入 27451 超 65536
    # 上下文上限 → 400 报错 → 子任务失败）。worker 应用 patch_file 做最小改动，输出本不该
    # 这么大；限上限既强制增量编辑、又留 fallback 接管空间。0 表示不限制（向后兼容）。
    worker_max_tokens: int = 8192
    # brain 规划单次输出上限（token）。FINDING-10(task 25a6d83c)：brain 旧实现【不限】输出，
    # 云端 reasoning 模型(GLM-5.2)在某规划批陷入【失控持续生成】→ 无 chunk 看门狗抓不到(chunk
    # 一直在吐)、read-timeout 不管总时长 → PLAN 单批挂 16min。设上限封顶单次生成,失控时被截断
    # → 该批降级而非无限挂。32k 足够最长的两阶段方案/分批拆解输出。0=不限(向后兼容,不建议)。
    brain_max_tokens: int = 32768
    # 流式看门狗（治本 A：首 token 与解码间隔【双超时拆分】）。本质不同的两件事不该共用一个阈值：
    #   - first_token_timeout：等首 token（含 prefill）——并发 + 大上下文下本就慢，给宽（默认 180s）；
    #   - inter_chunk_timeout：解码中途两 chunk 间隔——本该快，真停 30s 就是异常，给紧（默认 30s）。
    # 旧的单值 stream_chunk_timeout（45→120）把两者混为一谈：调大才容得下慢 prefill，但同时纵容了
    # 真正的中途 stall（故障发现变慢）。拆开后：prefill 有空间、真 stall 仍秒级抓。是 ultra E2E
    # "全是调用超时→空 diff→假失败"的根治。stream_chunk_timeout 保留为兜底/同步路径上限。
    stream_chunk_timeout: float = 120.0
    first_token_timeout: float = 180.0
    inter_chunk_timeout: float = 30.0
    # 总时长看门狗（治本第三条腿）：单次 brain 流式累计超此秒数判 runaway → 抛 transient → fallback。
    # 双超时管【两 chunk 间隔】、max_tokens 管【输出长度】，都拦不住"稳定吐却吐不完"的 reasoning runaway
    # （实测 GLM-5.2 contract_design 稳定吐 6w+ chunk/22min 才 stall 失败，前 22min 全空烧、半成品作废）。
    # 取值权衡：合法慢调用实测达 24.5min（contract_design 单次成功），故默认设【保守兜底】1500s(25min)——
    # 只兜真正"永不收尾"的病态调用，不误杀合法慢调用；要对 runaway fail-fast（牺牲个别合法慢调用换 fallback
    # 重跑）可调低，但更优解是从源头限 reasoning（reasoning_effort/关 thinking）。0=关闭。worker 热路径不开
    # （已有 stall+worker_max_tokens=8192 双重兜底）。
    brain_stream_wallclock_s: float = 1500.0
    # R55-1（round55 实锤）：**思考阶段**预算（秒）。云端 reasoning 模型（GLM-5.2）会在思维链里
    # 原地打转：实测 EXTRACT_REQ 一次调用吐了 1471s / 79605 chunk，**其中前 400+ chunk 一个正文都没有**
    # ——全是 reasoning。而 max_tokens 只封最终答案（reasoning_content 豁免），双超时只看 chunk 间隔
    # （它一直在吐，看门狗认为"健康"），于是唯一的兜底是 1500s 墙钟：**先烧满 25 分钟**再抛 transient
    # 切备模型、从头重跑。代价太高。
    # 治本抓手（实测坐实）：SiliconFlow 的 `thinking:{"type":"disabled"}` **真生效**（同题 40.9s/2252
    # chunk → 15.6s/736 chunk，正文长度相当）；而 `reasoning_effort=low` 被**忽略**（44.4s，无变化）。
    # 机制：思考阶段【还没吐出任何正文】时中途 abort 是**无损的**（下游一个 chunk 都没收到）→ 超过本
    # 预算即就地关 thinking、用**同一个模型**重开流，下游无感，几十秒拿到正文；而不是丢掉 25 分钟成果
    # 再换模型从头来过。合法的深度思考不受影响（预算给足）。0=关闭。
    # R56-2：预算必须**明显早于**节点级处决闸——PLAN-BATCH 的 B5 硬顶也是 900s，两者同刻触发
    # 时谁先谁不确定；B5 若抢先杀流，救援就永远没机会出手（EXTRACT_REQ 无 B5，所以 round56 才救成）。
    # 600s：早于 B5(900s)/墙钟(1500s)，又远高于合法思考时长（实测合法慢调用的思考期远不到 10min）。
    # env: SWARM_MODEL_BRAIN_REASONING_PHASE_BUDGET_S
    brain_reasoning_phase_budget_s: float = 600.0
    # E2（round38c 主题E，register #32）：worker 单次流式调用总墙钟。R35-A/round38c 实证
    # 存在「单调用挂满 900s 总预算」形态（stall 看门狗可被 provider 心跳空 chunk 重置、
    # B6 槽位排队不计入 stall），届时整个 agent 被外层 cancel，无任何本步换备。开 wallclock
    # 后超时抛 TransientInfraError → with_fallbacks 链内即刻切备模型，900s 预算至少能试 2 个。
    # 取值：worker_max_tokens=8192 @ 高负载 ~20tok/s ≈ 410s 是合法慢调用下限 → 420s 不误杀；
    # 误杀后果也只是提前切备重试（非丢工作）。0=关闭（回退旧行为）。
    # env: SWARM_MODEL_WORKER_STREAM_WALLCLOCK_S
    worker_stream_wallclock_s: float = 420.0

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
            cloud = [p for p in providers if p.kind == "cloud"]
            if cloud:
                chosen = cloud[0]
                # A-P1-15：配置了 >1 个 cloud provider 却无显式 model_providers 映射时，
                # 启发式只会无脑取第一个 cloud → 不同厂商模型全部静默路由到同一家。
                # 当前部署(Brain 单云 + Worker 本地)碰不到，故仅告警(不改路由行为)，
                # 提示多云必须用显式 model_providers 映射。
                if len(cloud) > 1:
                    _logger.warning(
                        "[provider_for_model] 模型 '%s' 经启发式在 %d 个 cloud provider 中"
                        "选了 '%s'（首个）。多云场景下不同厂商模型会全部静默路由到同一家——"
                        "请在 model_providers 中为该模型配置显式映射以消除歧义。",
                        model_name, len(cloud), chosen.id,
                    )
                return chosen
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
            self.routing_trivial, *self.routing_trivial_fallback,
            self.routing_medium, *self.routing_medium_fallback,
            self.routing_complex, *self.routing_complex_fallback,
            self.routing_multimodal, *self.routing_multimodal_fallback,
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
    # worker 本地主力并行池：并发批次内同难度子任务轮转分配到这些模型，
    # 让两个能力相当的本地主力(Qwen3.6-40B-Claude 256K / MiniMax 196K)同时干、分散负载、产出更快。
    # 空列表 = 不轮转(按 difficulty 路由单一模型,向后兼容)。
    worker_parallel_pool: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Qwen3.6-40B-Claude-4.6-NVFP4", "MiniMax-M2.7-Pro"])
    # 部分交付：单个子任务重试耗尽时，放弃它(+依赖者)继续交付其余，终态 PARTIAL(非 DONE)，
    # 而非 fail-fast 灭掉整个任务(原行为：1 个子任务拒答 → 33 个好子任务一起 FAILED)。
    # True=部分交付(仍诚实标 PARTIAL，不假成功)；False=旧 fail-fast。
    allow_partial_delivery: bool = True
    max_execution_time: int = 900     # 15 分钟（安全垫：LOCATING 已封顶提速后，复杂子任务
                                      # CODING 仍可能逼近旧 600s 上限——RUN13 实测 9 文件子任务
                                      # 光 CODING 就 560s，VERIFY 没预算超时→重试死循环。配合
                                      # PLAN 端按层拆分(每子任务≤4文件)双管：拆分治本、预算兜底。
    max_iterations: int = 50          # Agent 最大 Tool 调用轮次
    max_fix_rounds: int = 3           # 编码内循环最大修复轮次
    # D59：原 memory_limit/disk_limit 定义即终点（全仓零消费者，从未接进 sandbox create）。
    # e2b/CubeMaster 的 Sandbox.create 参数面只有 template/timeout/metadata/request_timeout，
    # 不支持每沙箱资源上限（资源规格烤在模板镜像里）→ 无法接线，删除装饰性定义防误导运维。
    command_whitelist: list[str] = Field(default_factory=lambda: [
        "mvn compile", "mvn test", "npm build", "npm test",
        "python -m py_compile", "python -m pytest",
        "tsc --noEmit", "eslint", "javac",
    ])
    # 安全审计阻断级别：critical/high=发现该级别漏洞则阻断交付；none=仅报告不阻断。
    # 满足"阻断交付 + 仅报告"双模式(用户决策)。
    security_block_severity: str = "critical"

    @field_validator("worker_parallel_pool", mode="before")
    @classmethod
    def _normalize_parallel_pool(cls, v: object) -> list[str]:
        return _coerce_model_list(v)


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
    # P0-SEC-05：默认开启 TLS 校验（secure-by-default，防 MITM 注入沙箱产物字节）。
    # 内网自签证书部署须显式设 SWARM_SANDBOX_VERIFY_SSL=false 降级（见 .env.example）。
    verify_ssl: bool = True
    default_template: str = ""
    dev_sidecar_path: str = "test/sandbox/dev_sidecar.py"
    use_for_worker: bool = True
    # I-SEC-2（round38c 主题I·外部深审 CRITICAL）：沙箱【启用但创建失败】时是否允许
    # 降级到宿主机执行。默认 False=fail-closed——LLM 产的任意命令本该在沙箱隔离，
    # 静默降级=命令逃出隔离直接跑在 brain 宿主机（安全边界破坏）。单机开发无沙箱
    # 场景用 use_for_worker=False（显式本地模式）而非依赖此降级。
    # env: SWARM_SANDBOX_ALLOW_LOCAL_FALLBACK
    allow_local_fallback: bool = False
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
    # 专属镜像构建后 push 到的本地 registry（CubeSandbox 0.5.0 起 create-from-image 只从
    # registry 拉镜像、不再读本地 dockerd → 必须经 registry 中转，见 2026-07-06 治本）。
    # 留空=旧行为（直接用本地 docker tag，仅 ≤0.4.0 有效）。默认 localhost:5000（沙箱机本地
    # registry，CubeMaster 解析 localhost 不出网、绕开被墙的 Docker Hub）。build_project_image
    # 会在沙箱机上按需自启该 registry（用 build_registry_image）。
    build_registry: str = "localhost:5000"
    # 自启本地 registry 用的镜像（须沙箱机本地已有或可达 mirror 拉取；Docker Hub 被墙环境
    # 用腾讯/阿里公共 mirror 的 registry:2）。
    build_registry_image: str = "ccr.ccs.tencentyun.com/library/registry:2"
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
    embed_dimension: int = 1024          # 向量维度（bge-m3=1024）——建 Qdrant 集合与写入前校验的单一来源
    rerank_score_threshold: float = 0.0  # rerank 分数低于此值的结果丢弃（0=不过滤）
    semantic_score_threshold: float = 0.0  # 向量相似度低于此值丢弃（0=不过滤）
    priority_file_top_k: int = 3         # priority 文件内每个取几条
    max_priority_files: int = 5          # 最多在几个 priority 文件内细检索
    hybrid_bm25_weight: float = 0.3      # 混合检索 BM25 权重（0=纯向量，1=纯关键词）
    # 周期全量重预处理（增量更新由 KBScheduler 处理；这里是兜底全量刷新）
    # 0 = 关闭（默认，仅靠增量 + 手动触发）；>0 = 每 N 小时检查一次 stale 项目并重跑。
    auto_reprocess_hours: float = 0.0
    auto_reprocess_check_interval: int = 1800   # 调度器检查间隔（秒，默认 30 分钟）
    # D59：原 index_update_timeout 定义即终点（全仓零消费者）→ 删除装饰性定义。增量索引
    # 的超时由各调用点显式传参/自身默认值控制，从未读过此配置。
    # 增量更新累计 N 次文件变更后，后台触发一次依赖图重建（kb_dependency_graph
    # 只在全量 preprocess 时才建，增量更新只删自身出边兜底；累积漂移到阈值后
    # 触发真重建以纠正缺边）。<=0 关闭自动重建（仅删出边 + 阈值日志）。
    depgraph_rebuild_threshold: int = 50


class SkillsConfig(BaseSettings):
    """经验拔插层配置（swarm/experience）。house style：SWARM_SKILLS_* 前缀。

    本层是 advisory 知识注入（"怎么做更好"），永不阻断交付；任何异常 fail-open 到空串。
    总开关 SWARM_SKILLS_ENABLED=0 = 整层旁路（不加载、不注入）。
    """
    model_config = SettingsConfigDict(
        env_prefix="SWARM_SKILLS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = True
    # 技能库目录。相对路径以包根(PROJECT_ROOT)解析；绝对路径原样。逗号分隔可挂多目录
    # （靠前优先）：内置库 + 用户导入库（如 ECC / 自有技能包的 <name>/SKILL.md 目录），
    # 让"既有内置"与"用户可导入"并存——先出现目录的同 id 技能覆盖后者。
    dir: str = "skills_library"
    planner_budget_chars: int = 1500   # planner【全文注入】字符预算（planner 无工具调用能力，只能 push）
    max_k: int = 5                     # planner 全文注入的技能数上限（进 prefill，故保守）
    # 混合式 push+pull：选择器按 栈×意图×阶段 收窄候选，每条注册成一个离散工具
    # experience__<id>，由小模型自己决定调哪个（或不调）。
    # G2（阶段E 止血，2026-07-10）：15 使工具面常态满额（基础 12+15=27，实测 Java 候选 21/
    # 双栈 28/栈探测失败也满挂 15 条纯通配）——是 C10 处方红线（经验≤3）的 5 倍、复读死循环
    # 土壤。收紧默认 3（对齐 G8 拍板 pull ≤3）；0 = 不挂经验工具。
    # tool_body_max_chars = 单个经验工具返回正文的上限。
    worker_max_tools: int = 3
    tool_body_max_chars: int = 4000
    # R40-3（round40 定案）：两轮 tool-telemetry 实证 experience__ pull 调用恒 0
    # （小模型不接可选离散工具，纯占工具槽）→ push 从 top-1 扩到 top-K 全文注入，
    # pull 工具默认关（SWARM_SKILLS_WORKER_PULL_ENABLED=1 回退旧混合行为）。
    # E9-3 push 门槛（栈特化+框架相关）逐条保留；E9-5（worker_max_tools=0 全关）不变。
    # R53-7：2 → 3。相关性维度上线后，专精技能（jpa-patterns / maven-dependency-management /
    # springboot-security）会挤掉通用编码规范——但"该模块怎么写 Java"和"这个子任务在写什么"
    # 是两件事，两者都要。第 3 槽让专精 + 通用规范并存。
    worker_push_k: int = 3
    worker_pull_enabled: bool = False
    rerank: bool = False               # 可选 LLM rerank；默认关（确定性优先）
    # 导入准入闸的 LLM 一致性裁判（标题/描述 vs 正文意图）。默认开=严格；LLM 不可用时自动降级
    # 为仅确定性校验(不硬拦)。SWARM_SKILLS_ADMIT_LLM_JUDGE=0 可关(纯确定性准入)。
    admit_llm_judge: bool = True

    def dir_list(self) -> list[str]:
        """把逗号分隔的 dir 解析成去空目录列表（保序）。"""
        return [p.strip() for p in (self.dir or "").split(",") if p.strip()]


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
    # 运行环境：development（默认）/ production。production 时启动期强校验安全配置。
    env: str = "development"  # 来自 SWARM_ENV
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
    # W3.1：登录签发 token 的有效期（小时）。0=永不过期（向后兼容既有行为）。
    # >0 时登录会把 token_expires_at 刷新为 now()+TTL，并在登录响应回传 expires_at，
    # 前端据此到期前提示重登。吊销/过期校验能力见 auth.store.get_user_by_token。
    token_ttl_hours: int = 0  # SWARM_TOKEN_TTL_HOURS
    # 遗留单 Key（非空且与 user token 匹配时视为 admin）
    api_key: str = ""
    # round28：本闸门只拦【云端(付费)】真实消耗（store.check_task_token_limit 读 usage_tracker
    # 云端专计）——本地模型 token=自建算力(时间)，runaway 由墙钟(下方 task_deadline)+recursion_limit
    # 兜底，不计入本预算（实测本地 13.35M 合法消耗曾被误杀）。故下列数值现为【云端 $ 预算】口径。
    max_task_tokens: int = 500_000  # 单任务【云端】token 硬上限【基线】（P1；0=关闭闸门）
    # round27：token 上限对齐下方墙钟的【弹性预算】= base + per_subtask×子任务数。
    # 原 flat 500k 标定于 P1 的估算语义（description/diff 尺寸 sanity）；round26 B2 换成
    # 真实 LLM 累计主导后，ULTRA 任务仅规划期即 >800k（round27 E2E 86d24aa0 实测 826k 被
    # 误杀）——弹性随规划揭示的任务规模放宽，与墙钟同理【绝不误杀合法大型任务】，
    # 真失控仍由 base（规划前）与弹性上限（规划后）兜。0=弹性项关闭（退回 flat base）。
    max_task_tokens_per_subtask: int = Field(150_000, ge=0)
    # R38-A（round38 治本）：per_subtask 弹性在【规划期】恒为 0——子任务数是规划的输出，
    # ultra 任务规划流水线（N 模块两阶段设计+契约+抽取+拆批）只有 base×plan_ratio=125k 可用，
    # round38 实测 7/9 模块设计被预算闸拒绝（总花费才 27k）；round27 实测 ULTRA 仅规划期
    # 云端 >800k。故 TECH_DESIGN-STAGE1 揭示模块数后立即按 base+per_module×模块数 放宽
    # （ledger.widen_budget 单调只增不减，与墙钟 P1-B"规划揭示后动态重算"同模式）。
    # 标定（R38b-1 ③ 上修 200k→300k）：round38b 实测 10 模块规划期 ~750k（506k 上游
    # +13 拆批），round27 实测 826k——经 plan_ratio 0.35 折算单模块 ≈75k/0.35/n → 300k
    # 让常态不触发阶段借位（借位是安全阀非常态）。0=关闭模块弹性（退回 flat base）。
    # SWARM_MAX_TASK_TOKENS_PER_MODULE 可调。
    max_task_tokens_per_module: int = Field(300_000, ge=0)
    # P1-B：单次 Brain 执行墙钟【弹性】上限，防失控任务（replan 空转/卡节点）无上限占沙箱/GPU。
    # ★整体考虑：绝不误杀合法大型任务★——实测合法 E2E 大任务跑 7-8h（见 DEVLOG round9 7h45m）。
    # 故用【弹性预算】：有效上限 = base + per_subtask×子任务数，随规划揭示的任务规模动态放宽
    # （与 recursion_limit 弹性同理）。只是【最外层兜底】——单次 LLM 流由 brain_stream_wallclock_s
    # (1500s) 兜、循环次数由 recursion_limit(≤300) 兜、沙箱由各自超时兜；本项斩的是这些都没兜住的
    # 累积性真失控。计【单次 active 执行段】(run_task/每次 resume)，不含人工审核等待。
    # base=0 关闭（不建议生产关）。SWARM_TASK_DEADLINE_S / SWARM_TASK_DEADLINE_PER_SUBTASK_S 可调。
    # ge=0：负值必是误配，启动即 fail（否则负数会与 0 一样静默关闭保护，运维无感知）。
    task_deadline_s: float = Field(21600.0, ge=0.0)            # 基线 6h（覆盖微/小/中任务，含慢本地端点余量）
    task_deadline_per_subtask_s: float = Field(1200.0, ge=0.0)  # 每子任务 +20min（45 子任务→6h+15h=21h，远超合法 8h）
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
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    def is_production(self) -> bool:
        """运行在生产环境（SWARM_ENV=production/prod）。"""
        return (self.env or "").strip().lower() in {"production", "prod"}


# 默认 bootstrap admin 密码（与 AppConfig.bootstrap_admin_password 默认值对齐）。
# 生产环境若仍是此值视为不安全，拒绝启动。
_DEFAULT_BOOTSTRAP_ADMIN_PASSWORD = "swarm"

# 公开默认 DB 弱凭据标记（DatabaseConfig.postgres_uri 默认 postgresql://swarm:swarm@...）。
# 生产环境 postgres_uri 若仍含此 user:pass 段即视为不安全。
_DEFAULT_DB_CREDENTIALS_MARKER = "swarm:swarm@"
# G1-1c：开发模式生产就绪提醒 warn-once（每次 reload_config 都调 validate → 重复刷屏）
_dev_security_warned: set[str] = set()


def validate_production_security(cfg: "AppConfig | None" = None) -> None:
    """生产模式启动期安全强校验（fail-closed）。

    仅当 is_production() 为真时，下列任一不安全配置都会 raise RuntimeError，
    让误配的生产部署在启动期就快速失败，而非带病运行到运行期才暴雷：
      1. 未显式设置 SWARM_SECRET_KEY —— 否则 secret_store 的 Fernet 根密钥会
         从公开默认连接串派生（弱保护，DB dump + 本仓库即可解密所有存储 key）。
         这里镜像 secret_store._get_fernet 的判定（os.environ 取 SWARM_SECRET_KEY 后 strip）。
      2. bootstrap_admin_password 仍是公开默认值 "swarm" —— 任何人都可登录 admin。
    开发模式（默认）永不 raise，仅在发现弱配置时打 warning 提示。
    """
    cfg = cfg if cfg is not None else get_config()
    # 镜像 secret_store._get_fernet 的判定：os.environ 取 SWARM_SECRET_KEY 再 strip。
    # 注：此项在生产 hard-fail → secret_store 的弱 KDF（DB 串 SHA256 派生根密钥）回退路径
    # 在生产永不触发（本校验在 on_startup 早于任何 secret 访问），故弱 KDF 已被本项关闭。
    secret_key = _os.environ.get("SWARM_SECRET_KEY", "").strip()
    insecure_secret = not secret_key
    insecure_password = cfg.bootstrap_admin_password == _DEFAULT_BOOTSTRAP_ADMIN_PASSWORD
    # #8：生产禁用 RBAC = 所有请求按匿名 admin 放行（api/auth.py 的 rbac_enabled=False 分支），
    # 等于全站无鉴权。生产模式必须强制开启。
    insecure_rbac = not cfg.rbac_enabled
    # P1-D：公开默认 DB 弱凭据 swarm:swarm —— 生产库若仍用它 = DB 可被任意猜测登录，
    # 危害等同默认 admin 密码。用 cfg.db（与其余检查同源，不另建 DatabaseConfig）。
    # 已知范围：只判嵌入 URI 的默认弱密码 swarm:swarm；swarm 用户无密码（依赖 .pgpass/trust）
    # 不判——避免误伤 .pgpass/PGPASSWORD 的合法部署（密码不在 URI ≠ 不安全）。
    insecure_db = _DEFAULT_DB_CREDENTIALS_MARKER in (cfg.db.postgres_uri or "")
    # P1-D：token TTL=0 = 令牌永不过期（泄露即长期有效）。属硬化建议非致命洞，生产仅告警。
    insecure_token_ttl = (getattr(cfg, "token_ttl_hours", 0) or 0) <= 0

    if not cfg.is_production():
        # 开发模式不拦截，但提醒弱配置。G1-1c（round38c 主题G）：每次 reload_config 都
        # 调本函数 → round38c 里 bootstrap×18/rbac×14 重复刷屏。开发提醒首次有价值、
        # 重复是噪声——进程内 warn-once（按问题类型），之后同类静默。
        for _flag, _msg in (
            (insecure_secret, "未设置 SWARM_SECRET_KEY（开发模式放行）；生产部署前必须显式设置高熵根密钥。"),
            (insecure_password, "bootstrap_admin_password 仍为默认值（开发模式放行）；生产部署前必须改为非默认强密码。"),
            (insecure_rbac, "rbac_enabled=False（开发模式放行）；生产部署前必须开启 RBAC，否则全站匿名 admin 放行。"),
            (insecure_db, "DB 仍用公开默认弱凭据 swarm:swarm（开发模式放行）；生产部署前必须改为强凭据。"),
        ):
            if _flag and _msg not in _dev_security_warned:
                _dev_security_warned.add(_msg)
                _logger.warning(_msg)
        return

    problems: list[str] = []
    if insecure_secret:
        problems.append(
            "未设置 SWARM_SECRET_KEY：生产环境必须显式提供高熵根密钥（32 字节 base64），"
            "否则 secret_store 会用公开默认连接串派生的弱密钥加密所有敏感信息。"
            "请设置环境变量 SWARM_SECRET_KEY。"
        )
    if insecure_password:
        problems.append(
            'bootstrap_admin_password 仍为公开默认值 "swarm"：任何人都可登录 admin。'
            "请设置环境变量 SWARM_BOOTSTRAP_ADMIN_PASSWORD 为非默认强密码。"
        )
    if insecure_rbac:
        problems.append(
            "rbac_enabled=False：生产环境禁用 RBAC 会让所有请求按匿名 admin 放行（全站无鉴权）。"
            "请开启 RBAC（移除 SWARM_RBAC_ENABLED=false 或设为 true）。"
        )
    if insecure_db:
        problems.append(
            "DB 仍用公开默认弱凭据 swarm:swarm：生产数据库可被任意猜测登录。"
            "请设置 SWARM_DB_POSTGRES_URI 为使用强凭据的连接串。"
        )
    if problems:
        raise RuntimeError(
            "生产模式（SWARM_ENV=production）安全自检失败，拒绝启动：\n  - "
            + "\n  - ".join(problems)
        )
    # 非致命硬化建议（不阻断启动，仅告警）：
    if insecure_token_ttl:
        _logger.warning(
            "生产环境 token_ttl_hours=0（令牌永不过期）：泄露的令牌将长期有效。"
            "建议设 SWARM_TOKEN_TTL_HOURS 为合理值（如 24 或 168）以限制暴露窗口。"
        )
    # RBAC 开启时仍配 legacy SWARM_API_KEY = 一把静态 admin 万能钥匙，绕过用户/token
    # 吊销与过期（对抗复核）。不硬拦（可能是有意的服务账号），但生产必须告警周知。
    if cfg.rbac_enabled and (getattr(cfg, "api_key", "") or "").strip():
        # R23-7 治本：静态 legacy key = 无法吊销/过期的全局 admin 后门。生产默认【硬拦】启动，
        # 除非显式 SWARM_ALLOW_LEGACY_API_KEY=true（服务账号有意为之，自担风险）。原仅告警不阻断。
        if _os.environ.get("SWARM_ALLOW_LEGACY_API_KEY", "").strip().lower() not in ("1", "true", "yes", "on"):
            raise RuntimeError(
                "生产环境 RBAC 开启但仍设 legacy SWARM_API_KEY（等价【不可吊销/过期】的全局 admin 后门）。"
                "请清空 SWARM_API_KEY，改用可吊销的用户 token；如确为服务账号，显式设 "
                "SWARM_ALLOW_LEGACY_API_KEY=true 自担风险后再启动。"
            )
        _logger.warning(
            "生产环境仍启用 legacy SWARM_API_KEY（已 SWARM_ALLOW_LEGACY_API_KEY opt-in，自担风险）："
            "该静态 key 等价全局 admin 且无法吊销/过期，请尽快改用可吊销 token。"
        )


# 全局单例
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def reload_config() -> AppConfig:
    global _config
    candidate = AppConfig()
    # P1-D：先在【提交到全局 _config 之前】重跑生产安全门禁——否则运行期热更新可把生产配置改成
    # 不安全（禁 RBAC / 默认凭据 / 清 SECRET_KEY）而启动期校验管不到。生产下违规 → raise，
    # 且【不安装该不安全配置】（validate 在赋值 _config 之前，失败则 _config 保持旧安全值）。
    validate_production_security(candidate)
    _config = candidate
    # TD2606-C16：配置 reload 必须连带刷新依赖 .env 的下游 TTL 缓存（secret/sandbox/黑名单 store），
    # 否则新 base_url 配旧 key、旧沙箱模板等不一致最长可持续到各自 TTL 过期（~30s）。
    import importlib
    for _mod_name in (
        "swarm.config.secret_store",
        "swarm.config.sandbox_store",
        "swarm.config.command_blacklist_store",
        "swarm.config.skill_store",  # DB 系统级技能缓存
        "swarm.experience.service",  # 技能库缓存随 SWARM_SKILLS_DIR 等变更失效
    ):
        try:
            importlib.import_module(_mod_name).invalidate_cache()
        except Exception as e:  # noqa: BLE001 — 某 store 未加载/无缓存时不阻断 reload
            # 不阻断 reload，但留痕：静默 pass 会让"invalidate_cache 改名/真 bug/模块 import
            # 回归"导致缓存未刷新（配 stale）无迹可循。debug 级——多数是模块未加载的良性缺失。
            _logger.debug("[reload_config] 刷新缓存失败于 %s（缓存可能未刷新）：%s", _mod_name, e)
    return _config
