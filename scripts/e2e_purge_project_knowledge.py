#!/usr/bin/env python3
"""R65-T2 E2E 轮前知识层外科清理 + 重建（跨轮幻影知识的治本入口）。

背景（round65 实锤）：E2E 每轮复用同 project_id，失败轮 worker 产物经子任务
DONE 后增量回灌进知识层（PG kb_* + Qdrant swarm_kb），而 e2e_reset_baseline.sh
只 git reset 磁盘 → 幻影模块知识跨轮堆叠（实测 Qdrant 540 个 alarm 点 /
PG 286 个 alarm 符号 / 20+ 种互相冲突的模块布局），污染下一轮规划检索
（round47 教训「LLM 毒残留会被当权威复制回去」的知识层变体）。

做什么（三步，fail-loud）：
  1. PG 外科清理：store.purge_project_knowledge —— 只清 kb_*（文件事实/行为层），
     保留 projects 注册行 / task_records 任务史 / mem_* 经验层（跨轮学习价值）。
  2. Qdrant 配对清理：SemanticIndexer.delete_by_project（与 PG 必须成对，
     只清一半会留下"向量指着不存在符号"的更隐蔽污染）。
  3. 重建基线知识：POST /api/projects/{id}/preprocess（走 API 复用 CAS 防重入守卫），
     轮询 status 直到就绪或超时。API 不可达时【大声】报 PREPROCESS_PENDING 并非零退出
     ——宁可挡住起跑也不静默让棕地接地退化。--skip-rebuild 可显式跳过第 3 步
     （例如 restart-api 之前的时序）。

用法（在 swarm/ 包目录）：
    .venv/bin/python scripts/e2e_purge_project_knowledge.py <project_id|project_path>
        [--skip-rebuild] [--api http://localhost:8420] [--rebuild-timeout 900]

positional 参数既可给 project_id（UUID），也可给项目磁盘路径（脚本经
store.get_project_by_path 解析——e2e_reset_baseline.sh 只知道路径）。

退出码：0=全链成功；1=项目解析失败/未捕获异常；2=★半清状态★（PG 已清但 Qdrant
失败——幂等，修好连接重跑即收敛，绝不可带此状态起跑）；3=重建触发/等待失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_SWARM_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SWARM_PKG.parent))


def _resolve_project_id(ident: str) -> str:
    from swarm.project import store

    p = Path(ident)
    if p.exists() and p.is_dir():
        proj = store.get_project_by_path(str(p.resolve()))
        if not proj:
            print(f"[purge-kb] ❌ 路径 {p.resolve()} 未注册为项目（projects 表无此 path）",
                  file=sys.stderr)
            sys.exit(1)
        return proj["id"]
    proj = store.get_project(ident)
    if not proj:
        print(f"[purge-kb] ❌ 项目 {ident} 不存在（既不是已注册路径也不是有效 project_id）",
              file=sys.stderr)
        sys.exit(1)
    return ident


async def _purge_qdrant(project_id: str) -> None:
    from swarm.knowledge.semantic_index import SemanticIndexer

    indexer = SemanticIndexer()
    await indexer.connect()
    try:
        await indexer.delete_by_project(project_id)
    finally:
        await indexer.close()


def _parse_preprocess_status(payload: dict) -> str:
    """从 GET /api/projects/{id}/preprocess/status 响应提取项目状态。

    ★复核 CRITICAL 整改★：端点真实响应形状是 {"project_status": ..., "progress": ...}
    （api/routers/project.py:434-437），终态词表是 preprocess.py 写入的 READY / ERROR。
    读错键 = 轮询永远不识别完成 → 每轮起跑烧满超时后硬退（狼来了闸）。
    本函数是唯一解析点，配 test_r65_knowledge_purge.py 的真实形状测试锁防漂移。"""
    return str((payload or {}).get("project_status") or "").upper()


def _trigger_rebuild(api: str, project_id: str, timeout_s: int) -> bool:
    import urllib.error
    import urllib.request

    token_file = Path.home() / ".swarm" / "cli_token"
    token = token_file.read_text().strip() if token_file.exists() else ""
    if not token:
        print("[purge-kb] ❌ 无 ~/.swarm/cli_token，无法触发 preprocess 重建", file=sys.stderr)
        return False

    def _req(method: str, url: str):
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        _req("POST", f"{api}/api/projects/{project_id}/preprocess")
    except urllib.error.HTTPError as exc:
        # 409/已在跑 = CAS 守卫拦下，也算触发成功（有人在重建就行）
        if exc.code != 409:
            print(f"[purge-kb] ❌ 触发 preprocess 失败: HTTP {exc.code}", file=sys.stderr)
            return False
    except Exception as exc:  # noqa: BLE001
        print(f"[purge-kb] ❌ API 不可达（{exc}）——PREPROCESS_PENDING：知识层已清空但未重建，"
              f"API 起来后必须手动 POST /api/projects/{project_id}/preprocess", file=sys.stderr)
        return False

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            st = _req("GET", f"{api}/api/projects/{project_id}/preprocess/status")
            status = _parse_preprocess_status(st)
            if status == "READY":
                print(f"[purge-kb] ✓ preprocess 重建完成（project_status={status}）")
                return True
            if status == "ERROR":
                print(f"[purge-kb] ❌ preprocess 重建失败: {st}", file=sys.stderr)
                return False
        except urllib.error.HTTPError as exc:
            # 猎手 (c) 整改：鉴权失效不是瞬时抖动——伪装成超时会把真因埋 900s
            if exc.code in (401, 403):
                print(f"[purge-kb] ❌ 轮询被拒 HTTP {exc.code}——token 过期/无效，"
                      "请刷新 ~/.swarm/cli_token（scripts/e2e_login.sh）后重跑", file=sys.stderr)
                return False
        except Exception:  # noqa: BLE001 — 轮询期瞬时网络抖动继续等，超时兜底
            pass
        time.sleep(10)
    print(f"[purge-kb] ❌ preprocess 重建 {timeout_s}s 未就绪（超时）", file=sys.stderr)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project", help="project_id（UUID）或已注册项目磁盘路径")
    ap.add_argument("--skip-rebuild", action="store_true",
                    help="只清不重建（例如 API 尚未重启的时序；调用方自负重建责任）")
    ap.add_argument("--api", default="http://localhost:8420")
    ap.add_argument("--rebuild-timeout", type=int, default=900)
    args = ap.parse_args()

    from swarm.config.settings import DatabaseConfig
    from swarm.project import store

    project_id = _resolve_project_id(args.project)
    _db = DatabaseConfig()
    from urllib.parse import urlparse
    _pg_parsed = urlparse(str(getattr(_db, "postgres_uri", "") or ""))
    # 只取 host:port（postgres_uri 含凭据，绝不整串打印）
    _pg_host = f"{_pg_parsed.hostname or '?'}:{_pg_parsed.port or '?'}"
    _qd_url = str(getattr(_db, "qdrant_url", "") or "?")
    counts = store.purge_project_knowledge(project_id)
    total = sum(counts.values())
    print(f"[purge-kb] ✓ PG 知识层已清（project={project_id} @ pg={_pg_host}）: {total} 行 "
          f"{json.dumps(counts, ensure_ascii=False)}")
    if not counts:
        # 猎手 (f) 整改：{} = 9 张 kb_ 表一张都不存在——与"连错库"不可区分，必须留痕
        print(f"[purge-kb] ⚠️ 0 张 kb_ 表存在于 pg={_pg_host}——若非全新部署，"
              "请核对连接目标是否正确（连错库的清理 = 假清理）", file=sys.stderr)

    try:
        asyncio.run(_purge_qdrant(project_id))
    except Exception as exc:  # noqa: BLE001
        # 猎手 (a)/复核 MED 整改：PG 已清但 Qdrant 没清 = 半清状态（向量指向已删符号，
        # 比不清更隐蔽）——必须给出恢复口径 + 与重建超时不同的退出码
        print(f"[purge-kb] ❌ PG 知识层已清但 Qdrant 清理失败（{exc}）——状态不一致：\n"
              f"  向量库({_qd_url})仍指向已删符号。两侧操作均幂等：修好 Qdrant 连接后"
              "重跑本脚本即可收敛，绝不可带半清状态起跑 E2E", file=sys.stderr)
        sys.exit(2)
    print(f"[purge-kb] ✓ Qdrant 向量已清（delete_by_project {project_id} @ {_qd_url}）")

    if args.skip_rebuild:
        print("[purge-kb] ⚠️ --skip-rebuild：知识层为空，起跑前必须完成 preprocess 重建"
              "（POST /api/projects/{id}/preprocess），否则棕地接地退化")
        return
    if not _trigger_rebuild(args.api, project_id, args.rebuild_timeout):
        sys.exit(3)


if __name__ == "__main__":
    main()
