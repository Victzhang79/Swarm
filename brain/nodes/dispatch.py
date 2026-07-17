"""brain/nodes/dispatch.py — dispatch/monitor 节点 + 安全审计（B1 批3 抽出）。

被测试 patch 的 _dispatch_to_worker 留在 __init__.py；本模块内对它的调用用
`nodes._dispatch_to_worker(...)` 模块限定，使 patch("swarm.brain.nodes._dispatch_to_worker") 命中。
"""

from __future__ import annotations

import asyncio
import logging
import os

from swarm.audit import audit
from swarm.brain.context_log import touch_context
from swarm.brain.nodes.shared import (
    _subtask_produced_expected,
    _worker_profile_prompt,
    completed_l1_ids,
    gather_cancel_on_error,
)
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.memory.sliding_window import PRIORITY_WORKER
from swarm.models.errors import TaskTokenLimitExceeded
from swarm.types import Confidence, SubTask, WorkerOutput

logger = logging.getLogger(__name__)

# H9 修复：fire-and-forget 后台任务强引用集合，防 asyncio 弱引用下被 GC。
_BG_TASKS: set = set()


def _inject_predecessor_context(to_dispatch, plan_obj, subtask_results: dict) -> None:
    """跨子任务上下文传递：把前序已完成依赖子任务的产出注入后序子任务的 context_snippets。

    B3 依赖序拆分（接口→实现→装配）场景：后序子任务（如 ServiceImpl）需要看到前序
    （Service 接口）实际定义了什么方法签名，才能正确实现。这里把前序产出的 diff 里【新增的
    方法/类签名行】抽出来，append 到后序子任务的 context_snippets，随 worker prompt 下发。
    无依赖 / 前序未完成 → no-op。幂等：重复注入同一前序会去重（按标记）。
    """
    import re

    for st in to_dispatch:
        deps = [d for d in (getattr(st, "depends_on", []) or []) if d in subtask_results]
        if not deps:
            continue
        marker = "\n\n🔗 前序子任务已产出（实现时对齐这些已定义的接口/签名）：\n"
        if marker.strip()[:10] in (getattr(st, "context_snippets", "") or ""):
            continue  # 已注入过
        pred_blocks: list[str] = []
        for dep_id in deps:
            out = subtask_results.get(dep_id)
            diff = getattr(out, "diff", "") or ""
            if not diff.strip():
                continue
            added = [ln[1:].strip() for ln in diff.split("\n")
                     if ln.startswith("+") and not ln.startswith("+++")]
            # ① 类/方法/接口签名
            sigs = []
            for s in added:
                if re.match(r"^(public|private|protected|class|interface|enum|def |func |function |export )", s) \
                   or re.search(r"\b[A-Za-z_]\w*\s*\([^)]*\)\s*[{;:]?\s*$", s):
                    sigs.append(s[:140])
            # ② API 端点契约（第二批-4：前端最需要——后端暴露了哪些 HTTP 端点）。
            # 抽 @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@RequestMapping(含路径) +
            # @RequestMapping 类级前缀；前端据此对齐 URL，减少前后端契约偏离。
            endpoints = []
            for s in added:
                m = re.search(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']', s)
                if m:
                    verb = m.group(1).upper()
                    endpoints.append(f"{verb if verb != 'REQUEST' else 'ANY'} {m.group(2)}")
                elif re.search(r"@(Get|Post|Put|Delete|Patch|Request)Mapping", s):
                    endpoints.append(s[:100])
            block_parts = []
            if endpoints:
                block_parts.append("API 端点（前端请对齐这些后端实际暴露的 URL/方法）:\n" + "\n".join(f"  {e}" for e in endpoints[:30]))
            if sigs:
                block_parts.append("方法/类签名:\n" + "\n".join(sigs[:30]))
            if block_parts:
                pred_blocks.append(f"### 来自 {dep_id} 的产出契约:\n" + "\n".join(block_parts))
        if pred_blocks:
            st.context_snippets = (getattr(st, "context_snippets", "") or "") + marker + "\n\n".join(pred_blocks)
            logger.info("[DISPATCH] 跨子任务上下文：已为 %s 注入 %d 个前序产出签名", st.id, len(pred_blocks))


def _reconcile_dispatch_accounts(plan_obj, to_dispatch) -> bool:
    """R65REPLAY-T4 派发侧兜底：规划收尾期对账后，执行期仍会有代码路径改写 scope
    账面（实锤=预算闸拆分对 scope 的 deep-copy 继承把 st-11-1 一个死等复制成 4 个；
    HANDLE_FAILURE 期亦有 create_files/ua 修补路径）。进 seed 闸前用同一 helper
    （plan_finisher 单一事实源）再对账一次。★覆盖是写者无关的（复核 F5）：helper
    每次从当前 plan 的 create_files/writable/depends_on 全量结构重算，不按写者点名
    ——新增任何账写者只要发生在下一次 dispatch 前即自动被覆盖，维护者勿加按写者
    的特判★。返回是否发生剔账（调用方据此显式 emit plan——in-place 变异靠
    checkpoint 捎带是本仓被禁模式）。幂等，失败 fail-open（seed 闸 BLOCKED+A2 兜底）。
    """
    try:
        from swarm.brain.plan_finisher import reconcile_upstream_account
        removed = reconcile_upstream_account(plan_obj)
    except Exception as _exc:  # noqa: BLE001 — 对账失败按未变更继续（可观测不静默）
        logger.warning("[DISPATCH] R65REPLAY-T4 上游账对账异常（跳过）: %s", _exc)
        return False
    if not removed:
        return False
    _batch_hit = sorted(set(removed) & {getattr(st, "id", "") for st in (to_dispatch or [])})
    logger.warning(
        "[DISPATCH] R65REPLAY-T4 派发前剔除幽灵上游账 %d 个子任务（本批命中 %s）"
        "——不剔则 seed 闸永久死等生产者在自己下游的产物", len(removed), _batch_hit[:6])
    return True


def _inject_upstream_products(to_dispatch, subtask_results: dict,
                              project_path: str | None = None) -> bool:
    """B1（round38c 主题B 治本）：派发时把【全体 L1 通过子任务】的产物文件清单注入
    本批子任务的 scope.upstream_artifacts——跨沙箱同步按"完成态 diff 全集"为源，
    替代四处声明驱动启发式（同父 A1 累积/同包补齐/readable 补传/模块树，全部够不着
    越包/跨父文件，st-13-2 八轮缺 VO 与 st-3 链漏传的根因，forensics_B1B2_code.md）。

    - 只取完成态（l1_passed）diff：被弃半成品绝不播毒（本地树 _ch∪_ut 方案的否决理由）。
    - 只取 ADDED/MODIFIED（_changes_from_diff）：删除态入清单会让 seed 闸误判
      missing → 假 BLOCKED。
    - 注入 upstream_artifacts 而非 readable：readable 全量渲染进 worker prompt
      （prompts.py:206），百文件级会撑爆上下文；upstream_artifacts 零 prompt 渲染，
      消费方=seed 闸（executor._precheck_upstream_seed）+ bootstrap 补传
      （executor_sync 扫 readable∪upstream_artifacts，本批配套改）。
    - BLOCKED 重派也经本注入（transient 回队 → 再进 dispatch）→ owner 完成后产物
      自然进来=输入真变化，"重开完成态 owner 的重 seed"由此覆盖，无需 failure 侧第三臂。
    - 对抗复核 CONFIRMED#2/#3 治理：①跨 diff 终态归并——后续完成者删除/重命名的旧路径
      从全集剔除（单 diff 内跳过 DELETED 不够：st-A 创建、st-B 删除时 A 的贡献残留 →
      seed 闸判缺 → 全体待派子任务假 BLOCKED，与 B2 指纹合谋成任务死刑）；②本地树
      存在性过滤——注入源语义=已 pull-back 落本地树的完成态产物，不存在即陈旧
      （merge 期清理/用户删除），绝不进 seed 闸。
    幂等去重；cap 可观测（SWARM_UPSTREAM_PRODUCTS_CAP，默认 800）。
    返回是否发生注入变更（调用方据此显式 emit plan——in-place 变异靠 checkpoint
    捎带是本仓被禁模式，重启即丢）。
    """
    product_files: list[str] = []
    _seen: set[str] = set()
    _removed: set[str] = set()
    for _sid, _out in (subtask_results or {}).items():
        if not getattr(_out, "l1_passed", False):
            continue
        for _chg in _changes_from_diff(getattr(_out, "diff", "") or ""):
            _ct = getattr(_chg, "change_type", None)
            _p = getattr(_chg, "file_path", "") or ""
            if not _p:
                continue
            if getattr(_ct, "value", _ct) == "deleted":
                _removed.add(_p)
                continue
            if _p not in _seen:
                _seen.add(_p)
                product_files.append(_p)
    # 跨 diff 终态归并：任一完成态 diff 删除过的路径从全集剔除（删除者必然后于创建者
    # 看到该文件，终态=已删）。
    if _removed:
        product_files = [p for p in product_files if p not in _removed]
    # 本地树存在性过滤：pull-back 未落盘/已被清理的路径绝不进 seed 闸（假 BLOCKED 源）。
    if project_path:
        try:
            from pathlib import Path as _Path
            _root = _Path(project_path)
            _before = len(product_files)
            product_files = [p for p in product_files if (_root / p).is_file()]
            if len(product_files) != _before:
                logger.info(
                    "[DISPATCH] B1 存在性过滤剔除 %d 个不在本地树的陈旧产物路径（防假 BLOCKED）",
                    _before - len(product_files))
        except Exception as _exc:  # noqa: BLE001 — 过滤失败按未过滤继续（seed 闸仍有 transient 兜底）
            logger.warning("[DISPATCH] B1 存在性过滤异常（跳过过滤）: %s", _exc)
    if not product_files:
        return False
    try:
        _cap = int(os.environ.get("SWARM_UPSTREAM_PRODUCTS_CAP", "800"))
    except (TypeError, ValueError):
        _cap = 800
    if len(product_files) > _cap:
        logger.warning(
            "[DISPATCH] B1 完成态产物全集 %d 个超 cap=%d，截断注入（超出部分不进 seed 闸，"
            "可调 SWARM_UPSTREAM_PRODUCTS_CAP）", len(product_files), _cap)
        product_files = product_files[:_cap]
    _changed = False
    for st in to_dispatch:
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        _own = set(getattr(sc, "writable", None) or []) | set(getattr(sc, "create_files", None) or [])
        _add = [p for p in product_files if p not in _own]
        if not _add:
            continue
        _merged = list(dict.fromkeys(list(getattr(sc, "upstream_artifacts", None) or []) + _add))
        if _merged != list(getattr(sc, "upstream_artifacts", None) or []):
            sc.upstream_artifacts = _merged
            _changed = True
            logger.info("[DISPATCH] B1 注入完成态产物全集 → %s upstream_artifacts=%d 个",
                        st.id, len(_merged))
    return _changed


def _c2_missing_symbols(subtask, shared_contract: dict, diff: str) -> list[str]:
    """C2（round38c 主题C）：本子任务【归属】的 shared_contract 符号中未出现在其 diff
    的清单。归属口径与 C1 validate_contract_ownership/verify D5 同源：子任务
    description/acceptance_criteria/contract 词边界命中，或 create_files/writable
    文件名按命名惯例等价命中（R42 复核 F1：C1 换 basename_owns_symbol 后 C2 沿用
    字面 <Symbol>.<ext> 会对 I 前缀/Impl 惯例文件永不认主=同口径不变量被击穿，
    该符号类的"owned 必须现身 diff"检查结构性失火）。纯函数可测。"""
    import json as _json
    import re as _re

    from swarm.brain.contract_utils import contract_symbols
    from swarm.brain.plan_validator import basename_owns_symbol
    symbols = contract_symbols(shared_contract or {})
    if not symbols:
        return []
    sc = getattr(subtask, "scope", None)
    corpus = (
        (getattr(subtask, "description", "") or "") + " "
        + " ".join(getattr(subtask, "acceptance_criteria", None) or [])
        + " " + _json.dumps(getattr(subtask, "contract", None) or {}, ensure_ascii=False)
    ).lower()
    stems = [str(f).replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
             for f in (list(getattr(sc, "create_files", None) or [])
                       + list(getattr(sc, "writable", None) or []))]
    # F2 消歧（R43 复核 F1 修订：先强度后长度，精确同名不被弱通道长符号抢走）
    from swarm.brain.plan_validator import basename_symbol_match
    _syms = [str(x) for x in symbols]
    file_owned: set[str] = set()
    for b in stems:
        best, best_key = None, None
        for y in _syms:
            t = basename_symbol_match(b, y)
            if t < 0:
                continue
            key = (t, -len(y))
            if best_key is None or key < best_key:
                best, best_key = y, key
        if best is not None:
            file_owned.add(best)
    dl = (diff or "").lower()
    missing: list[str] = []
    for sym in symbols:
        s = str(sym).lower()
        pat = _re.compile(r"(?<![0-9a-z_])" + _re.escape(s) + r"(?![0-9a-z_])")
        owned = str(sym) in file_owned or bool(pat.search(corpus))
        if owned and s not in dl:
            missing.append(str(sym))
    return sorted(missing)


def _changes_from_diff(diff: str) -> list:
    """从 unified diff 提取 FileChange（ADDED/MODIFIED/DELETED）。纯函数、可测。

    #4(a)：过去只 `^\\+\\+\\+ b/` 匹配 ADDED/MODIFIED，删除的 target 是 `+++ /dev/null` 匹配不到
    → 从不发 DELETED → 任务交付删除的文件索引永不清。这里额外识别 `--- a/X` + `+++ /dev/null`
    删除段发 DELETED，并去重（删除段的 `--- a/X` 不再被误当 MODIFIED 源端）。
    """
    import re

    from swarm.knowledge.updater import ChangeType, FileChange

    changes: list = []
    seen: set[str] = set()
    lines = diff.splitlines()
    # ① 删除段：--- a/X 紧跟 +++ /dev/null
    for i, line in enumerate(lines):
        if line.startswith("+++ /dev/null") and i > 0 and lines[i - 1].startswith("--- a/"):
            fpath = lines[i - 1][6:].strip()
            if fpath and fpath not in seen:
                seen.add(fpath)
                changes.append(FileChange(file_path=fpath, change_type=ChangeType.DELETED))
    # ② 新增/修改：+++ b/X（其源端是否 /dev/null 判 ADDED vs MODIFIED）
    for m in re.finditer(r"^\+\+\+ b/(\S+)", diff, re.MULTILINE):
        fpath = m.group(1)
        if fpath in seen:
            continue
        seg_start = m.start()
        prev = diff.rfind("--- ", 0, seg_start)
        is_new = prev >= 0 and "/dev/null" in diff[prev:seg_start]
        seen.add(fpath)
        changes.append(FileChange(
            file_path=fpath,
            change_type=ChangeType.ADDED if is_new else ChangeType.MODIFIED,
        ))
    return changes


def _feedback_to_knowledge(project_id: str, subtask, worker_output) -> None:
    """事实库回灌：子任务产出的变更文件 → knowledge updater 增量索引（best-effort，不阻塞）。

    补"worker 产出不回灌"的断裂——worker 在沙箱建文件 pull-back 到本地但不 git push，
    knowledge 收不到事件 → 事实库滞后 → 下个任务核验"文件在不在"误判。这里 DONE 后直接喂。
    只索引本子任务变更的少数文件（updater 本就支持文件级增量），轻量。异步 fire-and-forget。
    """
    if not project_id:
        return
    try:
        from swarm.knowledge.updater import UpdateEvent

        changes = _changes_from_diff(worker_output.diff or "")
        if not changes:
            return
        event = UpdateEvent(
            project_id=project_id,
            task_id=getattr(subtask, "id", None),
            changes=changes,
            metadata={"source": "worker_feedback"},
        )

        async def _run():
            try:
                from swarm.knowledge.hooks import enqueue_kb_update
                _eid = await enqueue_kb_update(event)
                # R54-3：成功与否必须在【真正入队之后】才播报——旧实现在 create_task 之后
                # 立刻打 "已入队" INFO，而失败走 logger.debug（INFO 级下不可见）→ 日志宣称
                # 成功、实际每次都 NotNullViolation，整条回灌链路死了都没人发现。
                logger.info("[DISPATCH] 事实库回灌：%d 个变更文件入队增量索引"
                            "（子任务 %s，event=%s）",
                            len(changes), getattr(subtask, "id", "?"), _eid)
            except Exception as exc:  # noqa: BLE001
                # fail-loud：回灌断了 = 知识库永远看不到产出 = 后续任务的事实核验基于陈旧世界。
                logger.warning("[DISPATCH] 知识库回灌入队失败（非致命但知识库将滞后）: %s", exc)

        # fire-and-forget：入队异步消费，不阻塞主流程
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # H9 修复：持任务强引用 + 完成时移除，防 asyncio 弱引用下被 GC 静默丢失回灌
            _t = loop.create_task(_run())
            _BG_TASKS.add(_t)
            _t.add_done_callback(_BG_TASKS.discard)
        except RuntimeError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DISPATCH] 知识库回灌跳过（非致命）: %s", exc)


def _enforce_dispatch_budget_gate(plan_obj, completed_ids, dispatch_remaining,
                                  max_concurrent, to_dispatch, abandoned=None,
                                  deprioritized=None, force_strong_out=None):
    """主干B 不变量·DISPATCH 闸门：派发前确保每个工作单元【文件数≤上界】（预防式治本）。

    根因：编排允许 oversized 子任务一路派到 worker，撞 900s 墙钟超时后才在恢复阶梯拆小——
    那是"检测-补偿"（先浪费一个超时再补救）。这里在派发前就用确定性按文件拆小
    （_split_oversized_by_files，非 LLM、快、收敛：每块≤MAX_FILES_PER_SUBTASK），就地改写
    plan + dispatch_remaining，再重选批次，使超预算的大块【根本不会进 worker】。

    收敛性：file-split 产出的子块文件数恒≤上界 → 下轮闸门对它们直接放行，幂等无循环。
    拆不动的（单文件巨核等）原样放行并显式 log（不静默截断），交超时阶梯兜底。
    返回 (plan_obj, dispatch_remaining, to_dispatch)（plan 可能被重建，调用方须回写 state）。
    """
    try:
        from swarm.brain.planning_nodes import (
            _oversized_by_files,
            _rebuild_plan,
            _remap_dependents_to_terminals,
            _split_oversized_by_files,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DISPATCH] 预算闸门：planning 辅助导入失败(跳过): %s", exc)
        return plan_obj, dispatch_remaining, to_dispatch

    oversized = [st for st in to_dispatch if _oversized_by_files(st)]
    if not oversized:
        return plan_obj, dispatch_remaining, to_dispatch

    new_subtasks = list(plan_obj.subtasks)
    remaining = list(dispatch_remaining)
    changed = False
    for st in oversized:
        try:
            children = _split_oversized_by_files(st)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DISPATCH] 预算闸门拆小 %s 异常(放行): %s", st.id, exc)
            continue
        if not children or len(children) <= 1:
            # 拆不动（单文件巨核）→ 原样放行，显式 log 不静默；交超时阶梯兜底。
            # E5（round38c 主题E）：同时直接标 force_strong——旧行为是先白烧 1×900s
            # 超时 + 2-3 轮重试（E1 修复前还全是同模型空转）才由 FINDING-12 补最强
            # 模型；大块既然确定拆不动，第一轮就上最强模型+boost（浪费型死点提前止损）。
            if force_strong_out is not None:
                force_strong_out[st.id] = True
            logger.warning(
                "[DISPATCH] 预算闸门：子任务 %s 超文件上界但确定性拆不动 → 原样派发"
                "（E5：首轮即 force_strong 最强模型；交超时强制拆小阶梯兜底，不静默）", st.id,
            )
            continue
        idx = next((i for i, x in enumerate(new_subtasks) if getattr(x, "id", None) == st.id), None)
        if idx is None:
            continue
        new_subtasks[idx:idx + 1] = children
        _remap_dependents_to_terminals(new_subtasks, st.id, children)
        if st.id in remaining:
            remaining.remove(st.id)
        for c in children:
            if c.id not in remaining:
                remaining.append(c.id)
        changed = True
        logger.info(
            "[DISPATCH] 预算闸门：派发前把超文件上界子任务 %s 确定性拆为 %d 小块 %s（不让大块进 worker）",
            st.id, len(children), [c.id for c in children],
        )
    if not changed:
        return plan_obj, dispatch_remaining, to_dispatch
    plan_obj = _rebuild_plan(plan_obj, new_subtasks)
    to_dispatch = plan_obj.get_dispatch_batch(
        completed_ids, remaining, max_concurrent, abandoned, deprioritized
    )
    return plan_obj, remaining, to_dispatch


def _has_hetero_alternate(difficulty) -> bool:
    """E1（round38c 主题E）：该难度路由是否存在异构备选（retry_alternate 兑现判据）。

    惰性 import 防循环依赖；判据异常按无备选处理（回退 boost 路径，不炸派发）。"""
    try:
        from swarm.models.router import ModelRouter
        d = difficulty.value if hasattr(difficulty, "value") else str(difficulty or "medium")
        return ModelRouter().has_alternate_for_subtask(d)
    except Exception:  # noqa: BLE001
        return False


def _select_pool_override(difficulty, idx, pool, use_alternate_effective, force_strong, routing_complex):
    """B10 治本（纯函数，便于测试）：并行池 model_override 决策。

    - force_strong（拒答/步数耗尽）或【complex 子任务】→ routing_complex(最强)，绕过轮转：
      别让轮转把硬子任务分到池里较弱模型(如 MiniMax)先降质跑一轮，再靠 force_strong 补救；
    - 否则池非空且非 alternate → 轮转 pool[idx%len]（同能力主力分散负载）；
    - 池空/alternate → None（按 difficulty 路由，保持既有兜底）。
    """
    d = str(getattr(difficulty, "value", None) or difficulty or "").lower()
    if force_strong or d in ("complex", "ultra"):
        return routing_complex
    if pool and not use_alternate_effective:
        return pool[idx % len(pool)]
    return None


async def dispatch(state: BrainState) -> dict:
    """DISPATCH 节点 — 将就绪的子任务派发给 Worker

    输入: plan, dispatch_remaining, subtask_results, knowledge_context
    输出: subtask_results, dispatch_remaining
    """
    plan_obj = state.get("plan")
    if plan_obj is None:
        logger.error("[DISPATCH] 没有执行计划")
        return {"dispatch_remaining": []}

    # CODEWALK 根因A纪律③：拷贝后再改（对齐 failure.py 惯例）。直接 mutate 共享 state
    # 对象会在 checkpoint 写盘前原地污染（LangGraph last-write-wins 通道的隐式契约）。
    subtask_results: dict = dict(state.get("subtask_results", {}))
    dispatch_remaining: list = list(state.get("dispatch_remaining", []))
    knowledge_context = state.get("knowledge_context", {})

    # 如果是首次进入 dispatch，初始化 dispatch_remaining
    if not dispatch_remaining and not subtask_results:
        dispatch_remaining = [t.id for t in plan_obj.subtasks]

    # audit #19：重入防护——dispatch_remaining 为空但仍有"既未完成、也不在 remaining"
    # 的子任务时（理论上不该出现，但 handle_failure/rebase 等异常路径可能造成），
    # 把这些遗漏子任务补回 remaining，避免直接跳过派发导致任务卡死/漏做。
    # 永久放弃集（阶梯三打桩/revert/连坐）：派发层须感知，否则被放弃子任务在
    # subtask_results.pop 后既不在 completed 也不在 remaining → 被孤儿回填当"漏做"复活，
    # 与 BLOCKED→replan 合谋成无界循环。读 state 单一事实源，全程排除。
    _abandoned = (set(state.get("abandoned_subtask_ids") or [])
                  | set(state.get("give_up_isolated_ids") or []))
    _completed = set(subtask_results.keys())
    if not dispatch_remaining:
        _orphaned = [t.id for t in plan_obj.subtasks
                     if t.id not in _completed and t.id not in _abandoned]
        if _orphaned:
            logger.warning(
                "[DISPATCH] 检测到 %d 个未完成但不在 remaining 的子任务，补回派发队列: %s",
                len(_orphaned), _orphaned,
            )
            dispatch_remaining = _orphaned

    # 治本 D23：依赖闸门只把【L1 通过】的结果当"已完成"。滞留的 L1 未通过失败结果不得满足
    # 下游 depends_on（否则上游从未真正成功、下游提前派发空烧）。消费 l1_passed 单一事实源。
    completed_ids = completed_l1_ids(subtask_results)
    config = get_config()
    max_concurrent = config.worker.max_concurrent

    # ── Fix F·dispatch 前进保证（解 head-of-line 死锁）：正在重试中的失败子任务
    # （subtask_retry_counts>0）在派发选批时【降优先级】——从未尝试的就绪生产者（新前沿）
    # 先占并发槽，失败撮只填剩余槽。失败撮常撞 900s 超时且早序恒就绪，旧逻辑让它们每批霸占
    # 全部槽位 → 生产者饿死 → 完成数冻结（15 轮无一到 MERGE 的真根因）。纯调度顺序改动，
    # 不改放弃集/熔断/有界重试语义（失败仍在 remaining、仍会被处理，只是不再独占槽）。
    _deprioritized = {
        sid for sid, c in (state.get("subtask_retry_counts") or {}).items()
        if isinstance(c, int) and c > 0
    }

    to_dispatch = plan_obj.get_dispatch_batch(
        completed_ids, dispatch_remaining, max_concurrent, _abandoned, _deprioritized
    )

    # ── 主干B 不变量·DISPATCH 预算闸门：超文件上界的工作单元在派发前确定性拆小，
    # 不让大块进 worker 撞 900s 超时（预防式治本，非超时后补偿）。plan 可能被重建 → 须回写 state。
    _plan_before_gate = plan_obj
    # E5：拆不动的大块由闸门直接标 force_strong（首轮即最强模型，省 1×900s 白烧）
    _gate_force_strong = dict(state.get("subtask_force_strong") or {})
    plan_obj, dispatch_remaining, to_dispatch = _enforce_dispatch_budget_gate(
        plan_obj, completed_ids, dispatch_remaining, max_concurrent, to_dispatch,
        _abandoned, _deprioritized, force_strong_out=_gate_force_strong
    )
    _gate_split = plan_obj is not _plan_before_gate
    # R65REPLAY-T4 派发侧兜底：执行期写者产生的幽灵 ua 进 seed 闸前剔除。
    # 复核 F4 定序裁决：必须在 B1 注入【之前】且【不得】在 B1 后重跑——B1 只注入
    # 完成态+本地存在的产物（零死等风险），事后重跑对账反而可能剔掉 B1 合法注入、
    # 让 bootstrap 补传漏文件。plan 侧幽灵在此清，B1 增量交存在性过滤把关。
    _acct_changed = _reconcile_dispatch_accounts(plan_obj, to_dispatch)

    # ── 跨子任务上下文传递(B3 配套)：把【前序已完成依赖子任务】的产出注入后序子任务，
    # 让后序看到前序定义的真实接口签名/新建符号，避免接口对不上（依赖序拆分场景）。
    _inject_predecessor_context(to_dispatch, plan_obj, subtask_results)
    # B1：完成态产物全集注入（seed 闸复明 + bootstrap 补传扩源，含 BLOCKED 重派重 seed）。
    # project_path 供存在性过滤（陈旧路径防假 BLOCKED，对抗复核 CONFIRMED#3）。
    from swarm.brain import nodes as _nodes_mod
    try:
        _b1_proj_path = _nodes_mod._get_project_path(state.get("project_id") or "")
    except Exception:  # noqa: BLE001 — 取不到路径按未过滤注入（seed 闸 transient 兜底）
        _b1_proj_path = None
    _ua_changed = _inject_upstream_products(to_dispatch, subtask_results, _b1_proj_path)

    logger.info(
        f"[DISPATCH] 派发 {len(to_dispatch)} 个子任务（并行批次） "
        f"(已完成={len(completed_ids)}, 剩余={len(dispatch_remaining)})"
    )

    if not to_dispatch:
        _empty: dict = {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
            # 治本 D24：早退分支也遵守 always-emit 契约，始终回填 failed_subtask_ids（空也回填）。
            # 否则 last-write-wins 通道残留上一轮（如 contract 失败）的 failed 列表 → monitor 误读
            # 残留失败再进 handle_failure，此时 verification_failure 已清 → 走常规能力阶梯误全量重跑。
            "failed_subtask_ids": list(state.get("failed_subtask_ids", [])),
        }
        if _gate_split or _acct_changed:
            _empty["plan"] = plan_obj  # 闸门拆小/剔账后须回写新 plan，否则变更只活在内存重启即丢
        return _empty

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", "")

    # 注：原先这里调用 SandboxPool(...).warmup(project_id) 做"预热"，但那是
    # 失效死代码——每次都 new 一个临时 SandboxPool，warmup 把沙箱塞进它的 _pool
    # 后实例即被 GC，远端沙箱却永不回收 → 每次 dispatch 必产生 1 个孤儿沙箱。
    # 而真正的 worker 走 executor 的 create 路径，从不 acquire 这个池。
    # 预热既无收益又泄漏，直接移除。如需预热，应由长生命周期的单例池统一管理。

    # 阶段3.9 H-F7/R-F1（CONFIRMED）：alternate 决策按子任务记账——全局 bool 在"失败撮
    # 被降优先级错开到后续批"（本函数下方 _deprioritized 正是这么做的）时，首批消费即清
    # 会把 alternate 路由送给无关新前沿、真正重试者反拿主力模型。
    _alt_map = dict(state.get("subtask_use_alternate") or {})
    shared_contract = state.get("shared_contract") or (
        plan_obj.shared_contract if plan_obj else {}
    )
    # 主力并行轮转：把本批子任务按序轮转分配到 worker_parallel_pool 里的本地主力，
    # 让两个能力相当的本地主力(如 Qwopus3.6-27B-v2/MiniMax)同时干、分散负载、产出更快。
    # 轮转不覆盖 alternate(失败重试)；池空则不轮转(按 difficulty 路由)。
    _pool = list(getattr(config.worker, "worker_parallel_pool", []) or [])
    # FINDING-12 + E5：闸门对拆不动大块的首轮 force_strong 标记已合并进 _gate_force_strong
    _force_strong = _gate_force_strong

    async def _run_one(subtask: SubTask, idx: int = 0) -> tuple[SubTask, WorkerOutput | Exception]:
        # A6：惰性导入破 nodes↔dispatch eager 循环依赖（_dispatch_to_worker 是留在 __init__ 的
        # 可 patch 有状态符号；调用时 nodes 已完成初始化，patch("swarm.brain.nodes.X") 仍命中）。
        from swarm.brain import nodes
        # FINDING-12：拒答/步数耗尽子任务强制走【最强模型】(routing_complex=40B 256k)，不走 alternate
        # 也不走轮转池——小模型 agent 循环不收敛，最强模型最能在步数内完成。
        _fs = bool(_force_strong.get(subtask.id))
        # E1（round38c 主题E 复核 CONFIRMED）：旧判据 len(_pool)==1 把「池长」当「无备选」
        # ——池 1 模型但 difficulty fallback 链有异构备选时，retry_alternate 被静默改写成
        # 同模型+boost，failure 侧「换备选」日志与实派模型永久不符（register #26：9 次换备
        # 全空转）。改按 router 真相：无异构备选才回退 boost。RUN10「单模型不降级」诉求由
        # 配置表达（fallback 链配空/全 primary 即单模型模式），机制不再焊死。
        _single = not _has_hetero_alternate(getattr(subtask, "difficulty", None))
        _sid_alt = bool(_alt_map.get(subtask.id))
        # 复核 C-4（CONFIRMED）：complex/ultra 子任务禁用 alternate——它们已派最强模型
        # （_select_pool_override 恒 routing_complex），任何「换备选」都是降级；且
        # _dispatch_to_worker 的 use_alternate 分支优先于 model_override，不禁用则
        # override 算出最强又被丢弃。complex 重试走 boost 路径（同模型+加步数）。
        _d = str(getattr(getattr(subtask, "difficulty", None), "value", None)
                 or getattr(subtask, "difficulty", None) or "").lower()
        # A2（round48c 实锤）：C-4"complex 已派最强故禁换备"在最强模型反复失败时
        # 反转为死刑——st-14-1×11/st-21×9 全烧同一主力，failure 五轮说换备全被此
        # 闸吞掉。同一子任务同模型深度重试（≥2 次）后，多样性 > 假定强度：放行异
        # 构换备。首次重试仍维持 C-4（最强模型偶发失败换弱备确是降级）。
        # 复核#4：retry_counts 会被签名剪枝重置（A2b 立项理由），并读终身账本
        _deep_retry = (
            int((state.get("subtask_retry_counts") or {}).get(subtask.id, 0)) >= 2
            or int((state.get("subtask_dispatch_totals") or {}).get(subtask.id, 0)) >= 3)
        _ua = (_sid_alt and not _fs and not _single
               and (_d not in ("complex", "ultra") or _deep_retry))
        # B10：override 决策抽为纯函数——force_strong 或 complex 子任务绕过轮转直取最强模型，
        # 否则池非空非 alternate 走轮转，池空/alternate 按 difficulty 路由。
        _override = _select_pool_override(
            getattr(subtask, "difficulty", None), idx, _pool, _ua, _fs,
            config.model.routing_complex,
        )
        # FINDING-12：拒答/步数耗尽子任务重试时，换最强模型 + 加步数(trivial 30→60)。
        # 只换 40B 不抬 recursion_limit，多步任务照样撞 `Sorry, need more steps`(RUN5 实证)。
        _boost = 0
        if _fs:
            _boost = 30
        elif _sid_alt and not _ua:
            # alternate 请求未兑现（无异构备选 / complex-ultra 禁降级）→ 同模型+加步数
            # 助收敛（C-4：条件从 `_single and _sid_alt` 放宽，complex 禁用面也要拿 boost）
            _boost = 30
        try:
            output = await nodes._dispatch_to_worker(
                subtask,
                knowledge_context,
                project_id=project_id,
                task_id=task_id,
                use_alternate=_ua,
                user_profile_prompt=_worker_profile_prompt(state),
                shared_contract=shared_contract,
                model_override=_override,
                recursion_boost=_boost,
                base_ref=state.get("base_commit"),  # 3rd#2：钉扎 base 透传到 worker
            )
            return subtask, output
        except TaskTokenLimitExceeded:
            # 复核 H2（阶段1）：预算耗尽是任务级事实，原样上抛（runner salvage→PARTIAL），
            # 不吞成 (subtask, exc) 普通失败对（那会烧重试阶梯 + 污染 L5 归因）。
            raise
        except Exception as e:
            return subtask, e

    # 复核 H1b（阶段1）：任务级异常逃逸时取消兄弟 worker（裸 gather 不取消，兄弟继续烧钱
    # 且结算落在 detach 后形成幽灵账目）。
    # E4+E10（阶段5）：先完成者立即回写 completed_subtasks（批内进度不冻结到最慢者）；
    # 异常时取消未完成兄弟并原样上抛（保 H1b 语义）。
    # R65D-T6①（round65d 8min stall）：批门闩→滚动补位——批内任一任务完成即查新就绪者
    # 补位（get_dispatch_batch 同源选批：依赖闸/放弃集/fresh 优先/扇出全继承）。护栏：
    # ★任一失败/异常立即停止补位、收批返回（HANDLE_FAILURE/R13-4 批间熔断节奏原样）★；
    # 单节点补位总量封顶 max_concurrent×SWARM_DISPATCH_ROLL_FACTOR（默认 3，0=关回旧
    # 批门闩）；超大块（_oversized_by_files）不滚动，留下轮节点级预算闸拆小。
    # 结果的语义处理（failed 记账/知识回灌）仍在收齐后统一做（顺序无关，不改行为）。
    import os as _os
    try:
        _roll_factor = int(_os.environ.get("SWARM_DISPATCH_ROLL_FACTOR", "3") or 0)
    except ValueError:
        # 猎手 LOW-MED：非法值绝不静默启用——运维想用 "off"/"false" 关滚动时必须看得见
        # 开关没生效（应急回退唯一合法值=字面 "0"）。
        logger.error(
            "[DISPATCH] SWARM_DISPATCH_ROLL_FACTOR 配置非法(%r)——回退默认 3（滚动开启）；"
            "关闭滚动请设字面 0",
            _os.environ.get("SWARM_DISPATCH_ROLL_FACTOR"))
        _roll_factor = 3
    _roll_budget = max_concurrent * max(0, _roll_factor)
    _base_done = len(completed_l1_ids(subtask_results))
    _spawned_ids = {st.id for st in to_dispatch}
    _next_idx = len(to_dispatch)
    _rolling_completed = set(completed_ids)
    _results_view = dict(subtask_results)   # 滚动候选的前序上下文注入用（含本批新产出）
    _any_bad = False
    _rolled: list[str] = []
    pending: set = {asyncio.ensure_future(_run_one(st, i))
                    for i, st in enumerate(to_dispatch)}
    outcomes = []
    try:
        while pending:
            _done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for _fut in _done:
                _st, _oc = _fut.result()   # TaskTokenLimitExceeded 原样上抛（H2 语义）
                outcomes.append((_st, _oc))
                if isinstance(_oc, WorkerOutput) and _oc.l1_passed:
                    _rolling_completed.add(_st.id)
                    _results_view[_st.id] = _oc
                    _base_done += 1
                    if task_id:
                        try:
                            from swarm.project import store as _store
                            await asyncio.to_thread(
                                _store.update_task, task_id,
                                completed_subtasks=_base_done)
                        except Exception:  # noqa: BLE001 — 进度回写是增益，绝不阻断派发
                            pass
                else:
                    _any_bad = True   # 失败/异常 → 停止补位，收批交失败处置
            if (_roll_budget <= 0 or _any_bad
                    or len(_rolled) >= _roll_budget):
                continue
            _slots = max_concurrent - len(pending)
            if _slots <= 0:
                continue
            _rem = [t for t in dispatch_remaining if t not in _spawned_ids]
            if not _rem:
                continue
            try:
                from swarm.brain.planning_nodes import _oversized_by_files
                _next_batch = [
                    st for st in plan_obj.get_dispatch_batch(
                        _rolling_completed, _rem, _slots,
                        _abandoned, _deprioritized)
                    if st.id not in _spawned_ids and not _oversized_by_files(st)
                ][: _roll_budget - len(_rolled)]
            except Exception:  # noqa: BLE001 — 补位是增益，选批异常绝不拖垮本批
                logger.warning("[DISPATCH] R65D-T6 滚动补位选批异常（本轮停止补位）",
                               exc_info=True)
                _next_batch = []
            for _nst in _next_batch:
                try:
                    _inject_predecessor_context([_nst], plan_obj, _results_view)
                    # 复核 MED：注入 mutate 了 scope.upstream_artifacts——并入 _ua_changed
                    # 让下方 plan 显式回写闸看到（B1 CONFIRMED#1 纪律：in-place 变异
                    # 靠 checkpoint 捎带是被禁模式）。
                    _ua_changed = _inject_upstream_products(
                        [_nst], _results_view, _b1_proj_path) or _ua_changed
                except Exception:  # noqa: BLE001 — 注入是增益
                    logger.debug("[DISPATCH] 滚动补位上下文注入跳过: %s", _nst.id)
                pending.add(asyncio.ensure_future(_run_one(_nst, _next_idx)))
                _next_idx += 1
                _spawned_ids.add(_nst.id)
                _rolled.append(_nst.id)
            if _next_batch:
                logger.info(
                    "[DISPATCH] R65D-T6 滚动补位 %d 个（累计 %d/上限 %d，批内零失败）: %s",
                    len(_next_batch), len(_rolled), _roll_budget,
                    [s.id for s in _next_batch])
    except BaseException:
        for _t in pending:
            if not _t.done():
                _t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise

    def _worker_batch_context() -> dict:
        lines: list[str] = []
        for st, oc in outcomes:
            if isinstance(oc, WorkerOutput):
                summary = (oc.summary or "")[:120]
                l1 = "通过" if oc.l1_passed else "未通过"
                lines.append(f"{st.id}: {summary} (L1={l1}, diff={len(oc.diff or '')} chars)")
            elif isinstance(oc, Exception):
                lines.append(f"{st.id}: 执行异常 — {str(oc)[:100]}")
        if not lines:
            return {}
        return touch_context(
            state,
            "worker_batch",
            "\n".join(lines),
            priority=PRIORITY_WORKER,
        )

    worker_ctx = _worker_batch_context()

    # 收集整批结果 —— 不再遇到首个失败就 return，避免丢弃同批已完成的兄弟结果
    failed_ids = list(state.get("failed_subtask_ids", []))
    for subtask, outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.error(f"[DISPATCH] 子任务 {subtask.id} 执行失败: {outcome}")
            subtask_results[subtask.id] = WorkerOutput(
                subtask_id=subtask.id,
                diff="",
                summary=f"执行失败: {outcome}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": str(outcome)},
            )
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)
            if subtask.id in dispatch_remaining:
                dispatch_remaining.remove(subtask.id)
            continue

        worker_output = outcome
        subtask_results[subtask.id] = worker_output
        if subtask.id in dispatch_remaining:
            dispatch_remaining.remove(subtask.id)
        logger.info(
            f"[DISPATCH] 子任务 {subtask.id} 完成 "
            f"(L1={'通过' if worker_output.l1_passed else '未通过'}, "
            f"diff={len(worker_output.diff or '')} chars)"
        )
        # ── C2（round38c 主题C，对抗复核 CONFIRMED 修正）：收货侧遵约确定性对账。
        # 初版按 st.contract 抽符号=恒空（主线 contract 形状是 {"input","output"} 描述，
        # contract_symbols 读不到）——改按【shared_contract 中归属本子任务的符号】
        # （C1 同口径：描述/AC/contract 词边界 + create_files/writable 文件名命中）对
        # diff 核对。warn 级+机读键（l1_details.contract_missing_symbols）：不硬拒
        # （符号可能已存在于既有文件），供 L2 D5/交付对账/journal 提前 8h 看见。
        if worker_output.l1_passed and shared_contract:
            try:
                _c2_missing = _c2_missing_symbols(
                    subtask, shared_contract, worker_output.diff or "")
                if _c2_missing:
                    worker_output.l1_details = {
                        **(worker_output.l1_details or {}),
                        "contract_missing_symbols": _c2_missing[:20],
                    }
                    logger.warning(
                        "[DISPATCH] C2 遵约对账：%s 归属的契约符号 %d 个未出现在其 diff：%s"
                        "（可能已存在于既有文件；L2 D5 将全局复核）",
                        subtask.id, len(_c2_missing), _c2_missing[:5])
            except Exception as _c2_exc:  # noqa: BLE001 — 对账是观测增强，绝不阻断收货
                logger.warning("[DISPATCH] C2 遵约对账异常（跳过）: %s", _c2_exc)
        # 治本 D01：成功判据 = L1 通过 且 产出符合【该 intent/scope 预期的变更形态】。
        # 旧判据 `_diff_has_changes`（只认 `+` 行）把 AUDIT（空 diff 合法）与纯删除子任务
        # （只有 `-` 行 + `+++ /dev/null`）结构性判失败 → 反复重试至 abandon、审计意图无成功终态。
        if not worker_output.l1_passed or not _subtask_produced_expected(worker_output, subtask):
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)
        else:
            # ★对抗复核 #3 治本★：子任务【重试后 L1 通过 + 有有效 diff】→ 从 failed_ids 移除。
            # 此前只追加不移除 → contract retry 保留的 failed_subtask_ids 里，已成功重跑的 ID 残留 →
            # after_monitor 优先看 failed_ids 又进 handle_failure，形成"已成功仍判失败"的空转直至
            # 误 escalate/撞 recursion_limit。移除后该子任务不再被误判失败。
            if subtask.id in failed_ids:
                failed_ids.remove(subtask.id)
            # 事实库回灌（补滞后断裂）：子任务 L1 通过 + 有改动 → 把变更文件喂 knowledge updater
            # 增量索引，让后续子任务/任务的事实核验能看到最新产出（worker 不 git push，否则知识库永远不知）。
            _feedback_to_knowledge(state.get("project_id", ""), subtask, worker_output)
            # E10（阶段5）：completed_subtasks 即时回写已上移到 as_completed 循环
            # （单个完成即回写，不等最慢兄弟），此处不再重复。

    result: dict = {
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        **worker_ctx,
    }
    if _gate_force_strong != (state.get("subtask_force_strong") or {}):
        # E5：闸门新标的拆不动大块 force_strong 必须显式 emit（in-place 捎带是被禁模式）
        result["subtask_force_strong"] = _gate_force_strong
    if _gate_split or _ua_changed or _acct_changed:
        # B1 对抗复核#1：注入/对账 mutate 了 scope.upstream_artifacts——in-place 变异靠
        # checkpoint 捎带是被禁模式（C9 同纪律），显式 emit 否则重启恢复即丢注入。
        result["plan"] = plan_obj
    # H3 修复：永远回填 failed_subtask_ids（空也回填）。state 无 reducer(last-write-wins)，
    # 若仅非空时返回，上一轮失败列表会残留 → gates 误拒真正成功的运行。
    result["failed_subtask_ids"] = failed_ids
    # 3.8 生命周期收敛 TOP-1 + 3.9 H-F7/R-F1 升级：alternate 标记【按子任务】消费——
    # 本批真派出的 sid 从表中清除（消费即清，防粘滞劫持路由）；未派出的（被降优先级
    # 错开的失败撮）保留给后续批。空批早退分支不发键=零消费零清（语义自洽，不漂移）。
    # R65D-T6 双复核 CRITICAL/HIGH（各自带复现）：派发账必须用【全 spawn 集】——
    # 只数初始批会让滚动补位者绕过 A2 终身派发硬熔断（round48c 11 连派死型复活面）
    # 且 alternate 标记不被消费（粘滞劫持路由回归）。_spawned_ids=初始批∪滚动补位。
    _dispatched_ids = set(_spawned_ids)
    result["subtask_use_alternate"] = {
        k: v for k, v in _alt_map.items() if k not in _dispatched_ids}
    # A2（round48c 实锤）：终身派发计数——subtask_retry_counts 按签名剪枝，scope
    # 加宽/replan 改签名即重置 → st-14-1 实跑 11 次（重试上限/配额/阶梯全被绕）。
    # 本表按【子任务 id】单调累积、绝不剪枝，handle_failure 据它做硬熔断兜底。
    _totals = dict(state.get("subtask_dispatch_totals") or {})
    for _tid in _dispatched_ids:
        _totals[_tid] = _totals.get(_tid, 0) + 1
    result["subtask_dispatch_totals"] = _totals
    return result


def monitor(state: BrainState) -> dict:
    """MONITOR 节点 — 监控执行进度，检查是否还有下游/有无失败

    输入: dispatch_remaining, subtask_results, failed_subtask_ids
    输出: 无状态变更，仅作为路由判断节点
    """
    dispatch_remaining = state.get("dispatch_remaining", [])
    subtask_results: dict = state.get("subtask_results", {})
    failed_ids = state.get("failed_subtask_ids", [])

    # R65C-T2 修⑤：三本账统一口径——旧行把滞留的 L1 失败结果也计成"已完成"
    # （round65c 排障时 已完成 在 5↔3 间跳动=两处口径不一的假象），且放弃数不可见。
    _aband_n = len(set(state.get("abandoned_subtask_ids") or [])
                   | set(state.get("give_up_isolated_ids") or []))
    logger.info(
        f"[MONITOR] 剩余={len(dispatch_remaining)}, "
        f"已完成(L1过)={len(completed_l1_ids(subtask_results))}, "
        f"失败={len(failed_ids)}, 放弃={_aband_n}"
    )

    # 此节点不做状态变更，仅用于条件路由
    return {}
