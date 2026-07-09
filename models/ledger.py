"""TaskLedger —— 单任务全局预算脊柱（深读登记册 2026-07-09 §九 阶段1）。

单一权威账本：task_id → {cloud_tokens_in/out, local_tokens, llm_calls, wall_ms,
replan_rounds, stage_spent}，写穿 DB（周期 flush + 读前 flush，镜像 usage_tracker
成熟模式），resume/重启由 attach() 从 DB 恢复延续（治 F1：内存态归零）。

预留-结算模型（治 B4/B7/B10）：
  - reserve()：每次 LLM 调用发起前按估算（prompt 长度 + max_tokens）预留额度，
    在飞预留计入占用；余额不足【拒绝发起】抛 TaskTokenLimitExceeded——闸门从
    "节点边界查得晚"前移到调用发起点。
  - settle()：完成按真实 usage 结算并释放预留。
  - settle_error()：error/中止路径按已收 chunk 估算结算【宁可高估】——input 取
    max(已收 chunk, 预留估算)（流被掐断 input 在服务端已全额计费），output 取已收
    chunk。治 B4"中止调用 token 不入账（恰是饱和期最贵形态）"。

阶段子预算（治 B7 规划循环无聚合预算）：从总预算按比例派生
（extract 5% / plan 25% / execute 55% / verify 15%，可配比例非绝对值）；
阶段烧穿=该阶段 escalate（异常带 stage 归因），不吃兄弟阶段的份。
stage 由 runner 事件循环按节点名设置（ledger 自持 per-task 当前 stage，
不用 ContextVar——node 协程创建时已拷贝上下文，跨协程 ContextVar 不可达）。

本地 token 独立列、独立闸值（默认 0=不闸）：本地=自建算力由墙钟兜底，
绝不复现 round28"本地 13.35M 合法消耗被 $ 闸门误杀"；云端闸只拦真金白银。

budget_total=0 → track-only（观测不闸），对齐 max_task_tokens=0 的既有关闸语义。
未 attach 的 task（预处理期/无任务上下文）自动 track-only。
线程安全：callbacks 可能从线程池/多协程并发进来，全程持锁。
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import uuid

from swarm.models.errors import TaskTokenLimitExceeded

logger = logging.getLogger(__name__)

# §九 示例比例（可配比例而非绝对值）；未列出的 stage 不设子预算（只受总闸）。
DEFAULT_STAGE_RATIOS: dict[str, float] = {
    "extract": 0.05,
    "plan": 0.25,
    "execute": 0.55,
    "verify": 0.15,
}

# 阶段1.4：brain 图节点 → 预算阶段（runner 事件循环 on_chain_start 时设置）。
# 未列出/未知节点返回 None=保持上一阶段（ChannelWrite 等框架噪声不重置）。
STAGE_BY_NODE: dict[str, str] = {
    "ingest": "extract", "extract_requirements": "extract",
    "analyze": "plan", "detect_stack": "plan", "clarify": "plan", "assess": "plan",
    "tech_design": "plan", "contract_design": "plan", "review_design": "plan",
    "elaborate": "plan", "plan": "plan", "validate_plan": "plan", "confirm": "plan",
    "increment_retry": "plan",
    "dispatch": "execute", "monitor": "execute", "handle_failure": "execute",
    "merge": "execute", "revision": "execute",
    "adversarial_verify": "verify", "verify_l2": "verify", "verify_runtime": "verify",
    "verify_l3": "verify", "deliver": "verify",
    "learn_success": "verify", "learn_failure": "verify",
}


def stage_for_node(node_name: str) -> str | None:
    return STAGE_BY_NODE.get(node_name or "")


# 阶段1.5：重试层发起前的最小余量（低于此还开新一轮重试=必然中途烧穿，宁可现在
# 确定性 salvage→PARTIAL 保产物）。与 _LedgerGuard 预留口径同数量级。
RETRY_MIN_HEADROOM = 4096


def _reservation_ttl_s() -> float:
    """复核 F1（阶段1，CRITICAL）：预留 TTL 兜底。langchain 对被取消的 ainvoke()
    【不触发 on_llm_error】（复核用最小脚本实测 langchain-core 1.4.0 坐实）——worker
    单步 wait_for 掐断（生产高频）后预留永不释放，虚高预留把预算充裕的任务误拒成
    PARTIAL。超过最大合法单次调用时长（brain 流墙钟 1500s）+ 余量的在飞预留视为
    泄漏，惰性按 settle_error 语义结算（input 全额宁可高估——服务端已处理 prompt）。"""
    import os
    try:
        v = float(os.environ.get("SWARM_LEDGER_RESERVATION_TTL_S", "1800") or "1800")
        return v if v > 0 else 1800.0
    except ValueError:
        return 1800.0


def estimate_tokens_text(text: str) -> int:
    """复核 H4（阶段1）：预留/无 usage 结算共用的估算器（guard 与 output 估算同口径）。
    CJK ~1.7 char/tok（本仓主语言），其余 ~3.5 char/tok——原一刀切 //3 对 CJK 低估
    2 倍，把"宁可高估"的结算口径反转成低估。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return int(cjk / 1.7 + other / 3.5) + 1

_DDL = """
CREATE TABLE IF NOT EXISTS task_ledger (
    task_id          TEXT PRIMARY KEY,
    cloud_tokens_in  BIGINT NOT NULL DEFAULT 0,
    cloud_tokens_out BIGINT NOT NULL DEFAULT 0,
    local_tokens     BIGINT NOT NULL DEFAULT 0,
    llm_calls        BIGINT NOT NULL DEFAULT 0,
    wall_ms          BIGINT NOT NULL DEFAULT 0,
    replan_rounds    INTEGER NOT NULL DEFAULT 0,
    stage_spent      JSONB  NOT NULL DEFAULT '{}'::jsonb,
    budget_total     BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_FLUSH_INTERVAL = 15.0


class _Entry:
    """单任务账目（内部结构，持锁访问）。"""

    __slots__ = (
        "budget_total", "local_budget", "stage_ratios", "stage",
        "cloud_in", "cloud_out", "local", "llm_calls", "wall_ms",
        "replan_rounds", "stage_spent", "reserved_cloud", "reserved_local",
        "stage_reserved", "dirty", "seg_t0", "attached",
    )

    def __init__(self) -> None:
        self.budget_total = 0
        self.local_budget = 0
        self.stage_ratios: dict[str, float] = dict(DEFAULT_STAGE_RATIOS)
        self.stage: str | None = None
        self.cloud_in = 0
        self.cloud_out = 0
        self.local = 0
        self.llm_calls = 0
        self.wall_ms = 0
        self.replan_rounds = 0
        self.stage_spent: dict[str, int] = {}
        self.reserved_cloud = 0
        self.reserved_local = 0
        self.stage_reserved: dict[str, int] = {}
        self.dirty = False
        self.seg_t0: float | None = None  # 本执行段起点（attach 置位，detach 结算 wall_ms）
        # 复核 H1a（阶段1）：只有 attach 过的条目才允许落库——detach 后迟到的 worker
        # 结算会经 _get_or_create 再造条目，若可 flush 则全量 upsert 把 DB 真值覆盖成
        # 近空幽灵行（budget=0/stage_spent={}），毁掉 resume 延续。track-only 条目
        # （未 attach/幽灵）永不写 DB，flush 周期顺带清理其中无在飞预留者。
        self.attached = False


_lock = threading.Lock()
_entries: dict[str, _Entry] = {}
# reservation_id → (task_id, stage, kind, est_in, est_out, ts_monotonic)
_reservations: dict[str, tuple[str, str | None, str, int, int, float]] = {}
_flusher_started = False
_table_ready = False


def _reset_for_tests() -> None:
    """仅测试用：清空全部内存态。"""
    global _table_ready
    with _lock:
        _entries.clear()
        _reservations.clear()
        _table_ready = False


# ──────────────────────────── 落库通道（best-effort，镜像 usage_tracker）────────────────────────────

def _pool():
    from swarm.infra.db import sync_pool
    return sync_pool()


def _ensure_table() -> bool:
    global _table_ready
    if _table_ready:
        return True
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
        _table_ready = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ledger] 建表失败(稍后重试): %s", exc)
        return False


def _load_row(task_id: str) -> dict | None:
    """从 DB 读取该任务已结算账目（resume/重启延续）。失败返回 None（不阻断）。"""
    if not _ensure_table():
        return None
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cloud_tokens_in, cloud_tokens_out, local_tokens, llm_calls, "
                    "wall_ms, replan_rounds, stage_spent FROM task_ledger WHERE task_id=%s",
                    (task_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        raw_ss = row[6]
        ss = raw_ss if isinstance(raw_ss, dict) else json.loads(raw_ss or "{}")
        return {
            "cloud_tokens_in": int(row[0]), "cloud_tokens_out": int(row[1]),
            "local_tokens": int(row[2]), "llm_calls": int(row[3]),
            "wall_ms": int(row[4]), "replan_rounds": int(row[5]),
            "stage_spent": {str(k): int(v) for k, v in (ss or {}).items()},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ledger] 读取账本失败（按空账继续，宁可少限不误杀）: %s", exc)
        return None


def _flush_row(task_id: str, row: dict) -> bool:
    """全量 upsert 一行（账本是权威绝对值，非增量——与 usage_tracker 聚合增量不同）。"""
    if not _ensure_table():
        return False
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO task_ledger
                      (task_id, cloud_tokens_in, cloud_tokens_out, local_tokens,
                       llm_calls, wall_ms, replan_rounds, stage_spent, budget_total, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s, now())
                    ON CONFLICT (task_id) DO UPDATE SET
                      cloud_tokens_in  = EXCLUDED.cloud_tokens_in,
                      cloud_tokens_out = EXCLUDED.cloud_tokens_out,
                      local_tokens     = EXCLUDED.local_tokens,
                      llm_calls        = EXCLUDED.llm_calls,
                      wall_ms          = EXCLUDED.wall_ms,
                      replan_rounds    = EXCLUDED.replan_rounds,
                      stage_spent      = EXCLUDED.stage_spent,
                      budget_total     = EXCLUDED.budget_total,
                      updated_at       = now()
                    """,
                    (task_id, row["cloud_tokens_in"], row["cloud_tokens_out"],
                     row["local_tokens"], row["llm_calls"], row["wall_ms"],
                     row["replan_rounds"], json.dumps(row["stage_spent"]),
                     row.get("budget_total", 0)),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ledger] 落库失败(保 dirty 下次再试): %s", exc)
        return False


def flush() -> None:
    """把 dirty 任务全量写穿 DB（best-effort，永不抛）。

    复核 H1a（阶段1）：只 flush attached 条目——detach 后迟到结算再造的幽灵条目
    绝不落库（全量 upsert 会把 DB 真值覆盖成近空行）；顺带清理无在飞预留的幽灵/
    track-only 条目（内存卫生）。"""
    with _lock:
        dirty = {tid: _snapshot_locked(tid)
                 for tid, e in _entries.items() if e.dirty and e.attached}
        for tid in dirty:
            _entries[tid].dirty = False
        _live_res_tasks = {tid for (tid, *_r) in _reservations.values()}
        _ghosts = [tid for tid, e in _entries.items()
                   if not e.attached and tid not in _live_res_tasks]
        for tid in _ghosts:
            _entries.pop(tid, None)
    for tid, row in dirty.items():
        if not _flush_row(tid, row):
            with _lock:
                e = _entries.get(tid)
                if e is not None:
                    e.dirty = True  # 下轮重试


def _start_flusher() -> None:
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True

    def _loop() -> None:
        import time
        while True:
            time.sleep(_FLUSH_INTERVAL)
            try:
                flush()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_loop, name="task-ledger-flusher", daemon=True)
    t.start()
    atexit.register(flush)


# ──────────────────────────── 生命周期 ────────────────────────────

def attach(task_id: str, budget_total: int, *, local_budget: int = 0,
           stage_ratios: dict[str, float] | None = None) -> None:
    """任务执行段起点登记预算并从 DB 恢复已结算额度（resume/重启延续）。幂等：
    重复 attach 保留内存已结算值，仅更新预算/比例。"""
    if not task_id:
        return
    persisted = None
    with _lock:
        entry = _entries.get(task_id)
        fresh = entry is None
    if fresh:
        persisted = _load_row(task_id)  # DB I/O 放锁外
    with _lock:
        entry = _entries.get(task_id)
        if entry is None:
            entry = _Entry()
            _entries[task_id] = entry
            if persisted:
                entry.cloud_in = int(persisted.get("cloud_tokens_in") or 0)
                entry.cloud_out = int(persisted.get("cloud_tokens_out") or 0)
                entry.local = int(persisted.get("local_tokens") or 0)
                entry.llm_calls = int(persisted.get("llm_calls") or 0)
                entry.wall_ms = int(persisted.get("wall_ms") or 0)
                entry.replan_rounds = int(persisted.get("replan_rounds") or 0)
                entry.stage_spent = dict(persisted.get("stage_spent") or {})
                logger.info(
                    "[ledger] 任务 %s 账本自 DB 恢复：cloud=%d+%d local=%d calls=%d（延续不归零）",
                    task_id, entry.cloud_in, entry.cloud_out, entry.local, entry.llm_calls)
        entry.budget_total = max(0, int(budget_total or 0))
        entry.local_budget = max(0, int(local_budget or 0))
        if stage_ratios:
            entry.stage_ratios = {str(k): float(v) for k, v in stage_ratios.items()}
        if entry.seg_t0 is None:  # 每执行段一次（重复 attach 不重置段起点）
            import time as _time
            entry.seg_t0 = _time.monotonic()
        entry.attached = True  # H1a：唯一授予落库资格的地方
        entry.dirty = True
    _start_flusher()


def detach(task_id: str) -> None:
    """执行段结束：结算段墙钟 → 写穿 → 移出内存（终态账目仍在 DB 可查）。"""
    with _lock:
        e = _entries.get(task_id)
        if e is not None and e.seg_t0 is not None:
            import time as _time
            e.wall_ms += max(0, int((_time.monotonic() - e.seg_t0) * 1000))
            e.seg_t0 = None
            e.dirty = True
    try:
        flush()
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        _entries.pop(task_id, None)
        stale = [rid for rid, (tid, *_rest) in _reservations.items() if tid == task_id]
        for rid in stale:
            _reservations.pop(rid, None)


def _get_or_create(task_id: str) -> _Entry:
    entry = _entries.get(task_id)
    if entry is None:
        entry = _Entry()  # 未 attach → track-only（budget=0 不闸）
        _entries[task_id] = entry
    return entry


def set_budget(task_id: str, budget_total: int) -> None:
    """弹性预算更新（规划揭示子任务数后放宽，与墙钟 P1-B 同理）。"""
    if not task_id:
        return
    with _lock:
        e = _get_or_create(task_id)
        e.budget_total = max(0, int(budget_total or 0))
        e.dirty = True


def set_stage(task_id: str, stage: str | None) -> None:
    if not task_id:
        return
    with _lock:
        _get_or_create(task_id).stage = (stage or None)


def get_stage(task_id: str) -> str | None:
    with _lock:
        e = _entries.get(task_id)
        return e.stage if e else None


# ──────────────────────────── 预留-结算 ────────────────────────────

def _stage_limit_locked(e: _Entry, stage: str) -> int | None:
    ratio = e.stage_ratios.get(stage)
    if ratio is None or e.budget_total <= 0:
        return None
    return int(e.budget_total * float(ratio))


def _usage_locked(task_id: str, e: _Entry, *, stage: str | None = None,
                  stage_limit: int | None = None) -> dict:
    spent = e.cloud_in + e.cloud_out
    u = {
        "task_id": task_id,
        "total": spent,
        "real_recorded": spent,
        "limit_effective": e.budget_total,
        "reserved": e.reserved_cloud,
        "local_tokens": e.local,
        "llm_calls": e.llm_calls,
    }
    if stage is not None:
        u["stage"] = stage
        u["stage_spent"] = e.stage_spent.get(stage, 0)
        u["stage_limit"] = stage_limit
    return u


def _expire_stale_reservations_locked() -> None:
    """复核 F1（阶段1，CRITICAL）：泄漏预留惰性过期。被取消的 ainvoke() 不触发
    on_llm_error（langchain-core 1.4.0 实测），预留会挂到 detach——超 TTL 的在飞预留
    按 settle_error 语义就地结算（input 全额宁可高估，output 0），自愈不依赖回调管道。"""
    import time as _time
    now = _time.monotonic()
    ttl = _reservation_ttl_s()
    stale = [rid for rid, res in _reservations.items() if now - res[5] > ttl]
    for rid in stale:
        task_id, stage, kind, est_in, est_out, ts = _reservations.pop(rid)
        e = _get_or_create(task_id)
        _release_locked(e, stage, kind, max(0, est_in) + max(0, est_out))
        if kind == "cloud":
            e.cloud_in += max(0, est_in)
            if stage is not None and est_in > 0:
                e.stage_spent[stage] = e.stage_spent.get(stage, 0) + max(0, est_in)
        else:
            e.local += max(0, est_in)
        e.llm_calls += 1
        e.dirty = True
        logger.warning(
            "[ledger] 任务 %s 预留 %s 超 TTL(%.0fs) 未结算（疑被取消的调用泄漏）→ "
            "按 input 估算 %d 全额结算并释放（宁可高估）", task_id, rid[:8], ttl, est_in)


def reserve(task_id: str, *, est_in: int, est_out: int, kind: str = "cloud") -> str:
    """调用发起前预留额度。余额不足抛 TaskTokenLimitExceeded（拒绝发起，不烧钱）。

    云端：查总预算 + 当前 stage 子预算（在飞预留计入占用）。
    本地：独立闸值（local_budget，默认 0=不闸）；绝不消耗云端额度。
    """
    import time as _time
    est = max(0, int(est_in or 0)) + max(0, int(est_out or 0))
    k = (kind or "cloud").lower()
    rid = uuid.uuid4().hex
    with _lock:
        _expire_stale_reservations_locked()
        e = _get_or_create(task_id or "")
        if k == "cloud":
            if e.budget_total > 0:
                if e.cloud_in + e.cloud_out + e.reserved_cloud + est > e.budget_total:
                    raise TaskTokenLimitExceeded(_usage_locked(task_id, e))
                stage = e.stage
                if stage is not None:
                    limit = _stage_limit_locked(e, stage)
                    if limit is not None and (
                            e.stage_spent.get(stage, 0)
                            + e.stage_reserved.get(stage, 0) + est > limit):
                        raise TaskTokenLimitExceeded(
                            _usage_locked(task_id, e, stage=stage, stage_limit=limit))
            e.reserved_cloud += est
            if e.stage is not None:
                e.stage_reserved[e.stage] = e.stage_reserved.get(e.stage, 0) + est
        else:
            if e.local_budget > 0 and e.local + e.reserved_local + est > e.local_budget:
                u = _usage_locked(task_id, e)
                u["kind"] = "local"
                u["limit_effective"] = e.local_budget
                u["total"] = e.local
                raise TaskTokenLimitExceeded(u)
            e.reserved_local += est
        _reservations[rid] = (task_id or "", e.stage if k == "cloud" else None, k,
                              est_in, est_out, _time.monotonic())
    return rid


def _release_locked(e: _Entry, stage: str | None, kind: str, est_total: int) -> None:
    if kind == "cloud":
        e.reserved_cloud = max(0, e.reserved_cloud - est_total)
        if stage is not None:
            e.stage_reserved[stage] = max(0, e.stage_reserved.get(stage, 0) - est_total)
    else:
        e.reserved_local = max(0, e.reserved_local - est_total)


def _settle_impl(rid: str, in_tokens: int, out_tokens: int) -> None:
    with _lock:
        res = _reservations.pop(rid, None)
        if res is None:
            return  # 已结算/未知预留（幂等）
        task_id, stage, kind, est_in, est_out, _ts = res
        e = _get_or_create(task_id)
        _release_locked(e, stage, kind, max(0, est_in) + max(0, est_out))
        total = max(0, int(in_tokens)) + max(0, int(out_tokens))
        if kind == "cloud":
            e.cloud_in += max(0, int(in_tokens))
            e.cloud_out += max(0, int(out_tokens))
            if stage is not None and total > 0:
                e.stage_spent[stage] = e.stage_spent.get(stage, 0) + total
        else:
            e.local += total
        e.llm_calls += 1
        e.dirty = True


def settle(rid: str, *, real_in: int, real_out: int) -> None:
    """调用完成：按真实 usage 结算并释放预留。"""
    _settle_impl(rid, int(real_in or 0), int(real_out or 0))


def settle_error(rid: str, *, chunk_in: int, chunk_out: int) -> None:
    """error/中止路径结算（治 B4）：input 取 max(已收 chunk, 预留估算)——流被掐断
    input 在服务端已全额计费；output 取已收 chunk。宁可高估，绝不再 pop 丢弃。"""
    with _lock:
        res = _reservations.get(rid)
    if res is None:
        return
    _task_id, _stage, _kind, est_in, _est_out, _ts = res
    _settle_impl(rid, max(int(chunk_in or 0), int(est_in or 0)), int(chunk_out or 0))


def cancel(rid: str) -> None:
    """未发起即取消（预留释放、零结算、不计 llm_calls）。"""
    with _lock:
        res = _reservations.pop(rid, None)
        if res is None:
            return
        task_id, stage, kind, est_in, est_out, _ts = res
        e = _get_or_create(task_id)
        _release_locked(e, stage, kind, max(0, est_in) + max(0, est_out))


# ──────────────────────────── 查询 / 重试层接口 ────────────────────────────

def remaining(task_id: str) -> int:
    """云端剩余额度（budget=0 → 无限大语义，返回极大值）。"""
    with _lock:
        e = _entries.get(task_id)
        if e is None or e.budget_total <= 0:
            return 2**62
        return max(0, e.budget_total - e.cloud_in - e.cloud_out - e.reserved_cloud)


def ensure_budget(task_id: str, *, min_tokens: int = 0, stage: str | None = None) -> None:
    """重试层统一扣减入口（§九：batch attempt/主备/failure 阶梯/replan 发起前查余额）。
    余额（总 或 指定 stage 子预算）不足 min_tokens → 抛 TaskTokenLimitExceeded。"""
    if not task_id:
        return
    with _lock:
        _expire_stale_reservations_locked()
        e = _entries.get(task_id)
        if e is None or e.budget_total <= 0:
            return
        if (e.budget_total - e.cloud_in - e.cloud_out - e.reserved_cloud) < min_tokens:
            raise TaskTokenLimitExceeded(_usage_locked(task_id, e))
        st = stage or e.stage
        if st is not None:
            limit = _stage_limit_locked(e, st)
            if limit is not None and (
                    limit - e.stage_spent.get(st, 0) - e.stage_reserved.get(st, 0)
            ) < min_tokens:
                raise TaskTokenLimitExceeded(
                    _usage_locked(task_id, e, stage=st, stage_limit=limit))


def note_replan(task_id: str) -> None:
    if not task_id:
        return
    with _lock:
        e = _get_or_create(task_id)
        e.replan_rounds += 1
        e.dirty = True


def set_replan_rounds(task_id: str, rounds: int) -> None:
    """按 state.replan_count 绝对值同步（runner 事件循环消费节点输出，防重复累加；
    只增不减——resume 恢复的历史值不被更小的新轮覆盖）。"""
    if not task_id:
        return
    with _lock:
        e = _get_or_create(task_id)
        r = int(rounds or 0)
        if r > e.replan_rounds:
            e.replan_rounds = r
            e.dirty = True


def add_wall_ms(task_id: str, ms: int) -> None:
    if not task_id or not ms:
        return
    with _lock:
        e = _get_or_create(task_id)
        e.wall_ms += max(0, int(ms))
        e.dirty = True


def _snapshot_locked(task_id: str) -> dict:
    e = _entries[task_id]
    return {
        "cloud_tokens_in": e.cloud_in,
        "cloud_tokens_out": e.cloud_out,
        "local_tokens": e.local,
        "llm_calls": e.llm_calls,
        "wall_ms": e.wall_ms,
        "replan_rounds": e.replan_rounds,
        "stage_spent": dict(e.stage_spent),
        "budget_total": e.budget_total,
        "reserved": e.reserved_cloud,
        "stage": e.stage,
    }


def snapshot(task_id: str) -> dict:
    with _lock:
        if task_id not in _entries:
            return {}
        return _snapshot_locked(task_id)
