"""模型能力探测器（设计 v3 A批2 / A.2）。

对一个 provider（OpenAI 兼容端点）下的模型探测真实能力，写入 model_capabilities：
  - context_window：A.2.1 四层分层探测（models 字段 → max_model_len → 错误消息解析 → 启发式默认）
  - supports_multimodal：发一条含 image_url 的最小消息，正常响应=True，报不支持=False
  - gen_speed_tps：发固定小 prompt，测 tokens/秒

工程红线（设计 A.6）：
  - 探测有副作用（真实 API 调用花 token/算力）——必须用户显式触发，绝不在启动时自动全探。
  - context_window 第 3 层用"故意超长的最小请求"从 400 错误里 parse 真值；服务端只是
    拒绝（不真正分配超大上下文，不 OOM），比二分试探便宜安全。**明确不做主动二分试探**。
  - 本地推理服务 /models 格式各异（OpenAI/Open WebUI/Ollama）→ 解析容错。

纯 httpx，无 LangChain 依赖（要拿原始 HTTP 状态码和错误体）。可独立 mock 单测。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from swarm.config.settings import ProviderConfig
from swarm.models import capability_store as cap

logger = logging.getLogger(__name__)

# 探测请求超时（秒）。本地服务慢，给足；但探测整体不应卡太久。
_LIST_TIMEOUT = 15.0
_PROBE_TIMEOUT = 30.0

# context_window：models 字段里可能出现的键名（OpenAI / 各家 vLLM 命名不一）。
_CONTEXT_FIELD_KEYS = (
    "context_window", "max_model_len", "max_context_length",
    "context_length", "max_tokens", "n_ctx",
)

# 第 3 层：从 400 错误消息里抠真实上下文长度的正则模式（命中即取最大数字）。
_CONTEXT_ERROR_PATTERNS = (
    r"maximum context length is\s*(\d+)",
    r"max_model_len.*?(\d+)",
    r"context length of\s*(\d+)",
    r"maximum.*?(\d+)\s*tokens",
    r"reduce.*?length.*?(\d+)",
)

# 1×1 透明 PNG 的 data URL（多模态探测最小图，不依赖外部资源）。
_TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _auth_headers(provider: ProviderConfig) -> dict[str, str]:
    """构造鉴权头。本地服务常无 key，则不带 Authorization。"""
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def _base(provider: ProviderConfig) -> str:
    """归一化 base_url（去尾斜杠）。chat/models 端点在此基础上拼。"""
    return provider.base_url.rstrip("/")


def _chat_url(provider: ProviderConfig) -> str:
    """chat completions 端点。base_url 已含 /v1 或 /api 则直接拼，否则补 /v1。"""
    base = _base(provider)
    if base.endswith("/v1") or base.endswith("/api"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _models_url(provider: ProviderConfig) -> str:
    base = _base(provider)
    if base.endswith("/v1") or base.endswith("/api"):
        return f"{base}/models"
    return f"{base}/v1/models"


# ──────────────────────────────────────────────
# 列模型
# ──────────────────────────────────────────────

def list_models(provider: ProviderConfig) -> tuple[list[dict[str, Any]], str | None]:
    """列出 provider 下的模型原始对象（保留字段供 context 解析）。

    返回 (model_objects, error)。容错多种端点（OpenAI /models、Ollama /api/tags）。
    """
    headers = _auth_headers(provider)
    candidates = [_models_url(provider)]
    # 本地服务额外尝试 Ollama /api/tags（base 去掉 /v1|/api 后拼）。
    base_root = _base(provider).removesuffix("/v1").removesuffix("/api")
    candidates.append(f"{base_root}/api/tags")

    last_err: str | None = None
    verify = provider.kind != "local"  # 本地自签/局域网不验证 SSL
    for url in candidates:
        try:
            with httpx.Client(timeout=_LIST_TIMEOUT, verify=verify) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("data", data.get("models", []))
                objs = [m for m in raw if isinstance(m, dict) and (m.get("id") or m.get("name"))]
                if objs:
                    return objs, None
            elif resp.status_code == 401:
                return [], "认证失败：请检查 API Key"
            else:
                last_err = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
    return [], last_err


def _model_id_of(obj: dict[str, Any]) -> str:
    return str(obj.get("id") or obj.get("name") or "")


# ──────────────────────────────────────────────
# context_window 分层探测（A.2.1）
# ──────────────────────────────────────────────

def context_from_model_object(obj: dict[str, Any]) -> int | None:
    """第 1-2 层：从 /v1/models 返回的模型对象里读 context 字段。

    OpenAI 与部分 vLLM 在 model 对象（或其嵌套字段）里暴露上下文长度。
    """
    # 顶层字段
    for key in _CONTEXT_FIELD_KEYS:
        val = obj.get(key)
        if isinstance(val, int) and val > 0:
            return val
    # 嵌套常见容器
    for container_key in ("meta", "config", "model_info", "details"):
        sub = obj.get(container_key)
        if isinstance(sub, dict):
            for key in _CONTEXT_FIELD_KEYS:
                val = sub.get(key)
                if isinstance(val, int) and val > 0:
                    return val
    return None


def context_from_error(provider: ProviderConfig, model_id: str) -> int | None:
    """第 3 层：发故意超长的最小请求，从 400 错误消息里 parse 真实上下文长度。

    安全性：服务端在校验阶段即拒绝超长请求，不真正分配上下文/不 OOM。
    用 max_tokens=1 + 极大 prompt token 数触发 "maximum context length is N"。
    """
    headers = _auth_headers(provider)
    verify = provider.kind != "local"
    # 用一个声称超长的请求。多数 OpenAI 兼容服务在收到超 max_model_len 的输入时
    # 返回 400 + 含真实上限的错误消息。这里用重复字符堆出可观 token 量但不夸张到
    # 撑爆请求体——配合错误正则即可拿真值。
    big_prompt = "token " * 200_000  # ~200k 词，足以超过绝大多数上下文上限
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": big_prompt}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, verify=verify) as client:
            resp = client.post(_chat_url(provider), headers=headers, json=payload)
        if resp.status_code == 200:
            # 网关接受了超长请求（不按 OpenAI 标准拒绝，常见于 Open WebUI / 自建网关）：
            # 从 usage.prompt_tokens 推断 context_window 的【下界】——它至少能装下这么多。
            # 比启发式默认更接近真值（虽非精确上限）。标注为 parsed(下界估计)。
            try:
                data = resp.json()
                pt = (data.get("usage") or {}).get("prompt_tokens")
                if isinstance(pt, int) and pt >= 1024:
                    # 向上取整到常见窗口档位附近（保守取 prompt_tokens 本身作下界）
                    return pt
            except Exception:  # noqa: BLE001
                pass
            return None
        text = resp.text or ""
        return _parse_context_from_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_from_error 探测失败 model=%s: %s", model_id, exc)
        return None


def _parse_context_from_text(text: str) -> int | None:
    """从错误文本里抠上下文上限数字（取所有匹配中的最大值，过滤过小噪声）。"""
    candidates: list[int] = []
    low = text.lower()
    for pat in _CONTEXT_ERROR_PATTERNS:
        for m in re.finditer(pat, low):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            # 过滤明显噪声（如 max_tokens=1 里的 1）：上下文窗口至少 1024。
            if n >= 1024:
                candidates.append(n)
    return max(candidates) if candidates else None


def probe_context_window(
    provider: ProviderConfig, model_id: str, model_obj: dict[str, Any] | None = None
) -> tuple[int, str]:
    """四层分层探测 context_window，命中即停。

    返回 (window, source)，source ∈ {parsed, probed, default}：
      1-2 层（models 字段）→ parsed
      3 层（错误消息解析）→ probed（真发了请求拿真值）
      4 层（启发式默认）→ default
    """
    # 第 1-2 层：models 对象字段
    if model_obj is not None:
        win = context_from_model_object(model_obj)
        if win:
            return win, cap.SOURCE_PARSED

    # 第 3 层：错误消息解析（发真实请求）
    win = context_from_error(provider, model_id)
    if win:
        return win, cap.SOURCE_PROBED

    # 第 4 层：启发式默认
    return cap.heuristic_context_window(model_id, provider.kind), cap.SOURCE_DEFAULT


# ──────────────────────────────────────────────
# 多模态探测
# ──────────────────────────────────────────────

def probe_multimodal(provider: ProviderConfig, model_id: str) -> bool | None:
    """发一条含 image_url 的最小消息：正常响应=True，报不支持图像=False。

    返回 True/False；网络等不确定错误返回 None（调用方回退启发式）。
    """
    headers = _auth_headers(provider)
    verify = provider.kind != "local"
    payload = {
        "model": model_id,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What color is this? Reply one word."},
                {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URL}},
            ],
        }],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, verify=verify) as client:
            resp = client.post(_chat_url(provider), headers=headers, json=payload)
        if resp.status_code == 200:
            return True
        text = (resp.text or "").lower()
        # 明确"不支持图像/多模态"信号 → False
        neg_signals = (
            "does not support image", "image input", "not support multimodal",
            "no multimodal", "invalid", "unsupported", "image_url",
            "vision", "not a multimodal", "cannot process image",
        )
        if resp.status_code in (400, 422) and any(s in text for s in neg_signals):
            return False
        # 其它错误（5xx/超时/限流）→ 不确定
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe_multimodal 失败 model=%s: %s", model_id, exc)
        return None


# ──────────────────────────────────────────────
# 速度探测
# ──────────────────────────────────────────────

def probe_speed(provider: ProviderConfig, model_id: str) -> float:
    """发固定小 prompt，测 tokens/秒。失败返回 0.0（未测）。"""
    headers = _auth_headers(provider)
    verify = provider.kind != "local"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Count from 1 to 30, comma separated."}],
        "max_tokens": 128,
        "temperature": 0,
    }
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=_PROBE_TIMEOUT, verify=verify) as client:
            resp = client.post(_chat_url(provider), headers=headers, json=payload)
        elapsed = time.monotonic() - t0
        if resp.status_code != 200 or elapsed <= 0:
            return 0.0
        data = resp.json()
        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens")
        if not completion_tokens:
            # 无 usage 字段则按返回文本粗估（~4 chars/token）
            choices = data.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content") or ""
            completion_tokens = max(len(content) // 4, 1)
        return round(completion_tokens / elapsed, 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe_speed 失败 model=%s: %s", model_id, exc)
        return 0.0


# ──────────────────────────────────────────────
# 单模型 / 全 provider 探测编排
# ──────────────────────────────────────────────

def probe_model(
    provider: ProviderConfig,
    model_id: str,
    model_obj: dict[str, Any] | None = None,
    *,
    measure_speed: bool = True,
) -> dict[str, Any]:
    """探测单个模型全部能力，返回一条能力记录（不落库，由调用方决定）。"""
    from datetime import datetime, timezone

    window, win_source = probe_context_window(provider, model_id, model_obj)

    mm = probe_multimodal(provider, model_id)
    if mm is None:
        # 探测不确定 → 回退启发式，并把 source 降级标注
        mm = cap.heuristic_supports_multimodal(model_id)
        mm_uncertain = True
    else:
        mm_uncertain = False

    speed = probe_speed(provider, model_id) if measure_speed else 0.0

    # 整体 source：context 探到真值则 probed，否则跟随 context source
    if win_source == cap.SOURCE_PROBED or not mm_uncertain:
        source = cap.SOURCE_PROBED if win_source != cap.SOURCE_DEFAULT else cap.SOURCE_DEFAULT
    else:
        source = win_source

    note_parts = []
    if win_source == cap.SOURCE_DEFAULT:
        note_parts.append("context未探明(默认)")
    if mm_uncertain:
        note_parts.append("多模态未探明(启发式)")

    return {
        "provider_id": provider.id,
        "model_id": model_id,
        "context_window": window,
        "supports_multimodal": bool(mm),
        "gen_speed_tps": speed,
        "kind": provider.kind,
        "source": source,
        "note": "；".join(note_parts),
        "probed_at": datetime.now(timezone.utc),
    }


def probe_provider(
    provider: ProviderConfig,
    *,
    only_models: list[str] | None = None,
    measure_speed: bool = True,
    persist: bool = True,
    conn_str: str | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """探测 provider 下的模型，写库（persist=True）。

    only_models 给定时**只探这些指定模型**（用户路由策略里在用的模型）——
    云端聚合接入点可能列出几十上百模型，全探既花钱又无意义；默认 API 只传在用集合。
    only_models=None 时探 /v1/models 返回的全部模型（本地服务/高级场景）。

    指定模型若能在 /v1/models 里匹配到对象，则复用其字段（第1-2层 context 探测）；
    匹配不到也照常探测（用户配的模型名通常是对的，云端 /models 未必完整）。

    progress_cb(done, total, current_model) 可选，用于上报进度。
    返回 {provider_id, total, probed, errors, capabilities}。
    """
    objs, err = list_models(provider)
    obj_by_id = {_model_id_of(o): o for o in objs if _model_id_of(o)}

    # 认证失败是致命错误：key 无效时所有探测都会 401，继续探只会落一堆假 default 数据。
    # 直接中止并把真实原因返回给用户（让 UI 提示"检查 API Key"），不静默吞掉。
    if err and "认证失败" in err:
        return {"provider_id": provider.id, "total": 0, "probed": 0,
                "error": err, "capabilities": []}

    if only_models is not None:
        # 精确探测在用模型：以 only_models 为准，能匹配到对象就带上字段
        targets = [(m, obj_by_id.get(m)) for m in only_models if m]
        # 列模型因"端点不暴露 /models"等非认证原因失败 → 仍可探（用户配的模型名通常对）
    else:
        if err and not objs:
            return {"provider_id": provider.id, "total": 0, "probed": 0,
                    "error": err, "capabilities": []}
        targets = [(_model_id_of(o), o) for o in objs if _model_id_of(o)]

    total = len(targets)
    caps: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, (model_id, obj) in enumerate(targets):
        if not model_id:
            continue
        if progress_cb:
            try:
                progress_cb(i, total, model_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            record = probe_model(provider, model_id, obj, measure_speed=measure_speed)
            if persist:
                cap.upsert_capability(
                    record["provider_id"], record["model_id"],
                    context_window=record["context_window"],
                    supports_multimodal=record["supports_multimodal"],
                    gen_speed_tps=record["gen_speed_tps"],
                    kind=record["kind"], source=record["source"],
                    note=record["note"], probed_at=record["probed_at"],
                    conn_str=conn_str,
                )
            caps.append(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("探测模型 %s 失败: %s", model_id, exc)
            errors.append(f"{model_id}: {exc}")

    if progress_cb:
        try:
            progress_cb(total, total, "")
        except Exception:  # noqa: BLE001
            pass

    return {
        "provider_id": provider.id,
        "total": total,
        "probed": len(caps),
        "errors": errors,
        "capabilities": caps,
    }
