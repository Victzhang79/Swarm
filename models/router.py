"""模型路由 — 动态根据子任务难度/模态选择模型 + Fallback

接入点模型（providers）：每个模型显式归属一个 provider（云端 API 或本地推理服务），
路由按 provider 配置构建 ChatOpenAI —— 不再靠"模型名含 / 就是云端"的脆弱启发式。
老配置（仅 siliconflow + local 两个扁平字段）由 ModelConfig._effective_providers()
自动合成两个 provider，向后兼容零迁移。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol, runtime_checkable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from swarm.config.settings import ModelConfig, ProviderConfig, get_config

logger = logging.getLogger(__name__)


def _monotonic() -> float:
    """心跳计时时钟（独立间接层）。

    单列出来是为了【可测】：测试可只 patch 本函数喂受控时间，而不污染 asyncio 事件循环
    自身依赖的 time.monotonic（直接全局 patch 会让 wait_for 的计时一起崩）。
    """
    import time

    return time.monotonic()



def _breaker_error_transient(err: str) -> bool:
    """F-F（4.9 复核 T8）：判断 Runnable on_error 的错误串是否为可喂 breaker 的
    transient 基建形态（超时/连接/网关类）。

    langchain Run.error = repr(exc)+完整 traceback——在整段上做子串匹配会被行号
    （"502"命中 line 5021）与框架路径（"connect"命中 connectionpool.py 帧）假阳性，
    把 capability 错误误喂 breaker 熔断健康模型。只取首行（repr(exc) 部分）匹配；
    数字网关码要求词边界。capability（model_not_found/400 类）绝不喂。
    """
    first = (err or "").splitlines()[0].lower() if err else ""
    if not first:
        return False
    if any(k in first for k in ("timeout", "timed out", "connect", "stall", "unavailable")):
        return True
    import re as _re
    return bool(_re.search(r"\b50[234]\b", first))

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


class _UsageRecorder(BaseCallbackHandler):
    """在每次 LLM 调用结束时记录 token 用量（云端/本地 + 项目 + 模型）到 usage_tracker。

    统一在 get_chat_model 挂一次（那里 provider.kind/id/model 全在手），覆盖所有 cloud+local、
    brain+worker、primary+fallback 路径，且只挂一处=不重复计数。project_id 取自 worker/brain 调用
    前 set_worker_context 推入的 ContextVar（无上下文的早期调用归 ''=无项目归属）。best-effort。
    """

    def __init__(self, kind: str, provider_id: str, model_name: str) -> None:
        self.kind = (kind or "cloud").lower()
        self.provider_id = provider_id or ""
        self.model_name = model_name or ""
        self._starts: dict[Any, float] = {}  # run_id → 起始时刻（算单次调用耗时）
        # run_id → [max_input, max_output]：流式逐 chunk usage 的【按字段取最大】。
        # 治本【流式 usage 膨胀】：部分 OpenAI 兼容网关（实测云端 GLM）在【每个 chunk】都回
        # 累计 usage（input 恒定、output 单调增），而 langchain 拼接 AIMessageChunk 时把各
        # chunk 的 usage_metadata【逐字段求和】→ 末态 = Σ累计 ≈ N×真值（581 chunk → input 膨胀
        # 581 倍）。标准 OpenAI/本地仅末 chunk 带 usage，无此问题。统一【取各字段跨 chunk 最大值】
        # 即正解：累计型→max=末次累计=真总量；仅末chunk型→max=唯一值=真总量。两类网关都对。
        self._usage: dict[Any, list[int]] = {}

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        try:
            self._starts[kwargs.get("run_id")] = _monotonic()
        except Exception:  # noqa: BLE001
            pass

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        # 流式：逐 chunk 抓 usage_metadata，按字段累计【最大值】（不求和，避免累计型网关膨胀）。
        try:
            chunk = kwargs.get("chunk")
            um = getattr(getattr(chunk, "message", None), "usage_metadata", None) \
                or getattr(chunk, "usage_metadata", None)
            if not um:
                return
            rid = kwargs.get("run_id")
            slot = self._usage.get(rid)
            if slot is None:
                slot = [0, 0]
                self._usage[rid] = slot
            i = int(um.get("input_tokens", 0) or 0)
            o = int(um.get("output_tokens", 0) or 0)
            if i > slot[0]:
                slot[0] = i
            if o > slot[1]:
                slot[1] = o
        except Exception:  # noqa: BLE001
            pass

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            rid = kwargs.get("run_id")
            # 优先用流式逐 chunk 抓到的【字段最大值】（非 langchain 求和的膨胀末态）；
            # 无流式 chunk usage（非流式调用）才回退 LLMResult 的 token_usage/usage_metadata。
            tracked = self._usage.pop(rid, None)
            if tracked and (tracked[0] > 0 or tracked[1] > 0):
                prompt_t, completion_t = tracked[0], tracked[1]
            else:
                prompt_t, completion_t = _extract_token_usage(response)
            if prompt_t <= 0 and completion_t <= 0:
                return
            t0 = self._starts.pop(rid, None)
            dur_ms = int((_monotonic() - t0) * 1000) if t0 is not None else 0
            from swarm.knowledge.service import get_worker_project_id
            from swarm.models import usage_tracker
            usage_tracker.record(
                get_worker_project_id(), self.kind, self.provider_id, self.model_name,
                prompt_t, completion_t, duration_ms=dur_ms,
            )
        except Exception:  # noqa: BLE001
            pass  # 统计绝不拖垮模型调用

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        # B4（2026-07-09 登记册·阶段1）：中止/超时/掐流的调用【已收 chunk 的 usage 必须入账】——
        # 原直接 pop 丢弃 → token 硬顶对最烧钱的饱和形态（流中途被杀）失明。现按逐 chunk
        # 跟踪的字段最大值照常 record（best-effort，绝不拖垮错误传播）。
        rid = kwargs.get("run_id")
        t0 = self._starts.pop(rid, None)
        tracked = self._usage.pop(rid, None)
        try:
            if tracked and (tracked[0] > 0 or tracked[1] > 0):
                dur_ms = int((_monotonic() - t0) * 1000) if t0 is not None else 0
                from swarm.knowledge.service import get_worker_project_id
                from swarm.models import usage_tracker
                usage_tracker.record(
                    get_worker_project_id(), self.kind, self.provider_id, self.model_name,
                    tracked[0], tracked[1], duration_ms=dur_ms,
                )
        except Exception:  # noqa: BLE001
            pass  # 统计绝不拖垮错误传播


class _LedgerGuard(BaseCallbackHandler):
    """§九 TaskLedger 单点闸（阶段1.3）：LLM 调用发起前预留额度、结束按真实结算、
    error/中止按已收 chunk 结算——挂在 get_chat_model 唯一 chokepoint，覆盖全部
    cloud+local、brain+worker、primary+fallback 路径。

    raise_error=True：on_llm_start 预留失败抛 TaskTokenLimitExceeded 必须【中止调用】
    （拒绝发起，不再发出必然烧钱的请求）——这是"闸门查得晚"（原只在节点边界查）的治本。
    task 归属取 usage_tracker 的 ContextVar（调用发起点上下文）；未 attach 的任务
    在 ledger 侧是 track-only（budget=0 不闸），预处理/无任务上下文调用零影响。
    模型实例带回调被 D54 缓存跨任务复用 → 本类状态只按 run_id 键控、task 按调用时解析。
    """

    raise_error = True  # 预留失败必须传播中止调用（langchain 默认吞回调异常）

    # 预留估算参数：input 按 prompt 字符 //3（CJK ~1.7 char/tok 与 EN ~4 char/tok 折中，
    # 结算按真实 usage 校正）；output 无显式 max_tokens 时的保守预留（brain 不设上限，
    # 若按 brain_max_tokens=32768 预留会在预算尾段大面积误拒——预留是准入控制，
    # 低估的暴露面有界=单次调用真实输出，由 settle 立即校正）。
    _DEFAULT_OUT_RESERVE = 4096

    def __init__(self, kind: str, model_name: str, max_tokens: int | None) -> None:
        self.kind = (kind or "cloud").lower()
        self.model_name = model_name or ""
        self.max_tokens = int(max_tokens) if max_tokens and max_tokens > 0 else 0
        self._rids: dict[Any, str] = {}          # run_id → reservation_id
        self._usage: dict[Any, list[int]] = {}   # run_id → [max_in, max_out]（同 _UsageRecorder 口径）

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        from swarm.models import ledger, usage_tracker
        task_id = usage_tracker.get_current_task()
        if not task_id:
            return  # 无任务归属（预处理/探测）→ 不预留不闸
        # 复核 H4：与结算共用 CJK 感知估算器（原 //3 对 CJK 低估 2 倍）。
        est_in = sum(ledger.estimate_tokens_text(p or "") for p in (prompts or [])) + 64
        est_out = self.max_tokens or self._DEFAULT_OUT_RESERVE
        # reserve 余额不足抛 TaskTokenLimitExceeded → raise_error=True 传播 → 调用被拒绝发起
        rid = ledger.reserve(task_id, est_in=est_in, est_out=est_out, kind=self.kind)
        self._rids[kwargs.get("run_id")] = rid
        ledger.register_inflight_rid(rid)  # C7：登记到调用方作用域（硬杀即时结算）

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        # 逐 chunk 抓 usage（字段取最大，累计型网关口径，见 _UsageRecorder 注释）——
        # error/中止结算的依据。
        try:
            chunk = kwargs.get("chunk")
            um = getattr(getattr(chunk, "message", None), "usage_metadata", None) \
                or getattr(chunk, "usage_metadata", None)
            if not um:
                return
            rid = kwargs.get("run_id")
            slot = self._usage.get(rid)
            if slot is None:
                slot = [0, 0]
                self._usage[rid] = slot
            i = int(um.get("input_tokens", 0) or 0)
            o = int(um.get("output_tokens", 0) or 0)
            if i > slot[0]:
                slot[0] = i
            if o > slot[1]:
                slot[1] = o
        except Exception:  # noqa: BLE001
            pass

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            run_id = kwargs.get("run_id")
            rid = self._rids.pop(run_id, None)
            tracked = self._usage.pop(run_id, None)
            if rid is None:
                return
            from swarm.models import ledger
            if tracked and (tracked[0] > 0 or tracked[1] > 0):
                real_in, real_out = tracked[0], tracked[1]
            else:
                real_in, real_out = _extract_token_usage(response)
            if real_in <= 0 and real_out <= 0:
                # 网关不回 usage → 按估算结算（input 全额=预留估 + output 按响应文本估），
                # 宁可高估不让预算失明。复核 H4：每任务一次 WARNING（可观测，双账本
                # usage_tracker 此形态记 0，读数会分叉——账本以 ledger 为权威）。
                _out_est = _estimate_output_tokens(response)
                from swarm.models import usage_tracker as _ut
                _tid = _ut.get_current_task() or ""
                if _tid and _tid not in _NO_USAGE_WARNED:
                    if len(_NO_USAGE_WARNED) > 1000:
                        _NO_USAGE_WARNED.clear()
                    _NO_USAGE_WARNED.add(_tid)
                    logger.warning(
                        "[ledger] 模型 %s 未回报 usage——ledger 按估算结算（input=预留估/"
                        "output=响应文本估），usage_tracker 此类调用记 0，两账本将分叉"
                        "（本任务仅提示一次）", self.model_name)
                ledger.settle_error(rid, chunk_in=0, chunk_out=_out_est)
                ledger.unregister_inflight_rid(rid)
                return
            ledger.settle(rid, real_in=real_in, real_out=real_out)
            ledger.unregister_inflight_rid(rid)
        except Exception as exc:  # noqa: BLE001
            # 复核 H6：结算失败必须留痕——预留将挂到 TTL 过期/detach 才释放，期间余额虚低。
            logger.warning("[ledger] on_llm_end 结算失败（预留挂到 TTL/detach 才释放）: %s", exc)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        try:
            run_id = kwargs.get("run_id")
            rid = self._rids.pop(run_id, None)
            tracked = self._usage.pop(run_id, None) or [0, 0]
            if rid is None:
                return
            from swarm.models import ledger
            # B4 治本：中止/超时/掐流按已收 chunk 估算结算（input 取 max(chunk,预留)宁可高估）
            ledger.settle_error(rid, chunk_in=tracked[0], chunk_out=tracked[1])
            ledger.unregister_inflight_rid(rid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ledger] on_llm_error 结算失败（预留挂到 TTL/detach 才释放）: %s", exc)


_NO_USAGE_WARNED: set = set()  # 复核 H4：无 usage 网关的每任务一次告警（有界防涨）


def _estimate_output_tokens(response: Any) -> int:
    """无 usage 回报时按响应文本长度估算 output token（与预留共用 CJK 感知口径）。"""
    try:
        from swarm.models import ledger
        n = 0
        for gens in (getattr(response, "generations", None) or []):
            for g in gens:
                n += ledger.estimate_tokens_text(getattr(g, "text", "") or "")
        return n
    except Exception:  # noqa: BLE001
        return 0


def _extract_token_usage(response: Any) -> tuple[int, int]:
    """从 LLMResult 取 (prompt_tokens, completion_tokens)：非流式走 llm_output.token_usage，
    流式(stream_usage=True)走 generations[..].message.usage_metadata。"""
    try:
        tu = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        if tu:
            return int(tu.get("prompt_tokens", 0) or 0), int(tu.get("completion_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        for gens in (getattr(response, "generations", None) or []):
            for g in gens:
                um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                if um:
                    return int(um.get("input_tokens", 0) or 0), int(um.get("output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


# B6：provider 并发槽位池。asyncio.Semaphore 绑定事件循环，按 (provider_id, loop_id,
# limit) 键控（测试/多循环环境各自独立；单进程 uvicorn 单循环=天然全局闸；键含 limit
# ——阶段2 复核 F-D：并发上限热变更须拿到新池，不得被最先建的旧池静默吞，与 D54 缓存键
# 含 concurrency 的意图对齐）。有界：循环销毁/旧 limit 池残留无害（不再被取用），超 64
# 条清理一次。
import asyncio as _b6_asyncio
import contextvars as _b6_cv

_PROVIDER_SLOTS: dict[tuple[str, int, int], "_b6_asyncio.Semaphore"] = {}
_PROVIDER_SLOTS_LOCK = threading.Lock()

# 阶段2 复核 F-B：本次流式调用的槽位等待观测（contextvar——async 生成器体在消费者任务
# 上下文中执行，消费侧 _invoke_llm_abortable 可据此区分"整段超时耗在排队（进程内自致
# 拥塞）"与"provider 真失败"，前者绝不喂熔断。None=本调用无槽位参与（本地/无闸/桩）。
_SLOT_WAIT_CV: "_b6_cv.ContextVar[dict | None]" = _b6_cv.ContextVar(
    "swarm_slot_wait", default=None)


def slot_wait_reset() -> None:
    """新一次流式调用开始前清上一轮观测（消费侧 _stream_once 调）。"""
    _SLOT_WAIT_CV.set(None)


# G2-1（主题G·归因面，#9-B）：当前 LLM 调用的 brain 节点/阶段标签——[stream] 心跳/收尾/
# 排队日志此前只记 model_name，看门狗一行分不清是 plan_batch 还是 validate_plan 在长流/
# runaway（用户三次点名"节点不清"）。contextvar 挂在 _invoke_llm_abortable 入口，随 astream
# 生成器同任务上下文传播到 _astream_inner 的日志点，零参数穿透。
_LLM_NODE_CV: "_b6_cv.ContextVar[str]" = _b6_cv.ContextVar("swarm_llm_node", default="")


def set_llm_node(label: str) -> object:
    """绑定当前 LLM 调用的节点/阶段标签（返回 token 供 reset_llm_node 还原）。"""
    return _LLM_NODE_CV.set(label or "")


def reset_llm_node(token: object) -> None:
    try:
        _LLM_NODE_CV.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError, TypeError):  # 跨任务/已重置/非 Token：忽略（观测面绝不抛）
        pass


def _llm_node_tag() -> str:
    """归因前缀：'[plan_batch] ' 或 ''（未绑定）。供 [stream] 日志拼接。"""
    n = _LLM_NODE_CV.get()
    return f"[{n}] " if n else ""


def slot_wait_state() -> "dict | None":
    """{'queued_at': t, 'acquired_at': t|None}；acquired_at is None=仍在排队中被杀。"""
    return _SLOT_WAIT_CV.get()


def _slot_wait_cap_s() -> float:
    """槽位等待上界（泄漏保险丝）：超界 WARNING + fail-open 放行。0=不设界。
    释放依赖生成器 finalize（GC 钩子异步发生，非同步保证）——泄漏时此界让系统
    可观测降级而非静默越锁越死。"""
    import os as _os
    try:
        v = float(_os.environ.get("SWARM_PROVIDER_SLOT_WAIT_S", "600") or "600")
        return max(0.0, v)
    except ValueError:
        return 600.0


def _provider_slot(provider_id: str, limit: int):
    if not provider_id or limit <= 0:
        return None
    try:
        loop = _b6_asyncio.get_running_loop()
    except RuntimeError:
        return None
    key = (provider_id, id(loop), int(limit))
    with _PROVIDER_SLOTS_LOCK:
        sem = _PROVIDER_SLOTS.get(key)
        if sem is None:
            if len(_PROVIDER_SLOTS) > 64:
                _PROVIDER_SLOTS.clear()
            sem = _b6_asyncio.Semaphore(limit)
            _PROVIDER_SLOTS[key] = sem
        return sem


def _resolve_provider_concurrency(provider) -> int:
    explicit = getattr(provider, "max_concurrency", None)
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            return 0
    if getattr(provider, "kind", "") != "cloud":
        return 0
    import os as _os
    try:
        return max(0, int(_os.environ.get(
            "SWARM_CLOUD_PROVIDER_MAX_CONCURRENCY", "6") or "6"))
    except ValueError:
        return 6


@runtime_checkable
class ModelProvider(Protocol):
    """模型提供者协议"""
    def get_chat_model(self, model_name: str, temperature: float = 0.2) -> BaseChatModel: ...


class _DualTimeoutChatOpenAI(ChatOpenAI):
    """治本 A：流式【双超时拆分】——首 token 与解码间隔本质不同，不该共用一个阈值。

    - 首 token（含 prefill）：并发 + 大上下文下本就慢，给宽（swarm_first_token_timeout，默认 180s）；
    - 解码中途两 chunk 间隔：本该快，真停 >swarm_inter_chunk_timeout（默认 30s）就是异常，给紧。
    仅覆盖 async `_astream`（worker 热路径）；sync 路径沿用 langchain 内置 stream_chunk_timeout 兜底。
    超时抛 TransientInfraError（含 "timeout" 标记）→ classify_failure 归 transient（退避重试/fallback，
    【绝不】当 capability 去换模型——是基建瞬时，不是模型弱，对齐治本 C）。

    治本（可观测）：双超时只保证"没 stall"，不报"还在跑多久"。brain 调用可吐满 brain_max_tokens
    (32768)，云端 reasoning 模型按 ~20tok/s 算要数十分钟（实测 contract_design 单次 24.5min，全程
    零日志）——一个【健康的长流式】与【真挂死】在日志上无法区分，运维只能空等/误判。这里加【自静默
    心跳】：调用未超 heartbeat_after 秒一律不打（短的 worker 热路径零噪声），超了才每 heartbeat_every
    秒记一行 elapsed+chunk 数，证明"流式仍在吐、未 stall"。不改超时语义，纯观测。

    治本（超时第三条腿·总时长 wall-clock）：双超时管【两 chunk 间隔】、max_tokens 管【输出长度】，
    但都管不住【稳定吐、却吐不完】的 runaway——实测 GLM-5.2 contract_design 稳定吐 6w+ chunk / 22min
    后才 stall 失败、failover，前 22min 全是空烧（半成品在 ainvoke 失败时整段作废、不落盘）。max_tokens
    没拦住是因为它只封最终答案、reasoning_content 豁免（或 chunk 亚 token 未数到上限）。这里加【总时长
    看门狗】：单次流式累计超 swarm_wallclock_budget 秒即抛 TransientInfraError（同 stall 归 transient →
    退避/fallback，绝不当 capability 换模型，对齐 C）。0=关闭（worker 热路径默认关，已有 stall+max_tokens
    兜底）；brain 调用开（封顶 runaway，让它早 fail-fast 切 fallback，而非空烧到自然 stall）。
    """

    swarm_first_token_timeout: float = 180.0
    swarm_inter_chunk_timeout: float = 30.0
    # 自静默心跳：调用总时长超过 after 秒才开始打心跳，之后每 every 秒一行（短调用零噪声）。
    swarm_heartbeat_after: float = 60.0
    swarm_heartbeat_every: float = 30.0
    # 总时长看门狗：单次流式累计超此秒数即判 runaway 抛 transient。0=关闭（默认，worker 热路径不动）。
    swarm_wallclock_budget: float = 0.0
    # R56-1：本实例是否已是链尾（无备选可切）。链尾遇 reasoning runaway 只能"关 thinking 重开"
    # （质量降级已知），非链尾则抛 transient 让 fallback 切到备用大脑（保留完整推理）。
    swarm_no_fallback: bool = False
    # R55-1：**思考阶段**预算（秒）。reasoning 模型在思维链里原地打转时，max_tokens 封不住
    # （只封最终答案）、stall 看门狗看不出（chunk 一直在吐）、墙钟兜底要先烧满 25 分钟。
    # 关键洞察：**正文尚未吐出一个字之前**中途 abort 是无损的（下游一个 chunk 都没收到）→
    # 超预算即就地关 thinking、用同一模型重开流，下游无感。0=关闭。
    swarm_reasoning_phase_budget: float = 0.0
    # B6（阶段2.4，登记册 §三）：provider 级进程并发闸。每任务 Semaphore(4)×多任务×
    # tech_design(3)×contract 并发可达十几路，系统自己把云端打饱和（饱和→超时→重试→更
    # 饱和自激振荡）。同 provider 的所有流式调用共享一个进程级槽位池，持有整个流期间。
    # 0=不闸（本地默认：只有时间成本，vLLM 自身排队）。
    swarm_provider_id: str = ""
    swarm_provider_concurrency: int = 0

    async def _astream(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        # Task#10：模型层 LLM 录制钩（env 门控 + 仅 brain）。默认关（未设
        # SWARM_CASSETTE_RECORD_DIR）时零开销直通；开启且本调用带 brain 节点标签
        # （_LLM_NODE_CV，仅 brain 图节点绑定，见 set_llm_node）才录——自动排除高频
        # worker 本地流量。tee_record 全程 fail-open 且忠实透传 GeneratorExit/取消，
        # 既有 aclose→GPU abort 链不受影响（见 cassette_record 铁律）。
        from contextlib import aclosing

        from swarm.models import cassette_record as _cass
        if _cass.recording_enabled() and _LLM_NODE_CV.get():
            # 复核 F1：aclosing 把消费者的 aclose 确定性转发给 tee_record（它再转发给
            # _astream_slotted，触发其 B6 槽位 finally 释放 + cassette _flush 落盘）——录制路径
            # 不给既有释放链新增 GC 悬挂 hop。底层 _astream_inner→super 的既有 GC 终结属 Task#10
            # 之外的历史行为，未改动（真正的 GPU abort 走 _astream_inner 的超时/取消分支，确定）。
            async with aclosing(_cass.tee_record(
                self._astream_slotted(*args, **kwargs),
                node=_LLM_NODE_CV.get(),
                model=str(getattr(self, "model_name", "") or ""),
                provider=self.swarm_provider_id or "",
                args=args, kwargs=kwargs,
            )) as _rec:
                async for _chunk in _rec:
                    yield _chunk
            return
        async for _chunk in self._astream_slotted(*args, **kwargs):
            yield _chunk

    async def _astream_slotted(self, *args: Any, **kwargs: Any):
        # B6 provider 级并发闸 + 双超时内层（原 _astream 主体，录制钩抽离后不变语义）。
        _sem = _provider_slot(self.swarm_provider_id, self.swarm_provider_concurrency)
        if _sem is None:
            async for chunk in self._astream_inner(*args, **kwargs):
                yield chunk
            return
        # 阶段2 复核 F-B：①排队观测进 contextvar（消费侧据此免把自致拥塞喂熔断）；
        # ②acquire 有界（SWARM_PROVIDER_SLOT_WAIT_S，泄漏保险丝）——释放依赖生成器
        # finalize（外层 astream 被弃时经 GC 钩子异步发生，无同步保证），槽位被占死时
        # 无界 acquire=静默越锁越死零日志；超界 WARNING + fail-open 放行（并发闸是抗
        # 自饱和的优化而非正确性闸，与"无备不熔"同一 fail-open 对称原则）。
        _wait = _SLOT_WAIT_CV.get()
        if _wait is None:
            _wait = {"queued_at": _monotonic(), "acquired_at": None}
            _SLOT_WAIT_CV.set(_wait)
        else:  # 消费侧已 reset 过则复用；异常残留也在此被覆盖
            _wait["queued_at"] = _monotonic()
            _wait["acquired_at"] = None
        _cap = _slot_wait_cap_s()
        _acquired = False
        try:
            if _cap > 0:
                try:
                    await _b6_asyncio.wait_for(_sem.acquire(), timeout=_cap)
                    _acquired = True
                except _b6_asyncio.TimeoutError:
                    logger.warning(
                        "[router] B6 provider '%s' 槽位等待超 %.0fs——fail-open 本次不闸"
                        "放行（若持续出现：疑槽位泄漏或并发上限过低，检查 "
                        "SWARM_CLOUD_PROVIDER_MAX_CONCURRENCY/持有流是否挂死）",
                        self.swarm_provider_id, _cap)
            else:
                await _sem.acquire()
                _acquired = True
            _wait["acquired_at"] = _monotonic()
            _q_s = _wait["acquired_at"] - _wait["queued_at"]
            if _q_s >= 30.0:
                logger.info("[router] B6 %sprovider '%s' 槽位排队 %.0fs 后获得（并发闸生效中）",
                            _llm_node_tag(), self.swarm_provider_id, _q_s)
            async for chunk in self._astream_inner(*args, **kwargs):
                yield chunk
        finally:
            if _acquired:
                _sem.release()

    async def _astream_inner(self, *args: Any, **kwargs: Any):
        import asyncio

        from swarm.models.errors import TransientInfraError

        agen = super()._astream(*args, **kwargs)
        first = True
        t0 = _monotonic()
        last_beat = t0
        n_chunks = 0
        last_chunk: Any = None
        content_seen = False    # R55-1：是否已吐出正文（未吐 → 仍在思考阶段，abort 无损）
        thinking_off = False    # R55-1：已就地降级过一次（绝不无限重开）
        while True:
            to = self.swarm_first_token_timeout if first else self.swarm_inter_chunk_timeout
            try:
                chunk = await asyncio.wait_for(agen.__anext__(), timeout=to)
            except StopAsyncIteration:
                # 干净收尾：长调用记 elapsed+chunk 数+finish_reason。finish_reason=="length" 证明撞了
                # max_tokens（output 计 token）；=="stop" 是自然收束——用于坐实 runaway 时 max_tokens 为何没拦。
                elapsed = _monotonic() - t0
                if elapsed >= self.swarm_heartbeat_after:
                    fr = "?"
                    try:
                        meta = getattr(getattr(last_chunk, "message", None), "response_metadata", None) or \
                            getattr(last_chunk, "generation_info", None) or {}
                        fr = meta.get("finish_reason") or meta.get("stop_reason") or "?"
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(
                        "[stream] %s%s 流式完成 %.0fs（共 %d chunk，finish_reason=%s）",
                        _llm_node_tag(), getattr(self, "model_name", None) or "model",
                        elapsed, n_chunks, fr,
                    )
                return
            except asyncio.TimeoutError as exc:
                phase = "首 token(prefill)" if first else "解码中途"
                try:
                    await agen.aclose()  # 关底层流，让推理端 abort 解码、释放 GPU
                except Exception:  # noqa: BLE001
                    pass
                raise TransientInfraError(
                    f"stream {phase} 超时 {to:.0f}s (stream stall timeout) —— 基建瞬时，退避重试/fallback"
                ) from exc
            except asyncio.CancelledError as _cexc:
                # B1 治本(P0)：cancel_task→handle.cancel() 在此 __anext__ await 上抛 CancelledError。
                # 旧代码无此分支 → agen 不关 → 底层 HTTP 流不断 → vLLM/Ollama 继续解码占 GPU 空烧。
                # 与 TimeoutError 同处理：关底层流让推理端 abort 解码释放 GPU，再上抛（不吞取消语义）。
                try:
                    await agen.aclose()
                except Exception:  # noqa: BLE001
                    pass
                # 复核 F1 辅修（阶段1）：ainvoke(streaming=True) 内部走本生成器，但被取消时
                # langchain 的 agenerate 不会触发 on_llm_error（gather 取消直接穿透，实测
                # langchain-core 1.4.0）→ _LedgerGuard 预留泄漏。此处主动补发（幂等：guard
                # 按 rid pop 一次；langchain 若后续再发则二次调用为 no-op）。TTL 兜底其余管道。
                _rm = kwargs.get("run_manager")
                if _rm is not None:
                    try:
                        await _rm.on_llm_error(_cexc, response=None)
                    except Exception:  # noqa: BLE001 — 补发失败不掩盖取消语义
                        pass
                raise
            first = False
            n_chunks += 1
            now = _monotonic()
            if not content_seen and str(getattr(chunk, "content", "") or ""):
                content_seen = True    # 正文已开吐 → 思考阶段结束，此后 abort 不再无损
            # R55-1（round55 实锤）：**思考阶段**预算——正文一个字都还没吐、思维链却已烧穿预算
            # → 模型在原地打转（实测 EXTRACT_REQ：1471s / 79605 chunk，前 400+ chunk 无一正文）。
            # 此刻 abort 是【无损】的（下游没收到任何 chunk）→ 就地关 thinking、用**同一模型**重开流：
            # 几十秒拿到正文，而不是烧满墙钟预算再切备模型从头重跑（后者丢掉 25 分钟且换模型）。
            # 只降级一次（_thinking_off 置位）：再失控就交给墙钟/stall 兜底，绝不无限重开。
            if (self.swarm_reasoning_phase_budget > 0 and not content_seen
                    and not thinking_off and now - t0 >= self.swarm_reasoning_phase_budget):
                try:
                    await agen.aclose()   # 关底层流，让推理端 abort 解码、释放算力
                except Exception:  # noqa: BLE001
                    pass
                # R56-1（round56 实锤，修正 R55-1）：失控时**不降级思考，先换模型**。
                # round56 实测：关 thinking 后同一模型确实产出了、流程也救活了，但**质量掉了**——
                # 同一份 PRD，有推理抽出 106 条需求、关推理只抽出 92 条，且**整块功能消失**
                # （AES/SHA512 加密、企微 Bot/Lark/VoIP 渠道、排班快照的 5 个数据字段、3 个 API）。
                # 更坏的是覆盖闸**看不见**这种损失（它只校验"抽出来的都被覆盖了"）。
                # 正确顺序：①有备选模型 → 抛 transient，让 fallback 链切到备用大脑（**保留完整推理**，
                # 不牺牲质量；此刻下游尚未收到任何 chunk，切换同样无损）；②无备选可切（swarm_no_fallback）
                # → 才退到"关 thinking 同模型重开"（有产出胜过烧穿墙钟，但质量降级已知）。
                if not self.swarm_no_fallback:
                    logger.warning(
                        "[stream] %s%s R56-1 思考阶段已 %.0fs（%d chunk）仍未吐出任何正文 → 判 reasoning "
                        "runaway：抛 transient 切【备用大脑】（保留完整推理，不牺牲质量；下游尚未收到任何 "
                        "chunk，切换无损）",
                        _llm_node_tag(), getattr(self, "model_name", None) or "model",
                        now - t0, n_chunks,
                    )
                    raise TransientInfraError(
                        # 首行含 "timeout" 供 _breaker_error_transient 关键词判定（与墙钟同口径）
                        f"reasoning runaway timeout: 思考阶段 {now - t0:.0f}s（{n_chunks} chunk）仍未吐出"
                        f"正文 —— 基建瞬时，切备用模型（保留推理）"
                    )
                logger.warning(
                    "[stream] %s%s R56-1 思考阶段已 %.0fs（%d chunk）仍未吐出任何正文，且**无备选模型可切** "
                    "→ 退到关闭 thinking 用同一模型重开流（有产出胜过烧穿墙钟；**质量降级已知**：实测会漏需求）",
                    _llm_node_tag(), getattr(self, "model_name", None) or "model", now - t0, n_chunks,
                )
                _eb = dict(kwargs.get("extra_body") or {})
                _eb["thinking"] = {"type": "disabled"}
                kwargs["extra_body"] = _eb
                agen = super()._astream(*args, **kwargs)
                thinking_off = True
                first = True          # 重开流：首 token 重新按 prefill 超时计
                t0 = _monotonic()     # 墙钟/心跳按新流重算（旧流成果已丢弃，不该继续计入）
                last_beat = t0
                n_chunks = 0
                continue
            # 总时长看门狗（第三条腿）：稳定吐但吐不完的 runaway，stall 看门狗与 max_tokens 都拦不住，
            # 累计超预算即判定 runaway → 抛 transient（早 fail-fast 切 fallback，不空烧到自然 stall）。
            if self.swarm_wallclock_budget > 0 and now - t0 >= self.swarm_wallclock_budget:
                try:
                    await agen.aclose()  # 关底层流，让推理端 abort 解码、释放 GPU
                except Exception:  # noqa: BLE001
                    pass
                raise TransientInfraError(
                    # 复核 P-1：首行含 "timeout" 供 _breaker_error_transient 关键词判定
                    # ——否则 F-F 健康重排学不到 runaway，惯性超时的 primary 永远排链头
                    f"stream wallclock timeout: 总时长 {now - t0:.0f}s 超预算 "
                    f"{self.swarm_wallclock_budget:.0f}s (runaway，已收 {n_chunks} chunk "
                    f"仍未收尾) —— 基建瞬时，退避/fallback"
                )
            # 自静默心跳：超过 after 且距上次心跳≥every 才记一行，证明长调用仍在吐 token（非挂死）。
            if now - t0 >= self.swarm_heartbeat_after and now - last_beat >= self.swarm_heartbeat_every:
                last_beat = now
                logger.info(
                    "[stream] %s%s 流式生成中 %.0fs（已收 %d chunk，未 stall）",
                    _llm_node_tag(), getattr(self, "model_name", None) or "model",
                    now - t0, n_chunks,
                )
            last_chunk = chunk
            yield chunk


# ── D54：ChatModel 实例缓存 ─────────────────────────────────────────────
# 旧行为：每次 get_chat_model 都 new 一个 ChatOpenAI（内含新 httpx 连接池）——brain/worker
# 每次 LLM 调用都重建客户端、重做 TLS 握手、连接池零复用。改为按【全部影响行为的参数】
# 值缓存实例（langchain ChatModel 无每调用可变状态，跨并发调用共享安全；_UsageRecorder /
# ModelInvocationLogger 均按 run_id 或无状态设计）。
# 失效语义：缓存键直接由 base_url/api_key/超时/温度/model 等【值】构成——PUT /api/routing
# 等热更改了任何行为参数，键即不同、自然取到新实例（值键化 = 语义级失效，无需 reload 钩子）。
# fail-closed：callbacks 含无法值指纹化的外部回调 → 跳过缓存走原路径（绝不错共享）。
_CHAT_MODEL_CACHE: "dict[tuple, BaseChatModel]" = {}
_CHAT_MODEL_CACHE_LOCK = threading.Lock()
_CHAT_MODEL_CACHE_MAX = 64  # 防配置频繁变更下无界增长；超限整体清空重建


def clear_chat_model_cache() -> None:
    """清空 ChatModel 实例缓存（运维/测试钩子；正常热更靠值键化自然失效）。"""
    with _CHAT_MODEL_CACHE_LOCK:
        _CHAT_MODEL_CACHE.clear()


def _callbacks_cache_token(callbacks: list | None) -> "tuple | None":
    """把 callbacks 列表转为值指纹；含未知回调类型 → None（该次调用不缓存）。

    仓内两类回调都是按构造参数确定行为：ModelInvocationLogger（无状态）与内部
    _UsageRecorder（get_chat_model 内部追加，不经此处）。外部/测试注入的任意回调
    无法值指纹化，宁可不缓存也不错共享。
    """
    tokens: list[tuple] = []
    for cb in callbacks or []:
        if isinstance(cb, ModelInvocationLogger):
            tokens.append(("MIL", cb.role, cb.model_name, cb.provider_id))
        else:
            return None
    return tuple(tokens)


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
        self, model_name: str, temperature: float = 0.2, callbacks: list | None = None,
        max_tokens: int | None = None, wallclock_budget: float | None = None,
        no_fallback: bool = False,
    ) -> BaseChatModel:
        # 本地推理服务常无需 key；空则用占位（vLLM/Ollama 网关忽略）。
        api_key: str = self.provider.api_key or "EMPTY"  # type: ignore[assignment]
        # D54：值键化实例缓存——键覆盖全部影响行为的参数（含超时族/重试/kind 推导的
        # extra_body 分支/回调指纹）。指纹化失败（外部回调）→ _cache_key=None 走原路径。
        _cb_token = _callbacks_cache_token(callbacks)
        _cache_key: tuple | None = None
        if _cb_token is not None:
            _cache_key = (
                self.provider.id, self.provider.kind, self.provider.base_url, api_key,
                self._resolve_retries(), model_name, float(temperature),
                int(max_tokens or 0), float(wallclock_budget or 0.0),
                float(getattr(self.config, "brain_reasoning_phase_budget_s", 0.0) or 0.0),
                bool(no_fallback),
                float(self.config.timeout_seconds),
                float(getattr(self.config, "first_token_timeout", self.config.stream_chunk_timeout)),
                float(getattr(self.config, "inter_chunk_timeout", 30.0)),
                _resolve_provider_concurrency(self.provider),  # B6：并发闸参数入缓存键
                _cb_token,
            )
            with _CHAT_MODEL_CACHE_LOCK:
                cached = _CHAT_MODEL_CACHE.get(_cache_key)
            if cached is not None:
                return cached
        # token 用量统计：在唯一 chokepoint 挂一个 _UsageRecorder（知 kind/provider/model），
        # 覆盖全部 cloud+local、brain+worker、primary+fallback 路径且只挂一处=不重复计数。
        _cbs = list(callbacks or [])
        _cbs.append(_UsageRecorder(self.provider.kind, self.provider.id, model_name))
        # §九 阶段1.3：TaskLedger 单点闸——同一 chokepoint 挂一次，预留-结算全路径覆盖。
        _cbs.append(_LedgerGuard(self.provider.kind, model_name, max_tokens))
        _kwargs: dict = dict(
            model=model_name,
            base_url=self.provider.base_url,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=temperature,
            timeout=self.config.timeout_seconds,
            max_retries=self._resolve_retries(),
            callbacks=_cbs,
            # streaming=True：取消/断连时 httpx 关闭流式连接，推理服务端(vLLM)
            # 检测到 client disconnect 即 abort 解码，释放 GPU；非流式则会跑完整段。
            streaming=True,
            # 计费级 token 统计：流式默认不回 usage，必须显式要 stream_options.include_usage，
            # 否则 on_llm_end 拿不到 prompt/completion tokens（统计全 0）。
            stream_usage=True,
            # langchain 内置单值看门狗：设为【宽】的 first_token_timeout，作为 sync 路径 + 兜底上限；
            # async 热路径的【紧】解码间隔由 _DualTimeoutChatOpenAI._astream 另行把关（治本 A）。
            stream_chunk_timeout=getattr(
                self.config, "first_token_timeout", self.config.stream_chunk_timeout),
        )
        # 输出 token 上限（仅 worker 路径传入；brain 规划需长输出故不限）。
        if max_tokens and max_tokens > 0:
            _kwargs["max_tokens"] = max_tokens
        # ── 关闭本地推理模型的 reasoning/think 块（task 94334785 根因）──
        # 本地 Qwen 系 reasoning 模型(如 Qwopus3.6-27B-v2 / ThinkingCap-Qwen3.6-27B)默认输出 <think>...</think>
        # 推理块，但经 vLLM chat template 后【开头 <think> 被吃掉、内容全进 think、think 外的
        # 真实答案为空】→ worker agent 拿到空回复 → 反复要求 → "Sorry, need more steps" 拒答
        # (实证：st-1 30s 空转拒答，未调任何工具)。worker 执行不需要 reasoning(要直接调工具
        # 干活)，故对【本地 provider】统一关 thinking；云端 provider(brain 规划用 GLM-5.1)不动，
        # 保留其 reasoning 能力。vLLM/Qwen 通过 chat_template_kwargs.enable_thinking 控制。
        if self.provider.kind == "local":
            _kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        # 治本 A：双超时拆分（首 token 宽 / 解码间隔紧）。读 config，缺省回退安全值。
        _kwargs["swarm_first_token_timeout"] = getattr(self.config, "first_token_timeout", 180.0)
        _kwargs["swarm_inter_chunk_timeout"] = getattr(self.config, "inter_chunk_timeout", 30.0)
        # 治本（第三条腿）：总时长看门狗。由调用方按角色传入（brain 开/worker 默认关），0=关闭。
        _kwargs["swarm_wallclock_budget"] = float(wallclock_budget or 0.0)
        # R55-1：思考阶段预算——只对【云端 reasoning 模型】有意义（本地 provider 上面已直接关
        # thinking）。开了墙钟兜底（即 brain 角色）才启用：两者是同一条腿的粗细两级——先用无损的
        # "关 thinking 重开流"抢救，抢救不回再由墙钟切备模型。
        _kwargs["swarm_no_fallback"] = bool(no_fallback)
        _kwargs["swarm_reasoning_phase_budget"] = (
            float(getattr(self.config, "brain_reasoning_phase_budget_s", 0.0) or 0.0)
            if (wallclock_budget or 0) > 0 and self.provider.kind != "local" else 0.0
        )
        # B6：provider 并发闸参数——provider 显式 max_concurrency 优先；云端缺省走
        # SWARM_CLOUD_PROVIDER_MAX_CONCURRENCY（默认 6）；本地缺省 0=不闸（时间成本口径）。
        _kwargs["swarm_provider_id"] = self.provider.id
        _kwargs["swarm_provider_concurrency"] = _resolve_provider_concurrency(self.provider)
        model = _DualTimeoutChatOpenAI(**_kwargs)
        if _cache_key is not None:
            with _CHAT_MODEL_CACHE_LOCK:
                if len(_CHAT_MODEL_CACHE) >= _CHAT_MODEL_CACHE_MAX:
                    _CHAT_MODEL_CACHE.clear()  # 罕见（配置频繁变更）；清空重建防无界增长
                _CHAT_MODEL_CACHE[_cache_key] = model
        return model


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

    _reachability_validated = False  # 类级：每进程只做一次路由可达性校验（避免多次实例化刷屏）

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or get_config().model
        # 按 provider.id 缓存 EndpointProvider 实例
        self._providers: dict[str, EndpointProvider] = {
            p.id: EndpointProvider(p, self.config)
            for p in self.config._effective_providers()
        }
        # TD2606-A8：启动期路由可达性校验（每进程一次）。死模型/拼错名不再只在【调用时】
        # 静默合成 local 兜底 + 埋日志 warning，而是在此显式列出；整条链全不可达 → ERROR。
        if not ModelRouter._reachability_validated:
            ModelRouter._reachability_validated = True
            try:
                self.validate_routing_reachability()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ROUTER] 路由可达性校验跳过(非致命): %s", exc)

    def validate_routing_reachability(self) -> list[dict]:
        """交叉校验每个路由档(primary + fallback 链)是否可达，返回不可达条目（供启动/健康检查）。

        可达判据（离线、确定性）：
          - 能力库(capability_store)【已探测】(非空) → 以探测结果为准：模型名在探测集内才算可达
            （provider 映射不证明模型真的在端点上——本地启发式会把任意名映射到 local 端点，
            故映射存在 ≠ 可达；探测过的清单才是事实源）。
          - 能力库为空（从未探测）→ 无法离线判定 → 退化到"有 provider 映射即假定可达"，不误报。
        整条链(primary+所有 fallback)均不可达 → ERROR（该难度档请求必失败）；部分不可达 → WARNING。
        TD2606-A8。
        """
        cfg = self.config
        try:
            from swarm.models.capability_store import list_capabilities
            known = {c.get("model_id") for c in (list_capabilities() or []) if c.get("model_id")}
        except Exception as exc:  # noqa: BLE001 — 能力库不可用时退化为"不校验"，不阻断
            logger.debug("[ROUTER] 能力库读取失败，跳过可达性校验: %s", exc)
            return []

        def _reachable(name: str) -> bool:
            if not name:
                return False
            prov = cfg.provider_for_model(name)
            # P1（治本，996db614 实测 GLM-5.2 误报"不可达"）：capability_store 是【本地模型】探测库
            # （只有本地模型被探测进去）；云端模型经 provider 显式映射/启发式解析到【真实云端点】，
            # 不进探测集却确实可达（实测 GLM-5.2 流式 79.9s 成功）。故云端模型【有云 provider 映射
            # 即可达】，不据本地探测库误报。
            if prov is not None and getattr(prov, "kind", "") == "cloud":
                return True
            if known:
                # 能力库已探测 → 本地模型以探测为准（本地映射存在 ≠ 模型真的烤进镜像）。
                return name in known
            # 能力库为空（从未探测）→ 退化到"有 provider 映射即假定可达"，不离线误报。
            return prov is not None

        tiers = [
            ("trivial", cfg.routing_trivial, list(cfg.routing_trivial_fallback or [])),
            ("medium", cfg.routing_medium, list(cfg.routing_medium_fallback or [])),
            ("complex", cfg.routing_complex, list(cfg.routing_complex_fallback or [])),
            ("multimodal", cfg.routing_multimodal, list(cfg.routing_multimodal_fallback or [])),
            ("brain", cfg.brain_primary, [cfg.brain_fallback]),
        ]
        issues: list[dict] = []
        for tier, primary, fbs in tiers:
            chain = [n for n in [primary, *fbs] if n]
            if not chain:
                continue
            unreachable = [n for n in chain if not _reachable(n)]
            if len(unreachable) == len(chain):
                logger.error(
                    "[ROUTER] 路由档 '%s' 整条链(primary+fallback)均不可达: %s —— 该难度请求将必失败，"
                    "请检查模型名拼写 / model_providers 映射 / 是否已探测上线。", tier, chain)
                issues.append({"tier": tier, "severity": "error",
                               "kind": "whole_chain_unreachable", "chain": chain})
            elif unreachable:
                logger.warning(
                    "[ROUTER] 路由档 '%s' 含不可达模型 %s（链上仍有可达兜底，建议核对名称/映射）。",
                    tier, unreachable)
                issues.append({"tier": tier, "severity": "warning",
                               "kind": "partial_unreachable", "unreachable": unreachable})
        return issues

    def get_brain_llm(self) -> BaseChatModel:
        """Brain 编排层 — 必须大模型（云端优先，本地 fallback）。

        FINDING-10：brain 调用传 max_tokens 上限（防 reasoning 模型失控持续生成把 PLAN/规划
        无限挂死）。0 表示不限（向后兼容）。max_tokens 只封最终答案 token、拦不住 reasoning runaway
        （实测 GLM-5.2 稳定吐 6w+ chunk/22min 才 stall），故再叠【总时长看门狗】wallclock 兜底。
        """
        _bmt = getattr(self.config, "brain_max_tokens", 0) or None
        _wc = getattr(self.config, "brain_stream_wallclock_s", 0.0)
        primary = self._get_provider_for_model(self.config.brain_primary).get_chat_model(
            self.config.brain_primary, temperature=self.config.brain_temperature,
            max_tokens=_bmt, wallclock_budget=_wc,
        )
        fallback = self._get_provider_for_model(self.config.brain_fallback).get_chat_model(
            self.config.brain_fallback, temperature=self.config.brain_temperature,
            max_tokens=_bmt, wallclock_budget=_wc,
            no_fallback=True,   # R56-1：链尾——它再失控就没人可切了，只能关 thinking 保产出
        )
        return primary.with_fallbacks([fallback])

    def get_brain_fallback_llm(self) -> BaseChatModel:
        """R35-A：Brain 备用模型（brain_fallback，默认 Kimi）单独取用——供调用方在【外层
        墙钟超时】后【显式切备】。get_brain_llm 的 with_fallbacks 仅在 primary 于流【内】抛
        异常时触发；而 _invoke_llm_abortable 的外层 wait_for 总超时在【消费者帧】抛
        asyncio.TimeoutError，绕过 with_fallbacks（round35 实证：SiliconFlow 饱和时 GLM-5.2
        稳定慢产 >300s→外层墙钟掐断→不切 Kimi→同模型空重试仍超时）。故备用模型须单独暴露，
        由调用方在外层超时后主动切一次（备用 fresh 预算）。与 get_brain_llm 同构造（同看门狗）。"""
        _bmt = getattr(self.config, "brain_max_tokens", 0) or None
        _wc = getattr(self.config, "brain_stream_wallclock_s", 0.0)
        return self._get_provider_for_model(self.config.brain_fallback).get_chat_model(
            self.config.brain_fallback, temperature=self.config.brain_temperature,
            max_tokens=_bmt, wallclock_budget=_wc,
        )

    @staticmethod
    def _assemble_worker_chain(named: list[tuple[str, "Runnable"]]) -> "Runnable":
        """F-F（阶段4）：worker 面 fallback 链组装——接 breaker 健康信号。

        此前 worker 纯靠 with_fallbacks 顺序兜底：primary 反复超时仍每次先撞 primary
        （每次白付一整个超时窗才降级）。现按 breaker.is_open 只读探询把 open 中的模型
        排到链尾（不删除——全 open 时仍按序尝试，绝不无模型可用）；并给每个模型挂
        listeners 回灌成败证据（成功→record_success 复位；超时/连接类错误→record_failure，
        capability 错误不喂——与 breaker 契约一致）。brain 面的 allow() 探针语义不动。
        """
        from swarm.models import breaker as _breaker

        def _listened(name: str, llm: "Runnable") -> "Runnable":
            def _on_end(run_obj) -> None:
                try:
                    _breaker.record_success(name)
                except Exception:  # noqa: BLE001 — 记账绝不拖垮调用
                    pass

            def _on_error(run_obj) -> None:
                try:
                    if _breaker_error_transient(
                            str(getattr(run_obj, "error", "") or "")):
                        _breaker.record_failure(name)
                except Exception:  # noqa: BLE001
                    pass

            try:
                return llm.with_listeners(on_end=_on_end, on_error=_on_error)
            except Exception:  # noqa: BLE001 — 老版本 Runnable 无 with_listeners 时裸用
                return llm

        healthy = [(n, m) for n, m in named if not _breaker.is_open(n)]
        opened = [(n, m) for n, m in named if _breaker.is_open(n)]
        if opened:
            logger.warning(
                "[router] F-F worker 链健康重排：熔断中的模型 %s 移到链尾（健康优先 %s）",
                [n for n, _ in opened], [n for n, _ in healthy])
        ordered = (healthy + opened) or named
        # R56-1 补齐（worker 面）：**链尾**必须标 no_fallback——它再思考失控就没人可切了，
        # 此时只能就地关 thinking 保产出；不标的话它会抛 TransientInfraError 而无人接，
        # 整个 worker 调用直接失败（比 R55-1 之前更糟）。注意链序是 breaker 健康**重排后**的，
        # 所以只能在这里定链尾，不能在构造时静态定。
        for _i, (_n, _m) in enumerate(ordered):
            if hasattr(_m, "swarm_no_fallback"):
                try:
                    _m.swarm_no_fallback = (_i == len(ordered) - 1)
                except Exception:  # noqa: BLE001 —— 非 Dual 模型/不可写 → 保持默认
                    pass
        chained = [_listened(n, m) for n, m in ordered]
        if len(chained) > 1:
            return chained[0].with_fallbacks(chained[1:])
        return chained[0]

    def get_llm_for_subtask(self, difficulty: str, modality: str = "text") -> Runnable:
        """根据子任务难度和模态动态选择模型。

        fallback 为【多级兜底链】：primary 失败 → 链上第 1 个 → 第 2 个 … 逐级降级
        （全本地，主→次→兜底）。LangChain with_fallbacks 接受多个，按序尝试。
        """
        primary_name, fallback_names = self._resolve_route(difficulty, modality)
        # D10（round48c 实锤）：fallback 链含 primary 自身（配置漂移常态）→ primary
        # 挂时同一死模型必两连败才到真备选（42 次实测）。链内去重 primary+去自重。
        _seen = {primary_name}
        fallback_names = [
            fb for fb in fallback_names
            if fb and fb not in _seen and not _seen.add(fb)]

        role = f"worker/{difficulty}"
        _wmax = getattr(self.config, "worker_max_tokens", 0) or None
        # E2（round38c 主题E）：worker 单流总墙钟——超时抛 TransientInfraError，
        # with_fallbacks 链内即刻切备（治「单调用挂满 900s 总预算无本步换备」）
        _wwc = float(getattr(self.config, "worker_stream_wallclock_s", 0.0) or 0.0)
        p_prov = self._get_provider_for_model(primary_name)
        primary = p_prov.get_chat_model(
            primary_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, primary_name, p_prov.provider.id)],
            max_tokens=_wmax, wallclock_budget=_wwc,
        )
        fallback_llms = []
        for i, fb_name in enumerate(fallback_names):
            if not fb_name:
                continue
            f_prov = self._get_provider_for_model(fb_name)
            fallback_llms.append(f_prov.get_chat_model(
                fb_name,
                temperature=self.config.worker_temperature,
                callbacks=[ModelInvocationLogger(f"{role}/fallback{i + 1}", fb_name, f_prov.provider.id)],
                max_tokens=_wmax, wallclock_budget=_wwc,
            ))
        # F-F（阶段4）：接 breaker 健康重排 + 成败记账（见 _assemble_worker_chain）
        _named = [(primary_name, primary)] + [
            (n, m) for n, m in zip(
                [fb for fb in fallback_names if fb], fallback_llms)]
        return self._assemble_worker_chain(_named)

    def get_llm_by_name(self, model_name: str, difficulty: str = "medium") -> Runnable:
        """按指定模型名取 worker LLM（用于主力并行轮转 override），带该难度的 fallback 链兜底。

        worker_parallel_pool 轮转时用：把同难度子任务分到不同本地主力模型，
        但仍保留该难度的 fallback 链（override 模型挂了能降级），不牺牲健壮性。
        """
        role = f"worker/{difficulty}"
        _wmax = getattr(self.config, "worker_max_tokens", 0) or None
        # E2（round38c 主题E）：worker 单流总墙钟——超时抛 TransientInfraError，
        # with_fallbacks 链内即刻切备（治「单调用挂满 900s 总预算无本步换备」）
        _wwc = float(getattr(self.config, "worker_stream_wallclock_s", 0.0) or 0.0)
        p_prov = self._get_provider_for_model(model_name)
        primary = p_prov.get_chat_model(
            model_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, model_name, p_prov.provider.id)],
            max_tokens=_wmax, wallclock_budget=_wwc,
        )
        # 复用该难度的 fallback 链（排除掉 override 模型自己，避免重复）
        _, fallback_names = self._resolve_route(difficulty, "text")
        fallback_llms = []
        for i, fb_name in enumerate(fallback_names):
            if not fb_name or fb_name == model_name:
                continue
            f_prov = self._get_provider_for_model(fb_name)
            fallback_llms.append(f_prov.get_chat_model(
                fb_name,
                temperature=self.config.worker_temperature,
                callbacks=[ModelInvocationLogger(f"{role}/fallback{i + 1}", fb_name, f_prov.provider.id)],
                max_tokens=_wmax, wallclock_budget=_wwc,
            ))
        # F-F（阶段4）：同 get_llm_for_subtask——健康重排 + 记账
        _fb_named = [fb for fb in fallback_names if fb and fb != model_name]
        _named = [(model_name, primary)] + list(zip(_fb_named, fallback_llms))
        return self._assemble_worker_chain(_named)

    def _alternate_candidates(self, difficulty: str, modality: str = "text") -> list[str]:
        """E1+复核 C-4：alternate 候选=fallback 链中 ≠primary 且【非 trivial 档 primary】
        的模型（难度本身为 trivial 除外）。

        C-4（CONFIRMED）：三档 fallback 链统一以 trivial 档模型（如 ThinkingCap-27B）居首
        时，「第一个 ≠primary」会把 medium/complex 的失败重试派到最弱模型=RUN10 顾虑成真。
        排除 trivial 档 primary 后，medium 的 alternate 自然落到更强档，「换模型」不再
        意味着「降级」。"""
        primary_name, fallback_names = self._resolve_route(difficulty, modality)
        _trivial = (getattr(self.config, "routing_trivial", None)
                    if str(difficulty).lower() != "trivial" else None)
        # R53-6（round49b/50b 实锤）：排除口径必须是【实际跑起来的主力】，不只是"路由表 primary"。
        # 实测 .env：MEDIUM primary=MiniMax，但 medium 子任务首派走 **worker_parallel_pool 轮转**
        # → 实跑 Qwopus；失败后 alternate 取"第一个 ≠ MiniMax"的 fallback = **Qwopus** = 刚挂的
        # 那一个 → 日志照打"使用备选模型"，实际原地重试，恢复阶梯的中间几级全是空转
        # （round49b：三轮 retry_alternate 全派回同一模型 → 直接掉进"放弃 78/89"）。
        # 池挂在 config.worker（WorkerConfig）上，不在 router.config（ModelConfig）上——
        # 取错对象会让本闸静默失效（拿到 None）。
        try:
            from swarm.config import get_config
            _pool = {m for m in (getattr(get_config().worker, "worker_parallel_pool", None) or []) if m}
        except Exception:  # 配置不可用（CI/单测）→ 不排除，退回旧行为
            _pool = set()
        _cands = [f for f in (fallback_names or [])
                  if f and f != primary_name and f != _trivial and f not in _pool]
        if not _cands and _pool:
            # 排除主力池后无候选 → 说明"备选"只剩主力自己：如实告警（此前是**静默**换回同一个
            # 模型并打印"使用备选模型"，把恢复阶梯骗成有梯子的样子）。
            logger.warning(
                "[ROUTER] R53-6 难度 %s 排除实跑主力池 %s 后无异构备选 → 换模型无从谈起，"
                "调用方将回退主力（阶梯只剩'放弃'一级，请补配异构备选）", difficulty, sorted(_pool))
        return _cands

    def has_alternate_for_subtask(self, difficulty: str, modality: str = "text") -> bool:
        """E1（round38c 主题E）：该难度是否存在 ≠primary 的异构备选。

        retry_alternate 兑现判据的唯一事实源——轻量只查路由表，不构造 LLM。dispatch 旧判据
        `len(worker_parallel_pool)==1` 把「池长」当「无备选」，池 1 模型但 fallback 链有异构
        备选时 alternate 被静默改写为同模型+boost（register #26：换备日志 9 次、实派恒主力）。
        """
        try:
            return bool(self._alternate_candidates(difficulty, modality))
        except Exception:  # noqa: BLE001 — 判据失败按无备选处理（回退 boost 路径，不炸派发）
            return False

    def get_alternate_llm_for_subtask(
        self, difficulty: str, modality: str = "text"
    ) -> tuple[Runnable, str]:
        """换备选模型路径（audit #34）：返回 (备选模型 LLM, 模型名)。

        用于失败重试时强制切换到备选模型。封装原先 dispatch 直接调用 ModelRouter
        私有方法(_resolve_route/_get_provider_for_model)的逻辑，恢复封装边界。
        选【第一个 ≠ primary 的 fallback】做备选——FINDING-8(task 3e07c592)：旧实现盲取
        fallback_names[0]，而 fallback 链首常就是 primary 本身(如 MEDIUM_FALLBACK 链首=primary)，
        导致 retry_alternate "换" 到刚失败的同一个模型，形同虚设(本地引擎崩溃时整盘失败)。
        无真异构备选时(如 COMPLEX 只配单模型)回退 primary 并告警(可观测，不静默)。
        """
        primary_name, fallback_names = self._resolve_route(difficulty, modality)
        # 复核 C-4：与 has_alternate_for_subtask 同源候选（排除 trivial 档 primary，
        # 「换模型」不再意味着「降到最弱档」）
        alt = next(iter(self._alternate_candidates(difficulty, modality)), None)
        if not alt:
            logger.warning(
                "[ROUTER] %s/%s 无异构备选模型(fallback 链为空或全=primary '%s')，"
                "retry_alternate 仍用 primary；建议为该难度配异构后端 fallback",
                difficulty, modality, primary_name,
            )
        model_name = alt or primary_name
        prov = self._get_provider_for_model(model_name)
        role = f"worker/{difficulty}/alternate"
        llm = prov.get_chat_model(
            model_name,
            temperature=self.config.worker_temperature,
            callbacks=[ModelInvocationLogger(role, model_name, prov.provider.id)],
            max_tokens=(getattr(self.config, "worker_max_tokens", 0) or None),
            wallclock_budget=float(
                getattr(self.config, "worker_stream_wallclock_s", 0.0) or 0.0),
        )
        return llm, model_name

    def _resolve_route(self, difficulty: str, modality: str) -> tuple[str, list[str]]:
        """查路由表 → (primary_model_name, fallback_model_names)。
        fallback 现为【多级兜底链】(list)：主失败后逐级降级，全本地。"""
        if modality == "multimodal":
            fb = self.config.routing_multimodal_fallback
            configured = self.config.routing_multimodal
            # 换装安全（2026-07-15 复核 hunter#4 治本）：显式配置的 routing_multimodal 若【本身确为
            # 多模态】(能力库探测真值 or 名字启发式)，它即单一权威——绝不让能力库里【已下线模型的
            # 陈旧 probed 行】(upsert-only 不自动删，source=probed 会盖过新配模型的 default 档)把图像
            # 子任务静默首派到死端点。仅当配的这个【不是多模态】(疑似误配)才回落 A.5 自动发现。
            # 与北极星「单一权威 resolver」一致：显式路由配置 > 能力库陈旧探测。
            if configured and self._model_supports_multimodal(configured):
                return (configured, fb)
            # 设计 v3 A.5：配的不是多模态 → 从能力库自动发现本地 VL 模型；仍无则回退写死配置。
            mm_primary = self._multimodal_model_from_capabilities()
            if mm_primary:
                return (mm_primary, fb)
            return (configured, fb)

        route_map = {
            "trivial": (self.config.routing_trivial, self.config.routing_trivial_fallback),
            "medium": (self.config.routing_medium, self.config.routing_medium_fallback),
            "complex": (self.config.routing_complex, self.config.routing_complex_fallback),
        }
        if difficulty not in route_map:
            # B8 治本(fail-loud)：拼写错误/新增 enum/plan 输出非法 difficulty(如 "ultra")旧代码
            # 静默降 medium → 复杂任务被发到弱模型、无告警。显式记名回退，可观测（保留 medium 兜底
            # 不崩，但把"未知档"暴露出来供排查/校准，而非静默降质）。
            logger.warning(
                "[ROUTER] 未知 difficulty=%r（不在 trivial/medium/complex），回退 medium 路由——"
                "疑似 typo/新增档未接线，请核对 plan 输出与路由表", difficulty,
            )
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
            # B5 治本：只保留【本地 provider】的多模态模型——worker 全本地策略，绝不因能力库里
            # 探到/import 了云端 VL 模型就把多模态子任务静默路由到云端（成本/延迟/数据路径越界）。
            # 仍保留 A.5 的自动发现（本地探测出的 VL 模型照常可用，不强制在静态 in_use 清单里）。
            # 过滤后为空 → 返回 None，调用方回退写死 routing_multimodal（显式配置权威兜底）。
            def _is_local(mid: str | None) -> bool:
                if not mid:
                    return False
                try:
                    prov = self.config.provider_for_model(mid)
                    return bool(prov) and getattr(prov, "kind", "") == "local"
                except Exception:  # noqa: BLE001
                    return False

            mm = [r for r in mm if _is_local(r.get("model_id"))]
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

    def _model_supports_multimodal(self, model_id: str) -> bool:
        """指定模型是否多模态：优先能力库【探测真值】(source!=default)，无真值记录回退名字启发式。

        供 _resolve_route 判定"显式配的 routing_multimodal 本身是否多模态"——是则它权威，
        不被能力库里下线模型的陈旧行盖过（换装安全，hunter#4 治本）。DB 不可达/异常 → 启发式兜底。
        """
        from swarm.models import capability_store as cap
        try:
            prov = self.config.provider_for_model(model_id)
            if prov is not None:
                rec = cap.get_capability(getattr(prov, "id", None), model_id)
                if rec and rec.get("source") != cap.SOURCE_DEFAULT:
                    return bool(rec.get("supports_multimodal"))
        except Exception:  # noqa: BLE001 — DB 不可达等，落启发式
            pass
        return cap.heuristic_supports_multimodal(model_id)

    def _get_provider_for_model(self, model_name: str) -> EndpointProvider:
        """模型 → EndpointProvider。按 config.provider_for_model() 显式归属优先，
        启发式兜底（向后兼容）。找不到任何 provider 时合成一个 local 兜底避免崩溃。
        """
        pc = self.config.provider_for_model(model_name)
        if pc is None:
            # N-14：无 provider 归属时合成 local 兜底（保持旧"默认本地"行为），但必须【显式告警】——
            # 否则打错的模型名会静默全发本地端点、get_routing_table 仍显示预期名，配置错误不可察。
            logger.warning(
                "[ROUTER] 模型 '%s' 无任何 provider 归属 → 合成 local 兜底端点(%s)。"
                "若该模型本应走云端/其他端点，请检查模型名拼写或 model_providers 映射，"
                "否则请求会静默全部发往本地。",
                model_name, self.config.local_base_url,
            )
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
            # worker 输出上限：防改大文件时全文重写撑爆 context（worker agent 走此路径，
            # 非 get_llm_for_subtask；之前只在后者加 max_tokens 故未生效，必须在此也加）。
            max_tokens=(getattr(self.config, "worker_max_tokens", 0) or None),
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
