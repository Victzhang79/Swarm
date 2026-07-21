"""Task#12：LLM cassette PLAYBACK —— cassette_record 的回放对偶。

痛点（用户 2026-07-20 拍板）：近 100 轮 live 全烧真金，LLM 录像【只录不放】=取证日志，
死在 LLM 阶段（PLAN 不收敛/tech_design/规划 churn）的场景无法本地复现，每次重烧云端。

本模块：`SWARM_CASSETTE_REPLAY_DIR` 门控——brain 每次 `_astream` 调用按 request_sha 查录像，
命中则喂回录制 chunks（零云端），miss 按 `SWARM_CASSETTE_REPLAY_MISS` 处置。与 record 同源指纹
（复用 `cassette_record.compute_request_sha`，同 message_to_dict+sort_keys 算法）保证匹配。

范围铁律：cassette_record 仅录【brain 图节点】LLM 调用（worker 从不绑 _LLM_NODE_CV）→ playback
同样只在 brain 节点回放，worker 代码生成无录像不可回放（诚实边界：LLM playback 覆盖 PLAN 期，
不覆盖 worker 执行期；执行期确定性死因仍靠 plan 快照 cassette_replay 或真跑）。

miss 策略（env SWARM_CASSETTE_REPLAY_MISS）：
- **error（默认，复核整改）**：抛 CassetteReplayMiss——真·全离线，任何 miss 即 fail-loud。这是本工具的
  存在意义（"别再偷偷烧云端"）：默认拒绝，miss 立即炸而非静默直连云端（复核：passthrough 作默认会
  令 typo dir/prompt 漂移静默全量走云端=成本反转，正是要防的）；
- passthrough（显式 opt-in）：WARNING + 返回 None → 调用方 fall through 直连 live（部分录像/gap-fill 场景
  才用，且看 WARNING 知哪些真烧了云端）。

★复核整改（silent-failure hunter + code-reviewer）★：
- 录制中途 error 的 record【不当成功回放】（rec['error'] 非空=失败尝试，跳过取下一条 FIFO）；
- 门控开但载入 0 条录像（typo/空 dir）→ WARNING+record_degrade（不与"这条 miss"混同）；
- record_dir==playback_dir → 启动 WARNING（防回放缺口 backfill 污染 golden 录像）；
- node/model 与命中 record 不符 → WARNING（FIFO 错位可观测）；异常型 miss 与 sha-miss 分开计数。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

_ENV_DIR = "SWARM_CASSETTE_REPLAY_DIR"
_ENV_MISS = "SWARM_CASSETTE_REPLAY_MISS"

_lock = threading.Lock()
_index: dict[str, deque] | None = None   # request_sha -> FIFO deque[record]
_indexed_dir: str | None = None
_empty_warned: str | None = None         # 已对该 dir 发过"0 录像"告警（去重）
_stats = {"hit": 0, "miss_sha": 0, "miss_error_rec": 0, "miss_exc": 0}


class CassetteReplayMiss(RuntimeError):
    """SWARM_CASSETTE_REPLAY_MISS=error 下的 miss——真·全离线时任何未录调用即 fail-loud。"""


def _record_degrade_safe(category: str) -> None:
    try:
        from swarm.infra.degrade import record_degrade
        record_degrade(category)
    except Exception:  # noqa: BLE001
        pass


def playback_dir() -> str:
    return (os.environ.get(_ENV_DIR) or "").strip()


def playback_enabled() -> bool:
    return bool(playback_dir())


def _miss_mode() -> str:
    # 默认 error（fail-loud）——复核：passthrough 作默认=成本反转（typo/漂移静默烧云端）。
    return (os.environ.get(_ENV_MISS) or "error").strip().lower()


def _load_index(d: str) -> dict[str, deque]:
    idx: dict[str, deque] = defaultdict(deque)
    total = 0
    bad = 0
    for path in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001 — 半截行（录制中途被 kill）跳过不炸
                        bad += 1
                        continue
                    sha = rec.get("request_sha")
                    if sha:
                        idx[sha].append(rec)
                        total += 1
        except OSError as e:
            logger.warning("[cassette-playback] 读录像文件失败 %s：%s", path, e)
    # 每指纹按 seq 稳定排序（多文件/多 pid 合并后 FIFO 消费同指纹重复调用，如 retry）
    for sha in list(idx.keys()):
        idx[sha] = deque(sorted(idx[sha], key=lambda r: r.get("seq", 0)))
    logger.info("[cassette-playback] 载入 %d 条录像（%d 唯一指纹%s）从 %s",
                total, len(idx), f"，跳过 {bad} 半截行" if bad else "", d)
    return idx


def _ensure_index() -> dict[str, deque]:
    global _index, _indexed_dir, _empty_warned
    d = playback_dir()
    with _lock:
        if _index is None or _indexed_dir != d:
            _index = _load_index(d) if d else {}
            _indexed_dir = d
            # ★复核★门控开却载入 0 条录像（typo/空 dir）→ 与"这条 miss"截然不同=配置错，
            # 每 dir 一次 WARNING+record_degrade，否则 100% 静默走云端只有 INFO 一行。
            if d and not _index and _empty_warned != d:
                _empty_warned = d
                logger.warning(
                    "[cassette-playback] ★回放 dir 已设但载入 0 条录像★ dir=%s —— 每次 brain 调用都会 "
                    "miss（error 模式=立即炸；passthrough=全量走云端）。请检查 SWARM_CASSETTE_REPLAY_DIR "
                    "路径是否正确、是否含 llm-*.jsonl", d)
                _record_degrade_safe("models.cassette_playback.replay_dir_empty")
            # ★复核★record_dir==playback_dir 会在 miss+passthrough 时把 live 补录进正被回放的 dir
            # （污染 golden 录像）→ 启动 WARNING。
            try:
                from swarm.models.cassette_record import record_dir as _rec_dir
                if d and _rec_dir() and os.path.abspath(_rec_dir()) == os.path.abspath(d):
                    logger.warning(
                        "[cassette-playback] ★录制与回放 dir 相同★ (%s)——miss+passthrough 会把 live 补录"
                        "进正回放的 dir，污染 golden 录像。回放期建议只设 REPLAY_DIR，勿同设 RECORD_DIR。", d)
            except Exception:  # noqa: BLE001
                pass
        return _index


def reset_index() -> None:
    """测试/换 dir 用：强制下次 _ensure_index 重载。"""
    global _index, _indexed_dir, _empty_warned
    with _lock:
        _index = None
        _indexed_dir = None
        _empty_warned = None
        for k in _stats:
            _stats[k] = 0


def lookup(node: str, model: str, args: tuple, kwargs: dict) -> dict | None:
    """按 request_sha 查录像并【消费】（FIFO）。命中返回 clean record，miss 返回 None。
    ★复核整改★：
    - 跳过 rec['error'] 非空的失败尝试（录制中途 error=timeout/中断/切备前的失败，不当成功回放，
      取下一条 FIFO——faithful 结果=拿到成功的那次，而非把半截失败流当成功喂出）；
    - node/model 与命中不符 → WARNING（FIFO 错位可观测）；
    - 异常型 miss（miss_exc）与 sha-miss（miss_sha）分开计数（系统性 bug vs 正常漂移可分）。
    fail-open：指纹算不出/索引异常 → None（miss，绝不炸调用链）。"""
    try:
        from swarm.models.cassette_record import compute_request_sha
        _msgs, sha = compute_request_sha(args, kwargs)
        if not sha:
            with _lock:
                _stats["miss_sha"] += 1
            return None
        idx = _ensure_index()
        with _lock:
            dq = idx.get(sha)
            if not dq:
                _stats["miss_sha"] += 1
                return None
            # 跳过失败尝试记录，取首个 clean（无 error）record
            rec = None
            while dq:
                cand = dq.popleft()
                if cand.get("error"):
                    logger.debug("[cassette-playback] 跳过失败尝试录像 seq=%s error=%.60s",
                                 cand.get("seq"), str(cand.get("error")))
                    continue
                rec = cand
                break
            if rec is None:
                # DR-07-F8(#100)：指纹在但只剩失败尝试录像 → 记 miss_error_rec（此前误计 miss_sha，
                # 使 miss_error_rec 恒 0、sha-miss 虚高，运维误诊"指纹漂移/录像陈旧"而非"该调用当轮
                # 就没成功过"）。总 miss=miss_sha+miss_error_rec+miss_exc 不变，分项才如实可辨。
                _stats["miss_error_rec"] += 1
                return None
            _stats["hit"] += 1
        # node/model 校验（锁外，仅告警不改判）
        if (rec.get("node") not in (None, "", node)) or (rec.get("model") not in (None, "", model)):
            logger.warning(
                "[cassette-playback] 命中 record 的 node/model 与本调用不符（FIFO 错位？）："
                "调用 node=%s model=%s vs 录像 node=%s model=%s",
                node, model, rec.get("node"), rec.get("model"))
        return rec
    except Exception as e:  # noqa: BLE001 — playback 绝不拖垮模型层
        with _lock:
            _stats["miss_exc"] += 1
        logger.warning("[cassette-playback] lookup 异常（降级 miss；异常型 miss 累计 %d）：%s",
                       _stats["miss_exc"], e)
        return None


async def replay_chunks(rec: dict):
    """把录制 record 的 chunks 重建为 ChatGenerationChunk 逐个 yield（含 reasoning/finish_reason）。"""
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    for c in (rec.get("chunks") or []):
        if not isinstance(c, dict):
            continue
        try:
            msg = AIMessageChunk(
                content=c.get("content") or "",
                additional_kwargs=c.get("additional_kwargs") or {},
                response_metadata=c.get("response_metadata") or {},
            )
        except Exception:  # noqa: BLE001 — 老/异形 payload 退化为仅正文
            msg = AIMessageChunk(content=c.get("content") or "")
        gi = c.get("generation_info") or None
        yield ChatGenerationChunk(message=msg, generation_info=gi)


def on_miss(node: str, model: str) -> None:
    """miss 处置：error→抛（真离线 fail-loud）；passthrough→WARNING 返回（调用方直连 live）。"""
    if _miss_mode() == "error":
        raise CassetteReplayMiss(
            f"cassette 回放 miss node={node} model={model}"
            "（SWARM_CASSETTE_REPLAY_MISS=error：录像未覆盖此调用/prompt 已漂移=录像陈旧）")
    logger.warning(
        "[cassette-playback] miss node=%s model=%s → 直连 live（passthrough；prompt 漂移或"
        "录像不全，此调用将真烧云端）", node, model)


def stats() -> dict:
    return dict(_stats)


def log_summary() -> dict:
    """回放对账汇总（供 run 收尾调用）——★复核★把 hit/miss 从"每调用一行淹没在长日志"
    升为一条聚合账，令"以为全离线实则大量走云端"可见。passthrough 模式下 miss>0 尤其要看见。"""
    s = dict(_stats)
    miss = s["miss_sha"] + s["miss_error_rec"] + s["miss_exc"]
    total = s["hit"] + miss
    if not playback_enabled() or total == 0:
        return s
    pct = 100.0 * miss / total
    _lvl = logger.warning if miss else logger.info
    _lvl("[cassette-playback] 回放对账：hit=%d miss=%d（%.0f%% 走 live；sha-miss=%d 异常-miss=%d）dir=%s",
         s["hit"], miss, pct, s["miss_sha"], s["miss_exc"], playback_dir())
    return s
