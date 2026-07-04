"""LLM token 用量统计：按【云端 vs 本地】+【每项目】+ 总计累计到 PostgreSQL。

用户需求：看清实际烧了多少 token（云端大模型 vs 本地大模型分别多少、总计、每个项目多少），
在 WebUI 系统菜单展示，数据落库（非文件）统计。

设计要点：
- **不阻塞 LLM 热路径**：record() 只做内存累加（加锁，无 I/O）；后台守护线程每 FLUSH_INTERVAL 秒
  批量 upsert 落库；进程退出 atexit flush；API 读取前也会主动 flush（见 get_token_usage_stats）。
- **best-effort 不崩**：落库失败一律吞掉并把快照合并回缓冲（下次再试），统计永不拖垮/搞挂任务。
- **聚合表**（非 per-call 行，控行数）：PK(project_id, kind, provider_id, model)，运行累加。
  project_id='' 表示无项目归属（早期规划/探测等无 project 上下文的调用）。
"""
from __future__ import annotations

import atexit
import contextvars
import logging
import threading

logger = logging.getLogger(__name__)

# 聚合表 DDL（幂等，首次 flush 懒建，不依赖迁移是否跑过）
_DDL = """
CREATE TABLE IF NOT EXISTS llm_token_usage (
    project_id        TEXT   NOT NULL DEFAULT '',
    kind              TEXT   NOT NULL DEFAULT 'cloud',
    provider_id       TEXT   NOT NULL DEFAULT '',
    model             TEXT   NOT NULL DEFAULT '',
    prompt_tokens     BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens      BIGINT NOT NULL DEFAULT 0,
    call_count        BIGINT NOT NULL DEFAULT 0,
    total_duration_ms BIGINT NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, kind, provider_id, model)
)
"""
# 既有表幂等补列；total_duration_ms=终身累计耗时（保留作历史维度）
_MIGRATE = "ALTER TABLE llm_token_usage ADD COLUMN IF NOT EXISTS total_duration_ms BIGINT NOT NULL DEFAULT 0"

# 延迟样本表（带 ts）：平均延迟用【最近 N 次调用滑动平均】更具观察意义（全时段累计会被稀释/冻结，
# 反映不出此刻快慢）。单个累计和算不出窗口，故存逐次带时间戳样本，按 kind 取最近 N 次求均；定期裁剪控体积。
_LAT_DDL = """
CREATE TABLE IF NOT EXISTS llm_latency_sample (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL DEFAULT 'cloud',
    model       TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0
)
"""
_LAT_IDX = "CREATE INDEX IF NOT EXISTS idx_llm_latency_kind_id ON llm_latency_sample (kind, id DESC)"
_RECENT_N = 200     # 滑动窗口：每 kind 最近 N 次调用
_LAT_KEEP = 4000    # 裁剪上限：全表只留最近这么多行（多 kind × N 留足余量），控体积

_FLUSH_INTERVAL = 15.0  # 秒：后台批量落库节奏

# 内存缓冲：key=(project_id, kind, provider_id, model) → [prompt, completion, calls, duration_ms]
_buffer: dict[tuple[str, str, str, str], list[int]] = {}
_lat_buffer: list[tuple[str, str, int]] = []   # 待落库延迟样本 (kind, model, duration_ms)
_lock = threading.Lock()
_flusher_started = False
_table_ready = False

# B2 治本：per-task 真实 token 累计（内存，best-effort）。llm_token_usage 表按
# (project,kind,provider,model) 聚合、无 task_id，做高风险 schema 迁移不划算；单进程拓扑下
# 用 ContextVar 把"当前 task"归属到 record()——runner 在执行段起点 set_current_task，worker
# 子任务经 asyncio.gather 继承上下文，故其 LLM 用量也归属该 task。供 check_task_token_limit
# 用【真实累计】而非仅 len//4 估算判超，且不再只在 merge/dispatch 查一次。
_current_task_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "swarm_usage_task", default=None)
_task_token_totals: dict[str, int] = {}


def set_current_task(task_id: str | None) -> None:
    """把后续本上下文内的 LLM 用量归属到该 task（供 per-task 真实累计闸门）。"""
    _current_task_var.set(task_id or None)


def get_task_total_tokens(task_id: str) -> int:
    """该 task 本进程内已记账的真实 token 累计（prompt+completion）。无记录返回 0。"""
    if not task_id:
        return 0
    with _lock:
        return int(_task_token_totals.get(task_id, 0))


def clear_task_total(task_id: str) -> None:
    """任务执行段结束后清理 per-task 累计（防长进程内存累积）。"""
    if not task_id:
        return
    with _lock:
        _task_token_totals.pop(task_id, None)


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
                cur.execute(_MIGRATE)
                cur.execute(_LAT_DDL)
                cur.execute(_LAT_IDX)
        _table_ready = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[usage] 建表失败(稍后重试): %s", exc)
        return False


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

    t = threading.Thread(target=_loop, name="llm-usage-flusher", daemon=True)
    t.start()
    atexit.register(flush)


def record(project_id: str | None, kind: str, provider_id: str, model: str,
           prompt_tokens: int, completion_tokens: int, duration_ms: int = 0) -> None:
    """累加一次 LLM 调用的 token 用量 + 耗时（内存，O(1) 加锁，不落库不阻塞热路径）。"""
    try:
        p = int(prompt_tokens or 0)
        c = int(completion_tokens or 0)
        d = max(0, int(duration_ms or 0))
    except (TypeError, ValueError):
        return
    if p <= 0 and c <= 0:
        return  # 无 usage（如未启 stream_usage 或该调用没返回）→ 不记噪声
    k = (kind or "cloud").lower()
    key = (project_id or "", k, provider_id or "", model or "")
    with _lock:
        slot = _buffer.get(key)
        if slot is None:
            _buffer[key] = [p, c, 1, d]
        else:
            slot[0] += p
            slot[1] += c
            slot[2] += 1
            slot[3] += d
        if d > 0:
            _lat_buffer.append((k, model or "", d))  # 逐次样本，供最近 N 次滑动平均
        # B2：per-task 真实累计（若当前上下文归属了某 task）。
        _tid = _current_task_var.get()
        if _tid:
            _task_token_totals[_tid] = _task_token_totals.get(_tid, 0) + p + c
    _start_flusher()


_CLOUD_HOSTS = ("siliconflow", "openai.com", "dashscope", "cohere.ai", "cohere.com", "aliyuncs")


def _infer_kind(url: str) -> str:
    u = (url or "").lower()
    return "cloud" if any(h in u for h in _CLOUD_HOSTS) else "local"


def record_embed(model: str, url: str, prompt_tokens: int, *, op: str = "embed") -> None:
    """B3：知识检索 embed/rerank 记账（best-effort，永不抛）。op=embed|rerank。

    这些调用直连 HTTP、不经 _UsageRecorder，历史上完全不入 token 统计 → WebUI/DB 成本失真、
    B2 per-task 真实累计也漏这块。此处补记；kind 据 url 推断，经 ContextVar 归属当前 task。
    """
    try:
        record("", _infer_kind(url), f"knowledge-{op}", model or op,
               prompt_tokens=int(prompt_tokens or 0), completion_tokens=0)
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    """把内存缓冲批量 upsert 落库；失败则合并回缓冲下次再试（best-effort，永不抛）。"""
    with _lock:
        if not _buffer and not _lat_buffer:
            return
        snapshot = dict(_buffer)
        lat_snapshot = list(_lat_buffer)
        _buffer.clear()
        _lat_buffer.clear()
    if not _ensure_table():
        _remerge(snapshot, lat_snapshot)
        return
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                for (pid, kind, prov, model), (p, c, n, d) in snapshot.items():
                    cur.execute(
                        """
                        INSERT INTO llm_token_usage
                          (project_id, kind, provider_id, model,
                           prompt_tokens, completion_tokens, total_tokens, call_count,
                           total_duration_ms, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (project_id, kind, provider_id, model) DO UPDATE SET
                          prompt_tokens     = llm_token_usage.prompt_tokens     + EXCLUDED.prompt_tokens,
                          completion_tokens = llm_token_usage.completion_tokens + EXCLUDED.completion_tokens,
                          total_tokens      = llm_token_usage.total_tokens      + EXCLUDED.total_tokens,
                          call_count        = llm_token_usage.call_count        + EXCLUDED.call_count,
                          total_duration_ms = llm_token_usage.total_duration_ms + EXCLUDED.total_duration_ms,
                          updated_at        = now()
                        """,
                        (pid, kind, prov, model, p, c, p + c, n, d),
                    )
                # 延迟样本批量插入 + 裁剪旧行（控体积）
                if lat_snapshot:
                    cur.executemany(
                        "INSERT INTO llm_latency_sample (kind, model, duration_ms) VALUES (%s,%s,%s)",
                        lat_snapshot,
                    )
                    cur.execute(
                        "DELETE FROM llm_latency_sample WHERE id < "
                        "(SELECT COALESCE(min(id),0) FROM (SELECT id FROM llm_latency_sample "
                        "ORDER BY id DESC LIMIT %s) t)",
                        (_LAT_KEEP,),
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[usage] 落库失败，合并回缓冲重试: %s", exc)
        _remerge(snapshot, lat_snapshot)


def _remerge(snapshot: dict, lat_snapshot: list | None = None) -> None:
    with _lock:
        for key, (p, c, n, d) in snapshot.items():
            slot = _buffer.get(key)
            if slot is None:
                _buffer[key] = [p, c, n, d]
            else:
                slot[0] += p
                slot[1] += c
                slot[2] += n
                slot[3] += d
        if lat_snapshot:
            _lat_buffer.extend(lat_snapshot)


def get_token_usage_stats(conn_str: str | None = None) -> dict:
    """聚合统计：云端/本地分项 + 总计 + 每项目。读前先 flush，保证页面看到的是最新值。"""
    try:
        flush()
    except Exception:  # noqa: BLE001
        pass
    out = {
        "by_kind": {"cloud": _empty_bucket(), "local": _empty_bucket()},
        "grand_total": _empty_bucket(),
        "per_project": [],
    }
    if not _ensure_table():
        return out
    try:
        with _pool().connection() as conn:
            with conn.cursor() as cur:
                # 云端 vs 本地分项
                cur.execute(
                    "SELECT kind, COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
                    "COALESCE(SUM(total_tokens),0), COALESCE(SUM(call_count),0), "
                    "COALESCE(SUM(total_duration_ms),0) "
                    "FROM llm_token_usage GROUP BY kind"
                )
                for kind, p, c, t, n, dur in cur.fetchall():
                    b = _bucket(p, c, t, n, dur)
                    if kind in out["by_kind"]:
                        out["by_kind"][kind] = b
                    out["grand_total"] = _add(out["grand_total"], b)
                # 每项目（JOIN projects 取名）
                cur.execute(
                    "SELECT u.project_id, COALESCE(p.name, ''), "
                    "  COALESCE(SUM(CASE WHEN u.kind='cloud' THEN u.total_tokens ELSE 0 END),0), "
                    "  COALESCE(SUM(CASE WHEN u.kind='local' THEN u.total_tokens ELSE 0 END),0), "
                    "  COALESCE(SUM(u.total_tokens),0), COALESCE(SUM(u.call_count),0) "
                    "FROM llm_token_usage u LEFT JOIN projects p ON p.id = u.project_id "
                    "GROUP BY u.project_id, p.name ORDER BY SUM(u.total_tokens) DESC"
                )
                for pid, name, cloud_t, local_t, total_t, n in cur.fetchall():
                    out["per_project"].append({
                        "project_id": pid or "",
                        "project_name": name or ("(无项目归属)" if not pid else pid),
                        "cloud_tokens": int(cloud_t), "local_tokens": int(local_t),
                        "total_tokens": int(total_t), "call_count": int(n),
                    })
                # 平均延迟 = 每 kind【最近 N 次调用】滑动平均（覆盖累计值，更具观察意义）
                out["latency_window"] = _RECENT_N
                cur.execute(
                    "SELECT kind, COALESCE(SUM(duration_ms),0), COUNT(*) FROM ("
                    "  SELECT kind, duration_ms, row_number() OVER (PARTITION BY kind ORDER BY id DESC) rn"
                    "  FROM llm_latency_sample) t WHERE rn <= %s GROUP BY kind",
                    (_RECENT_N,),
                )
                _g_sum = _g_cnt = 0
                for kind, dsum, dcnt in cur.fetchall():
                    avg = int(int(dsum) / int(dcnt)) if dcnt else 0
                    if kind in out["by_kind"]:
                        out["by_kind"][kind]["avg_latency_ms"] = avg
                        out["by_kind"][kind]["recent_calls"] = int(dcnt)
                    _g_sum += int(dsum); _g_cnt += int(dcnt)
                out["grand_total"]["avg_latency_ms"] = int(_g_sum / _g_cnt) if _g_cnt else 0
                out["grand_total"]["recent_calls"] = _g_cnt
    except Exception as exc:  # noqa: BLE001
        logger.warning("[usage] 读取统计失败: %s", exc)
    return out


def _empty_bucket() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "call_count": 0, "total_duration_ms": 0, "avg_latency_ms": 0}


def _bucket(p, c, t, n, dur=0) -> dict:
    n_i = int(n)
    return {"prompt_tokens": int(p), "completion_tokens": int(c),
            "total_tokens": int(t), "call_count": n_i,
            "total_duration_ms": int(dur),
            "avg_latency_ms": int(int(dur) / n_i) if n_i > 0 else 0}


def _add(a: dict, b: dict) -> dict:
    out = {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in
           ("prompt_tokens", "completion_tokens", "total_tokens", "call_count", "total_duration_ms")}
    out["avg_latency_ms"] = int(out["total_duration_ms"] / out["call_count"]) if out["call_count"] > 0 else 0
    return out
