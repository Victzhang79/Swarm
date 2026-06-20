"""KB 内容探查脚本（Phase 0 摸底 — 只读）。

连上 RuoYi-E2E 项目的 KB，打印:
  ① norms 总数 + 抽样 10 条（看 tag/title/内容片段）
  ② 对几个 RuoYi 领域词做语义检索，打印返回 dict 的【确切字段名】+ 抽样

用法:
    .venv/bin/python test/benchmark/retrieval_quality/probe_kb.py
"""

from __future__ import annotations

import asyncio
import json

from swarm.config.settings import get_config
from swarm.knowledge.norms_store import NormsStore
from swarm.knowledge.semantic_index import SemanticIndexer

# RuoYi-E2E 项目（与 plan_quality / E2E 基线一致）
PROJECT_ID = "5d0e9db8-d000-40f6-8df9-a929ea3c4712"

PROBE_QUERIES = ["分页", "Mapper XML", "数据权限", "代码生成", "Shiro 权限注解", "定时任务"]


async def main() -> None:
    cfg = get_config()
    db = cfg.db
    kb = cfg.knowledge

    print("=" * 78)
    print(f"KB 探查 — project_id={PROJECT_ID}")
    print(f"hybrid_bm25_weight={kb.hybrid_bm25_weight} "
          f"retrieval_top_k={kb.retrieval_top_k} rerank_top_k={kb.rerank_top_k}")
    print("=" * 78)

    # ── ① norms ──────────────────────────────
    norms = NormsStore(db)
    await norms.connect()
    try:
        all_norms = await norms.get_all_norms(PROJECT_ID, active_only=True)
        print(f"\n[norms] 总数(active)={len(all_norms)}")
        tags: dict[str, int] = {}
        for n in all_norms:
            tags[n.get("tag", "?")] = tags.get(n.get("tag", "?"), 0) + 1
        print(f"[norms] tag 分布: {tags}")
        print("[norms] 抽样 10 条:")
        for n in all_norms[:10]:
            content = (n.get("content") or "")[:120].replace("\n", " ")
            print(f"  - id={n.get('id')} tag={n.get('tag')} prio={n.get('priority')} "
                  f"title={n.get('title')!r}")
            print(f"      content: {content}")
    finally:
        await norms.close()

    # ── ② 语义检索 ────────────────────────────
    sem = SemanticIndexer(db, kb)
    await sem.connect()
    try:
        for i, q in enumerate(PROBE_QUERIES):
            print(f"\n[semantic] query={q!r}")
            try:
                res = await sem.search_with_rerank(
                    PROJECT_ID, q,
                    retrieval_top_k=kb.retrieval_top_k,
                    rerank_top_k=5,
                    query_terms=[q],
                )
            except Exception as exc:  # noqa: BLE001
                print(f"   !! 检索失败: {type(exc).__name__}: {exc}")
                continue
            print(f"   返回 {len(res)} 条")
            if res and i == 0:
                print("   >>> 第一条返回 dict 的【确切字段名】:")
                print("   ", sorted(res[0].keys()))
            for r in res[:3]:
                content = (r.get("content") or "")[:90].replace("\n", " ")
                print(f"   - source={r.get('source')!r} chapter={r.get('chapter')!r} "
                      f"file_path={r.get('file_path')!r} score={r.get('score')} "
                      f"rerank={r.get('rerank_score')}")
                print(f"       content: {content}")
    finally:
        await sem.close()


if __name__ == "__main__":
    asyncio.run(main())
