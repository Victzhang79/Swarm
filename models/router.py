"""模型路由 — 动态根据子任务难度/模态选择模型 + Fallback

接入点模型（providers）：每个模型显式归属一个 provider（云端 API 或本地推理服务），
路由按 provider 配置构建 ChatOpenAI —— 不再靠"模型名含 / 就是云端"的脆弱启发式。
老配置（仅 siliconflow + local 两个扁平字段）由 ModelConfig._effective_providers()
自动合成两个 provider，向后兼容零迁移。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from swarm.config.settings import ModelConfig, ProviderConfig, get_config

logger = logging.getLogger(__name__)


class ModelInvocationLogger(BaseCallbackHandler):
    """记录【实际被调用】的模型 + endpoint，并在 fallback 触发时显式告警。

    解决可观测性盲区：with_fallbacks 会在 primary 失败时静默切到 fallback，
    审计日志只记路由【意图】的 primary 名，无法证明到底哪个模型/endpoint 真干活。
    本回调在每次 LLM 真正启动时打印 model+provider，失败时打印错误，让降级可见。
    """

    def __init__(self, role: str, model_name: str, provider_id: str = "") -> None:
        self.role = role
        self.model_name = model_name
        self.provider_id = provider_id

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        is_fallback = "/fallback" in self.role
        tag = "⚠️ FALLBACK 降级" if is_fallback else "primary"
        logger.info(
            "[模型调用] role=%s %s 实际模型=%s provider=%s",
            self.role, tag, self.model_name, self.provider_id or "?",
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.warning(
            "[模型调用] role=%s 模型=%s 调用失败(可能触发 fallback): %s",
            self.role, self.model_name, str(error)[:200],
        )


@runtime_checkable
class ModelProvider(Protocol):
    """模型提供者协议"""
    def get_chat_model(self, model_name: str, temperature: float = 0.2) -> BaseChatModel: ...


class EndpointProvider:
    """通用接入点提供者 —— 按 ProviderConfig 构建 OpenAI 兼容 ChatModel。

    替代原先写死的 SiliconFlowProvider / LocalProvider。重试策略按 provider.kind:
    - local：max_retries=0（取消后绝不重发，避免占 GPU）
    - cloud：max_retries=config.max_retries
    provider.max_retries 显式设置时优先。
    """

    def __init__(self, provider: ProviderConfig, model_config: ModelConfig):
        self.provider = provider
        self.config = model_config

    def _resolve_retries(self) -> int:
        if self.provider.max_retries is not None:
            return self.provider.max_retries
        return 0 if self.provider.kind == "local" else self.config.max_retries

    def get_chat_model(
        self, model_name: str, temperature: float = 0.2, callbacks: list | None = None
    ) -> BaseChatModel:
        from langchain_openai import ChatOpenAI
        # 本地推理服务常无需 key；空则用占位（vLLM/Ollama 网关忽略）。
        api_key: str = self.provider.api_key or "EMPTY"  # type: ignore[assignment]
        return ChatOpenAI(
            model=model_name,
            base_url=self.provider.base_url,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=temperature,
            timeout=self.config.timeout_seconds,
            max_retries=self._resolve_retries(),
            callbacks=callbacks,
            # streaming=True：取消/断连时 httpx 关闭流式连接，推理服务端(vLLM)
            # 检测到 client disconnect 即 abort 解码，释放 GPU；非流式则会跑完整段。
            streaming=True,
            # 流式无 chunk 看门狗：远端 stall 时尽早中断 → fallback 更快接管。
            stream_chunk_timeout=self.config.stream_chunk_timeout,
        )


# ── 向后兼容别名 ─────────────────────────────────────────────
# 旧代码/测试 import SiliconFlowProvider / LocalProvider；保留为薄封装。
class SiliconFlowProvider(EndpointProvider):
    """[兼容] 旧的 SiliconFlow 提供者 —— 现合成一个 cloud provider。"""
    def __init__(self, config: ModelConfig | None = None):
        cfg = config or get_config().model
        super().__init__(
            ProviderConfig(
                id="siliconflow", label="SiliconFlow", kind="cloud",
                base_url=cfg.siliconflow_base_url, api_key=cfg.siliconflow_api_key,
            ),
            cfg,
        )


class LocalProvider(EndpointProvider):
    """[兼容] 旧的本地推理提供者 —— 现合成一个 local provider。"""
    def __init__(self, config: ModelConfig | None = None):
        cfg = config or get_config().model
        super().__init__(
            ProviderConfig(
                id="local", label="本地推理", kind="local",
                base_url=cfg.local_base_url, api_key=cfg.local_api_key,
            ),
            cfg,
        )


class ModelRouter:
    """动态模型路由器 — 根据子任务难度/模态选择模型，按 provider 归属构建。"""

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or get_config().model
        # 按 provider.id 缓存 EndpointProvider 实例
        self._providers: dict[str, EndpointProvider] = {
            p.id: EndpointProvider(p, self.config)
            for p in self.config._effective_providers()
        }

    def get_brain_llm(self) -> BaseChatModel:
        """Brain 编排层 — 必须大模型（云端优先，本地 fallback）"""
        primary = self._get_provider_for_model(self.config.brain_primary).get_chat_model(
            self.config.brain_primary, temperature=self.config.brain_temperature,
        )
        fallback = self._get_provider_for_model(self.config.brain_fallback).get_chat_model(
            self.config.brain_fallback, temperature=self.config.brain_temperature,
        )
        return primary.with_fallbacks([fallback])

    def get_llm_for_subtask(self, difficulty: str, modality: str = "text") -> Runnable:
        """根据子任务难度和模态动态选择模型"""
        primary_name, fallback_name = self._resolve_route(difficulty, modality)

        role = f"worker/{difficulty}"
        p_prov = self._get_provider_for_model(primary_name)
        primary = p_prov.get_chat_model(
            primary_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, primary_name, p_prov.provider.id)],
        )
        if fallback_name:
            f_prov = self._get_provider_for_model(fallback_name)
            fallback = f_prov.get_chat_model(
                fallback_name,
                temperature=self.config.worker_temperature,
                callbacks=[ModelInvocationLogger(role + "/fallback", fallback_name, f_prov.provider.id)],
            )
            return primary.with_fallbacks([fallback])
        return primary

    def get_alternate_llm_for_subtask(
        self, difficulty: str, modality: str = "text"
    ) -> tuple[Runnable, str]:
        """换备选模型路径（audit #34）：返回 (备选模型 LLM, 模型名)。

        用于失败重试时强制切换到备选模型。封装原先 dispatch 直接调用 ModelRouter
        私有方法(_resolve_route/_get_provider_for_model)的逻辑，恢复封装边界。
        优先用路由表的 fallback，无 fallback 时回退 primary。
        """
        primary_name, fallback_name = self._resolve_route(difficulty, modality)
        model_name = fallback_name or primary_name
        prov = self._get_provider_for_model(model_name)
        role = f"worker/{difficulty}/alternate"
        llm = prov.get_chat_model(
            model_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, model_name, prov.provider.id)],
        )
        return llm, model_name

    def _resolve_route(self, difficulty: str, modality: str) -> tuple[str, str]:
        """查路由表 → (primary_model_name, fallback_model_name)"""
        if modality == "multimodal":
            # 设计 v3 A.5：优先从能力库筛 supports_multimodal=True 的模型，
            # 而非读写死的 routing_multimodal。能力库无可用项则回退写死配置。
            mm_primary = self._multimodal_model_from_capabilities()
            if mm_primary:
                # primary 用能力库选出的真·多模态模型；fallback 仍用写死配置兜底
                return (mm_primary, self.config.routing_multimodal_fallback)
            return (
                self.config.routing_multimodal,
                self.config.routing_multimodal_fallback,
            )

        route_map = {
            "trivial": (self.config.routing_trivial, self.config.routing_trivial_fallback),
            "medium": (self.config.routing_medium, self.config.routing_medium_fallback),
            "complex": (self.config.routing_complex, self.config.routing_complex_fallback),
        }
        return route_map.get(
            difficulty,
            (self.config.routing_medium, self.config.routing_medium_fallback),
        )

    def _multimodal_model_from_capabilities(self) -> str | None:
        """从能力库挑一个 supports_multimodal=True 的模型（设计 A.5）。

        偏好：探测确认（source=probed/parsed/manual）的多模态模型优先于启发式默认；
        同等条件下 context_window 大者优先（看图常带长文本）。
        无能力库数据 / 无多模态模型 → 返回 None，调用方回退写死 routing_multimodal。
        """
        try:
            from swarm.models import capability_store as cap

            rows = cap.list_capabilities()
            mm = [r for r in rows if r.get("supports_multimodal")]
            if not mm:
                return None

            def _rank(r: dict) -> tuple[int, int]:
                # 探测确认的排前（source != default → 1），再按 context_window 降序
                confirmed = 0 if r.get("source") == cap.SOURCE_DEFAULT else 1
                return (confirmed, int(r.get("context_window") or 0))

            best = max(mm, key=_rank)
            return best.get("model_id") or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("从能力库选多模态模型失败，回退写死配置: %s", exc)
            return None

    def _get_provider_for_model(self, model_name: str) -> EndpointProvider:
        """模型 → EndpointProvider。按 config.provider_for_model() 显式归属优先，
        启发式兜底（向后兼容）。找不到任何 provider 时合成一个 local 兜底避免崩溃。
        """
        pc = self.config.provider_for_model(model_name)
        if pc is None:
            # 没有任何接入点配置 —— 合成一个 local 兜底（保持旧"默认本地"行为）
            fallback_pc = ProviderConfig(
                id="local", kind="local",
                base_url=self.config.local_base_url, api_key=self.config.local_api_key,
            )
            return EndpointProvider(fallback_pc, self.config)
        # 复用缓存实例（同 id）
        if pc.id in self._providers:
            return self._providers[pc.id]
        prov = EndpointProvider(pc, self.config)
        self._providers[pc.id] = prov
        return prov

    def get_worker_llm(self, strategy: str = "cost_optimized") -> Runnable:
        """获取 Worker LLM — 根据 strategy 选择模型

        Args:
            strategy: cost_optimized→trivial / quality→medium / complex→complex
        """
        strategy_map = {
            "cost_optimized": ("trivial", "text"),
            "quality": ("medium", "text"),
            "complex": ("complex", "text"),
        }
        difficulty, modality = strategy_map.get(strategy, ("medium", "text"))
        return self.get_llm_for_subtask(difficulty=difficulty, modality=modality)

    def get_model_by_name(self, model_name: str, temperature: float = 0.2) -> BaseChatModel:
        """按名称直接获取模型（带调用日志，证明实际用了哪个模型/endpoint）"""
        prov = self._get_provider_for_model(model_name)
        kind_label = "本地" if prov.provider.kind == "local" else "云端"
        return prov.get_chat_model(
            model_name,
            temperature,
            callbacks=[ModelInvocationLogger(
                role=f"worker/{kind_label}", model_name=model_name, provider_id=prov.provider.id,
            )],
        )

    def get_routing_table(self) -> dict:
        """返回当前路由表 + 接入点列表（给 API/前端用）"""
        providers = self.config._effective_providers()
        return {
            "brain_primary": self.config.brain_primary,
            "brain_fallback": self.config.brain_fallback,
            "providers": [
                {
                    "id": p.id, "label": p.display(), "kind": p.kind,
                    "base_url": p.base_url, "has_key": bool(p.api_key),
                }
                for p in providers
            ],
            "model_providers": dict(self.config.model_providers),
            "model_sizes": dict(self.config.model_sizes),
            "tiers": {
                "trivial": {
                    "primary": self.config.routing_trivial,
                    "fallback": self.config.routing_trivial_fallback,
                },
                "medium": {
                    "primary": self.config.routing_medium,
                    "fallback": self.config.routing_medium_fallback,
                },
                "complex": {
                    "primary": self.config.routing_complex,
                    "fallback": self.config.routing_complex_fallback,
                },
                "multimodal": {
                    "primary": self.config.routing_multimodal,
                    "fallback": self.config.routing_multimodal_fallback,
                },
            },
        }
