"""R41 确定性收尾器（round41 治本批）：PLAN 产出后、VALIDATE 前的零 LLM 修复。

round41 死因（task 3740e421 取证）：确定性修复能力齐备但接线互斥——
1. R40-1 孤儿文件挂靠只活在 maybe_file_plan_repair（task_plan is None 才走），
   P1 覆盖外科抢跑产出 plan 后缺件带病重验，最后一轮重试 9 秒原地复死：
   一个 `sql/alarm_notice_read.sql` 无 owner 杀掉 2h22min 的 90 子任务计划。
2. R39-4 脚手架注入只接线在 maybe_symbol_repair 内部：符号外科修不了硬符号
   如实回退时，注入随被丢弃的候选一起蒸发，全量重拆的新 plan 无人再注
   ——规则5 预警 11 模块贯穿三轮原样复现。

治本：无论哪条路径产出 plan（P1 外科 / R39-5 符号外科 / R40-1 缺件外科 /
LLM 全量重拆 / ULTRA 分批），进 VALIDATE 前统一跑本收尾器：
  ① inject_build_scaffold_subtasks —— 规则5 落空模块注入 pom 脚手架
     （unclaimed_contract_deps 只报"无人拥有该模块 pom"，结构上不与既有写者相撞）；
  ② attach_orphan_file_plan_entries —— file_plan 孤儿文件按同模块最深前缀挂靠，
     挂不上的 fail-open 留给 VALIDATE 如实打回（不越权猜挂）。
两步均幂等、确定性；VALIDATE 仍是权威判定，收尾器只消解机械可修缺口。
外科通道保留：它们额外提供"跳过 LLM 全量重拆"的成本优化，与收尾器不冲突。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _synthesize_orphan_subtasks(plan, orphans: list[str], file_plan,
                                project_path: str | None,
                                _task_description: str = "") -> dict[str, list[str]]:
    """R48-1：为挂靠无候选的 file_plan 孤儿按顶层模块新建子任务 → {sid: [paths]}。

    确定性、幂等；描述带上 file_plan 条目的 purpose（worker 拿到明示意图）；文件
    已存在于基线 → writable（改），否则 create_files（建）；同模块有脚手架子任务
    → depends_on（先有 pom 再写码）；parallel_groups 完整性守约（与 SCAFFOLD-INJECT
    同款接线；dispatch 纯 depends_on 驱动，组序无拓扑约束）。
    复核 F1：sid 撞既有 st-fileplan-* 时【收养进既有子任务】而非丢弃整组——
    continue 会让后到孤儿每轮原样打回=round48 死法换壳；复核 F2：组内按
    _MAX_FILES_PER_GROUP 预分片，绝不确定性造出超 validate 文件上限的子任务。
    """
    from pathlib import Path

    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskIntent
    _MAX_FILES_PER_GROUP = 6  # < validate 硬闸 12，且给 ELABORATE 按实体拆留余量
    # purpose 索引：归一路径 → file_plan 条目描述文本
    purpose: dict[str, str] = {}
    for e in (file_plan or []):
        if isinstance(e, dict) and e.get("path"):
            p = str(e["path"]).replace("\\", "/").strip("/")
            txt = str(e.get("purpose") or e.get("description") or "").strip()
            if txt:
                purpose[p] = txt
    groups: dict[str, list[str]] = {}
    for f in orphans:
        p = str(f).replace("\\", "/").lstrip("/")
        groups.setdefault(p.split("/", 1)[0] if "/" in p else "root", []).append(p)
    by_id = {st.id: st for st in plan.subtasks}
    created: dict[str, list[str]] = {}

    def _fmt(paths: list[str]) -> str:
        return "\n".join(
            f"- {p}" + (f"：{purpose[p]}" if p in purpose else "") for p in paths)

    for mod, paths in sorted(groups.items()):
        base_sid = f"st-fileplan-{mod}"
        # 复核 F1：既有承接子任务 → 收养（追加 scope+描述+验收），绝不丢弃
        if base_sid in by_id and by_id[base_sid].id.startswith("st-fileplan-"):
            host = by_id[base_sid]
            adopt = [p for p in paths
                     if p not in host.scope.create_files
                     and p not in host.scope.writable]
            if adopt:
                for p in adopt:
                    exists = bool(project_path) and (Path(project_path) / p).is_file()
                    (host.scope.writable if exists
                     else host.scope.create_files).append(p)
                    host.acceptance_criteria.append(
                        f"{p} 按 file_plan 用途实现并编译通过")
                host.description += "\n【file_plan 承接·追加】\n" + _fmt(adopt)
                created[base_sid] = adopt
            continue
        # 复核 F2：预分片防超限
        chunks = [paths[i:i + _MAX_FILES_PER_GROUP]
                  for i in range(0, len(paths), _MAX_FILES_PER_GROUP)]
        for ci, chunk in enumerate(chunks):
            sid = base_sid if ci == 0 else f"{base_sid}-{ci + 1}"
            if sid in by_id:
                continue
            writable, create = [], []
            for p in chunk:
                exists = bool(project_path) and (Path(project_path) / p).is_file()
                (writable if exists else create).append(p)
            st = SubTask(
                id=sid,
                description=(
                    f"【file_plan 承接】技术方案 file_plan 规划了以下 {mod} 模块文件，"
                    "但无子任务承接（收尾器确定性新建本子任务）。按各文件用途完整实现：\n"
                    + _fmt(chunk)),
                intent=TaskIntent.MODIFY if writable and not create else TaskIntent.CREATE,
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=writable, create_files=create),
                acceptance_criteria=[
                    f"{p} 按 file_plan 用途实现并编译通过" for p in chunk],
            )
            scaffold_sid = f"st-scaffold-{mod}"
            if scaffold_sid in by_id:
                st.depends_on.append(scaffold_sid)
            plan.subtasks.append(st)
            by_id[sid] = st
            if plan.parallel_groups:
                plan.parallel_groups.append([sid])
            created[sid] = chunk
    if created:
        logger.info(
            "[PLAN-FINISH] R48-1 孤儿无候选 → 确定性新建/收养承接子任务 %d 个: %s",
            len(created), {k: v[:3] for k, v in created.items()})
    return created


def finish_plan_deterministic(plan, file_plan, project_path: str | None = None,
                              task_description: str = "") -> dict:
    """对 plan 原地跑确定性收尾（脚手架注入 + 孤儿挂靠）。

    返回机读摘要 {scaffolds, orphans_attached, orphans_left}；任何一步异常
    fail-open（收尾器绝不拖垮 PLAN 节点，缺口留给 VALIDATE 权威打回）。
    接线位置（复核 F1 定案）：PLAN 后处理区【末端】（#6 覆盖单调化之后）——收尾器
    改 scope 会让 #6 的 scope 身份键漂移，放末端保证 #6 两侧比较的都是 LLM 原始
    scope；挂靠记录进 plan.finisher_attached 供 #6 跨轮对称剔除。注入的脚手架
    因此错过主 harness 循环 → 本函数自行 bootstrap（含 est_context_tokens 兜底）。
    """
    out: dict = {"scaffolds": [], "orphans_attached": 0, "orphans_left": []}
    if plan is None or not getattr(plan, "subtasks", None):
        return out
    try:
        from swarm.brain.contract_utils import inject_build_scaffold_subtasks
        injected = inject_build_scaffold_subtasks(plan, project_path)
        out["scaffolds"] = [e["module"] for e in injected]
        if injected:
            from swarm.brain.nodes.shared import bootstrap_subtask_harness
            _ids = {e["subtask_id"] for e in injected}
            for st in plan.subtasks:
                if st.id in _ids:
                    bootstrap_subtask_harness(st, task_description or st.description)
                    if not getattr(st, "est_context_tokens", 0):
                        st.est_context_tokens = 8000 + 6000  # TRIVIAL 基线+1 文件
    except Exception:  # noqa: BLE001 — fail-open，VALIDATE 兜底
        logger.warning("[PLAN-FINISH] 脚手架注入失败（fail-open）", exc_info=True)
    try:
        from swarm.brain.nodes.shared import _task_requests_tests
        from swarm.brain.plan_validator import normalized_file_plan_paths
        from swarm.brain.symbol_surgery import attach_orphan_file_plan_entries
        # 单子任务计划：validate_file_plan_ownership 同口径跳过（SIMPLE 面自证），
        # 收尾器不越权挂靠防 scope 膨胀。复核 F2：测试路径分母对称剔除——收尾器在
        # _strip_unrequested_tests 之后运行，挂测试文件=复活刚被剥掉的路径。
        paths = (normalized_file_plan_paths(
                     file_plan,
                     exclude_test_paths=not _task_requests_tests(task_description))
                 if len(plan.subtasks) > 1 else [])
        if paths:
            attached, left = attach_orphan_file_plan_entries(plan, paths)
            out["orphans_attached"] = attached
            out["orphans_left"] = left
            # ③ R48-1（round48 死因）：挂靠"无候选"（没有任何子任务碰该模块）时，
            # 旧行为留给 VALIDATE 打回——但 LLM 三轮都不按 issues 修（round48 实测
            # 单个 ruoyi-common 孤儿文件三连原样打回 → CONFIRM 拒绝杀整个计划）。
            # VALIDATE 提示语自己就写着治法"或为其新建子任务"——这一步是机械可做的，
            # 收尾器确定性闭环：按模块分组新建子任务承接（幂等、零 LLM）。
            if left:
                created = _synthesize_orphan_subtasks(
                    plan, left, file_plan, project_path, task_description)
                if created:
                    out["orphan_subtasks"] = created
                    _cset = {p for ids in created.values() for p in ids}
                    out["orphans_left"] = [p for p in left if p not in _cset]
                    from swarm.brain.nodes.shared import bootstrap_subtask_harness
                    for st in plan.subtasks:
                        if st.id in created:
                            bootstrap_subtask_harness(
                                st, task_description or st.description)
                            if not getattr(st, "est_context_tokens", 0):
                                # 复核 F3：MEDIUM 基线 50000 与主启发式同源（8000 是 TRIVIAL 档）
                                st.est_context_tokens = (
                                    50000 + 6000 * max(1, len(created[st.id])))
    except Exception:  # noqa: BLE001
        logger.warning("[PLAN-FINISH] 孤儿文件挂靠失败（fail-open）", exc_info=True)
    if (out["scaffolds"] or out["orphans_attached"] or out["orphans_left"]
            or out.get("orphan_subtasks")):
        logger.info(
            "[PLAN-FINISH] 确定性收尾：脚手架注入 %d 个模块%s；file_plan 孤儿挂靠 %d 个%s%s",
            len(out["scaffolds"]),
            f" {out['scaffolds']}" if out["scaffolds"] else "",
            out["orphans_attached"],
            f"；无候选新建承接子任务 {len(out['orphan_subtasks'])} 个"
            if out.get("orphan_subtasks") else "",
            f"（仍无候选 {len(out['orphans_left'])} 个: {out['orphans_left'][:5]}，"
            "留 VALIDATE 权威打回）" if out["orphans_left"] else "")
    return out
