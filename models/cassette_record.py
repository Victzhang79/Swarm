"""Task#10：模型层 LLM 录制钩——env 门控，把 brain 每次云端流式调用的【请求 messages +
有序响应 chunk】忠实落成 cassette，供离线重放【整条 brain 图】。

痛点（对齐 swarm-offline-cassette-replay-harness 记忆）：plan-cassette 抽自 checkpoint，
只留最终 state["plan"]，覆盖【plan 之后】的确定性流水线（R57-R62 死因）。但产出那份 plan 的
【上游 LLM 节点】（tech_design / contract_design / extract / plan 本身 + 新加的 coherence 闸
可能触发 replan）的原始 LLM 输入输出，checkpoint 一概不留——一旦不录即永久丢失。round63 是
数小时、计费的 live 跑；若它暴露一个【brain 规划期】新 bug，没有 LLM 录制就无法离线重放，只能
再烧一次多小时 live——正是本录制钩要根除的烧钱回路。故本钩必须在 round63 【前】落地。

铁律（对齐 router 心跳/槽位闸的 fail-open 纪律）：
  1) 默认关：未设 SWARM_CASSETTE_RECORD_DIR 即完全旁路，热路径零开销（一次 dict 取值）。
  2) 只录 brain：仅当 LLM 调用带 brain 节点标签（router._LLM_NODE_CV，仅 brain 图节点经
     set_llm_node 绑定；worker 从不绑）才录——录制钩为诊断 brain 规划而生，自动排除高频
     worker 本地流量。过滤由调用方（router._astream）施加，本模块不反向依赖 router。
  3) 观测面绝不拖垮调用：任何录制失败（磁盘满/序列化异常/取消）只 WARNING 一次并旁路，
     绝不把异常泄回 LLM 流；tee_record 忠实把 GeneratorExit/CancelledError 透传给底层流，
     保住既有 aclose→GPU abort 链（与 heartbeat/provider-slot 同一 fail-open 对称）。
  4) 每进程一份 llm-<pid>.jsonl：单事件循环内一次同步整行 write 对协程原子（build↔write 间
     无 await），且 pid 分文件规避跨进程 O_APPEND 对超 PIPE_BUF 长行的撕裂。

cassette 行 schema（swarm-llm-cassette/v1，一行一次完整 LLM 调用）：
  {seq, node, model, provider, request_sha, messages:[...], stop, elapsed_s,
   n_chunks, truncated, error, chunks:[{content, additional_kwargs, response_metadata}]}
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import threading
import time
from contextlib import aclosing
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# env 门控：设置此目录即开启录制（非空）；不设/空 = 关闭（默认）。单一开关，presence=on。
_ENV_DIR = "SWARM_CASSETTE_RECORD_DIR"
_SCHEMA = "swarm-llm-cassette/v1"
# OOM 保险丝：单次调用缓冲的 chunk 上限。云端 reasoning 调用实测可达 6w+ chunk；封顶避免
# 病态调用把内存吃穿。超过只累加 n_chunks 计数、不再存 chunk 体，落盘标 truncated=True。
_MAX_CHUNKS = 400_000

_seq = itertools.count()
_fh_lock = threading.Lock()
_fh: Any = None                 # 缓存的 append 文件句柄（每进程一份）
_fh_key: Any = None             # (pid, dir)——fork 后 pid 变即自动换文件
# 录制失败节流告警：首次必报，其后每 _WARN_EVERY 次再报一行（带累计计数）。纯 warn-once
# 会让"从第 1 次起就全失败"（磁盘满/权限）在数小时长跑里只留一行早已刷走的日志，运维误以为
# 全程已录、实则 cassette 从头就空（复核 F4）。周期性重报让"整轮失败"持久可见。
_fail_count = 0
_WARN_EVERY = 1000


def record_dir() -> str:
    """当前录制目录（去空白）；空串=录制关闭。每次实时读 env（dict 取值，热路径可忽略），
    不缓存——避免 get_config 式进程缓存把"跑前才设的开关"读旧（记忆 model-swap-runbook 陷阱）。"""
    d = os.environ.get(_ENV_DIR) or ""
    return d.strip()


def recording_enabled() -> bool:
    return bool(record_dir())


def _warn_fail(msg: str, exc: BaseException) -> None:
    global _fail_count
    _fail_count += 1
    n = _fail_count
    if n == 1 or n % _WARN_EVERY == 0:
        logger.warning("[cassette-record] 录制失败#%d——本调用旁路，LLM 流不受影响（%s）: %s",
                       n, msg, exc)


def _safe_str(x: Any) -> str:
    try:
        return str(x)
    except Exception:  # noqa: BLE001
        return "<unstr>"


def _jsonable(x: Any) -> Any:
    """尽力转 JSON 可序列化；失败退化为 str（保证 cassette 行永远可 dumps）。"""
    if x is None:
        return None
    try:
        json.dumps(x, ensure_ascii=False)
        return x
    except Exception:  # noqa: BLE001
        return _safe_str(x)


def _msg_to_dict(m: Any) -> dict:
    try:
        from langchain_core.messages import message_to_dict
        return message_to_dict(m)
    except Exception:  # noqa: BLE001 — 老/异形 message 退化为最小忠实切片
        return {"type": type(m).__name__,
                "data": {"content": _jsonable(getattr(m, "content", ""))}}


def _messages_from_args(args: tuple, kwargs: dict) -> list:
    # langchain BaseChatModel._astream(self, messages, stop=None, run_manager=None, **kwargs)
    # —— self 未入 args（绑定方法），args[0] 即 messages 列表。
    msgs = args[0] if args else kwargs.get("messages")
    return [_msg_to_dict(m) for m in (msgs or [])]


def _chunk_to_dict(chunk: Any) -> dict:
    """把一个流式 chunk 转最小忠实切片：正文 + reasoning（additional_kwargs）+ 收尾元数据。"""
    msg = getattr(chunk, "message", None)
    if msg is not None:
        out = {
            "content": _jsonable(getattr(msg, "content", "")),
            "additional_kwargs": _jsonable(getattr(msg, "additional_kwargs", None)),
            "response_metadata": _jsonable(getattr(msg, "response_metadata", None)),
        }
        # R64-T6：ChatGenerationChunk 的 finish_reason 落在 generation_info（不在
        # response_metadata）——旧实现 msg 分支把它丢了，round64 全 58 行 finish_reason
        # 皆 None、事后无法判截断。只在非空时带上；超大 payload（provider 塞 logprobs 等）
        # 只留 finish_reason+截断标（猎手批2 F4：条数帽 _MAX_CHUNKS 管不住单条膨胀，
        # 录制自诩轻量不能反成磁盘炸弹）。
        gi = getattr(chunk, "generation_info", None)
        if gi:
            gj = _jsonable(gi)
            try:
                if len(json.dumps(gj, ensure_ascii=False)) > 4096:
                    gj = {"finish_reason": (gi.get("finish_reason")
                                            if isinstance(gi, dict) else None),
                          "_truncated": True}
            except Exception:  # noqa: BLE001 — 尺寸探测失败按原样保留（fail-open）
                pass
            out["generation_info"] = gj
        return out
    return {
        "content": _jsonable(getattr(chunk, "content", "")),
        "generation_info": _jsonable(getattr(chunk, "generation_info", None)),
    }


def compute_request_sha(args: tuple, kwargs: dict) -> tuple[list, str]:
    """请求指纹（record/playback 单一事实源，Task#12 playback 复用保证匹配）。
    返回 (messages_dicts, sha)；异常 → (msgs 或 [], "")（fail-open，绝不炸调用链）。"""
    msgs = _messages_from_args(args, kwargs)
    try:
        sha = hashlib.sha256(
            json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001
        sha = ""
    return msgs, sha


def _new_buf(node: str, model: str, provider: str, args: tuple, kwargs: dict) -> dict:
    msgs, sha = compute_request_sha(args, kwargs)
    stop = kwargs.get("stop")
    return {
        "schema": _SCHEMA,
        "seq": next(_seq),
        "node": node,
        "model": model,
        "provider": provider,
        "request_sha": sha,
        "messages": msgs,
        "stop": _jsonable(stop),
        "chunks": [],
        "n_chunks": 0,
        "truncated": False,
        # 内部瞬时字段（落盘前 pop 掉，不进 JSON 行）：
        # _dir 在【调用开始】即钉扎（复核 F2）——_flush 若改读实时 record_dir()，一旦 env 在
        # 这次数十分钟的调用中途被清/改，整段已缓冲的记录会被静默丢弃、零日志（正是"看似这轮
        # 没调 LLM"的陷阱）。钉扎到调用起点的目录即消除该竞态。
        "_dir": record_dir(),
        "_t0": time.monotonic(),
    }


def _note(buf: dict, chunk: Any) -> None:
    buf["n_chunks"] += 1
    if len(buf["chunks"]) >= _MAX_CHUNKS:
        buf["truncated"] = True
        return
    buf["chunks"].append(_chunk_to_dict(chunk))


def _get_fh(d: str):
    global _fh, _fh_key
    key = (os.getpid(), d)
    if _fh is not None and _fh_key == key:
        return _fh
    with _fh_lock:
        if _fh is not None and _fh_key == key:
            return _fh
        if _fh is not None:  # 复核 F3：目标目录/进程变了——先关旧句柄，杜绝 fd 泄漏
            try:
                _fh.close()
            except Exception:  # noqa: BLE001
                pass
            _fh = None
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"llm-{os.getpid()}.jsonl")
        _fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — 进程级长生命句柄
        _fh_key = key
        return _fh


def _flush(buf: dict, error: BaseException | None) -> None:
    # 复核 F2：用调用起点钉扎的 _dir，绝不在此重读实时 record_dir()（避免中途改 env 静默丢记录）。
    d = buf.pop("_dir", "") or ""
    elapsed = round(time.monotonic() - buf.pop("_t0", time.monotonic()), 3)
    if not d:
        _warn_fail("录制起点目录为空（不应发生：仅在 enabled 时才缓冲）", RuntimeError("empty _dir"))
        return
    buf["elapsed_s"] = elapsed
    buf["error"] = None if error is None else f"{type(error).__name__}: {_safe_str(error)}"
    line = json.dumps(buf, ensure_ascii=False)
    fh = _get_fh(d)
    with _fh_lock:
        fh.write(line + "\n")
        fh.flush()  # round63 可能被中途 kill——逐调用 flush 保已录不丢（调用间隔以秒计）


async def tee_record(stream: AsyncIterator, *, node: str, model: str,
                     provider: str, args: tuple, kwargs: dict) -> AsyncIterator:
    """透传 stream 的每个 chunk 同时录制；全程 fail-open：录制任何环节失败都只旁路，
    绝不中断/篡改 LLM 流，且把 GeneratorExit/CancelledError 忠实透传给底层流（保 aclose 链）。"""
    buf: dict | None = None
    try:
        buf = _new_buf(node, model, provider, args, kwargs)
    except Exception as exc:  # noqa: BLE001
        _warn_fail("构建录制缓冲失败", exc)
        buf = None

    err: BaseException | None = None
    try:
        # 复核 F1：aclosing 确保消费者弃流/出错时，把 close 【确定性】转发给底层 stream，
        # 而非依赖 async 生成器 GC 终结器（在持久事件循环里非确定、可能滞后到进程销毁后）。
        # 这样 tee_record 这层不给既有 aclose→释放链新增 GC 悬挂 hop（router 层同法再包一层）。
        async with aclosing(stream):
            async for chunk in stream:
                if buf is not None:
                    try:
                        _note(buf, chunk)
                    except Exception as exc:  # noqa: BLE001 — 单 chunk 录制失败不掩盖流
                        _warn_fail("录制 chunk 失败", exc)
                yield chunk
    except BaseException as exc:  # noqa: BLE001 — 记录失败调用（超时/取消/GeneratorExit 也值得重放），不吞
        err = exc
        raise
    finally:
        if buf is not None:
            try:
                _flush(buf, err)
            except Exception as exc:  # noqa: BLE001
                _warn_fail("落盘 cassette 行失败", exc)
