"""模型路由 — 动态根据子任务难度/模态选择模型 + Fallback"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from swarm.config.settings import ModelConfig, get_config

logger = logging.getLogger(__name__)


class ModelInvocationLogger(BaseCallbackHandler):
    """记录【实际被调用】的模型 + endpoint，并在 fallback 触发时显式告警。

    解决可观测性盲区：with_fallbacks 会在 primary 失败时静默切到 fallback，
    审计日志只记路由【意图】的 primary 名，无法证明到底哪个模型/endpoint 真干活。
    本回调在每次 LLM 真正启动时打印 model+base_url，失败时打印错误，让降级可见。
    """

    def __init__(self, role: str, model_name: str) -> None:
        self.role = role
        self.model_name = model_name

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        # 每个模型实例绑自己的 logger，触发即证明【这个】模型真在干活。
        is_fallback = "/fallback" in self.role
        tag = "⚠️ FALLBACK 降级" if is_fallback else "primary"
        logger.info("[模型调用] role=%s %s 实际模型=%s", self.role, tag, self.model_name)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.warning(
            "[模型调用] role=%s 模型=%s 调用失败(可能触发 fallback): %s",
            self.role, self.model_name, str(error)[:200],
        )


@runtime_checkable
class ModelProvider(Protocol):
    """模型提供者协议"""
    def get_chat_model(self, model_name: str, temperature: float = 0.2) -> BaseChatModel: ...


class SiliconFlowProvider:
    """SiliconFlow API 提供者"""
    def __init__(self, config: ModelConfig | None = None):
        self.config = config or get_config().model

    def get_chat_model(self, model_name: str, temperature: float = 0.2, callbacks: list | None = None) -> BaseChatModel:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_name,
            base_url=self.config.siliconflow_base_url,
            api_key=self.config.siliconflow_api_key,
            temperature=temperature,
            timeout=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
            callbacks=callbacks,
            # streaming=True：取消/断连时 httpx 关闭流式连接，推理服务端(vLLM)
            # 检测到 client disconnect 即 abort 解码，释放 GPU；非流式则会跑完整段。
            streaming=True,
            # 流式无 chunk 看门狗：远端 stall 时尽早中断 → fallback 更快接管。
            stream_chunk_timeout=self.config.stream_chunk_timeout,
        )


class LocalProvider:
    """本地推理服务器提供者（ai.bit:3000）"""
    def __init__(self, config: ModelConfig | None = None):
        self.config = config or get_config().model

    def get_chat_model(self, model_name: str, temperature: float = 0.2, callbacks: list | None = None) -> BaseChatModel:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_name,
            base_url=self.config.local_base_url,
            api_key=self.config.local_api_key,
            temperature=temperature,
            timeout=self.config.timeout_seconds,
            callbacks=callbacks,
            # 本地小模型：取消后绝不重试（避免取消瞬间又发起新请求继续占 GPU）。
            max_retries=0,
            # streaming=True：取消时关闭连接 → vLLM abort 解码序列，立即释放显存。
            streaming=True,
            # 流式无 chunk 看门狗：本地小模型(ai.bit 网关)偶发 stall(120s 无 chunk)，
            # 调短到 45s 让 fallback 尽早接管，避免每次抖动卡满预算。
            stream_chunk_timeout=self.config.stream_chunk_timeout,
        )


class ModelRouter:
    """动态模型路由器 — 根据子任务难度/模态选择模型"""

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or get_config().model
        self._siliconflow = SiliconFlowProvider(self.config)
        self._local = LocalProvider(self.config)

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

        # 把日志回调直接绑到各 ChatOpenAI 构造参数上（而非 with_config 包在外层）——
        # langgraph create_react_agent 会重绑 model，外层 with_config 的 callbacks
        # 可能丢失；绑在模型实例上才能在每次真实 LLM 调用时触发，证明哪个模型在干活。
        role = f"worker/{difficulty}"
        primary = self._get_provider_for_model(primary_name).get_chat_model(
            primary_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, primary_name)],
        )
        if fallback_name:
            fallback = self._get_provider_for_model(fallback_name).get_chat_model(
                fallback_name,
                temperature=self.config.worker_temperature,
                callbacks=[ModelInvocationLogger(role + "/fallback", fallback_name)],
            )
            return primary.with_fallbacks([fallback])
        return primary

    def _resolve_route(self, difficulty: str, modality: str) -> tuple[str, str]:
        """查路由表 → (primary_model_name, fallback_model_name)"""
        # 多模态优先
        if modality == "multimodal":
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

    def _get_provider_for_model(self, model_name: str) -> SiliconFlowProvider | LocalProvider:
        """根据模型名判断走哪个 provider"""
        # 含 '/' 的通常是 SiliconFlow 模型（如 Pro/zai-org/GLM-5.1）
        # 或者在本地模型列表里的走本地
        local_models: set[str] = {
            self.config.worker_primary,
            self.config.worker_local,
        }
        # 也检查路由表中的本地模型
        for attr in ("routing_trivial", "routing_medium", "routing_multimodal"):
            val = getattr(self.config, attr, "")
            if val:
                local_models.add(val)
            fb = getattr(self.config, f"{attr}_fallback", "")
            if fb:
                local_models.add(fb)

        if model_name in local_models and "/" not in model_name:
            return self._local
        if "/" in model_name:
            return self._siliconflow
        # 默认尝试本地
        return self._local

    def get_worker_llm(self, strategy: str = "cost_optimized") -> Runnable:
        """获取 Worker LLM — 根据 strategy 选择模型

        Args:
            strategy: 模型选择策略
                - cost_optimized: 使用 routing_trivial（轻量模型）
                - quality: 使用 routing_medium（中等模型）
                - complex: 使用 routing_complex（大模型）
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
        provider = self._get_provider_for_model(model_name)
        prov_kind = "本地" if isinstance(provider, LocalProvider) else "云端"
        return provider.get_chat_model(
            model_name,
            temperature,
            callbacks=[ModelInvocationLogger(role=f"worker/{prov_kind}", model_name=model_name)],
        )

    def get_routing_table(self) -> dict:
        """返回当前路由表（给 API/前端用）"""
        return {
            "brain_primary": self.config.brain_primary,
            "brain_fallback": self.config.brain_fallback,
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
