"""Brain 规划/恢复核心簇 — 从 nodes/__init__.py 抽出的连通分量（god-file 拆解 · 主线1）。

内容：卡死子任务【恢复阶梯】(retry→定点拆小→保 build 放弃) + B-2【pom/模块脚手架】簇
(_grant_module_pom_writable / _widen_scope_for_compile_repair / _local_tree_revert_subtask) +
其纯图/足迹/依赖闭包 helper。这些函数【就地修改 plan / 依赖闭包】，是 replan 死循环治本的核心。

硬约束（承接 nodes/__init__ 顶部拆解清单）：
  1. 本模块【禁止】eager import swarm.brain.nodes(__init__)——__init__ 会 eager import 本模块做
     re-export，反向 eager 会重建 A6 破掉的环。对 __init__ 内符号(_get_brain_llm)一律【函数内 lazy
     import】；对 planning_nodes 同样 lazy(planning_nodes 反过来 eager import __init__)。
  2. 所有函数经 __init__ re-export 保 `swarm.brain.nodes.X` 可寻址。__init__ 内的调用点(handle_failure/
     _handle_failure_impl)以【模块全局】(re-export 绑定)解析，patch(`swarm.brain.nodes.X`) 对其生效；
     但【本模块内部的同簇互调】(如 _give_up_preserve_build→_proj_path_from_state/_generate_compile_stub)
     在本模块命名空间解析——测试若要 patch 这些内部调用，patch 目标须为 `swarm.brain.nodes.planning_core.X`。
"""
from __future__ import annotations

import json
import logging

from pathlib import Path

from swarm.brain.state import BrainState
from swarm.brain.nodes.shared import _parse_json_from_llm
from swarm.types import Confidence, WorkerOutput

logger = logging.getLogger(__name__)


def _widen_scope_for_compile_repair(plan_obj, fid: str, details: dict) -> list[str]:
    """治本(RUN16 st-20 死循环)：子任务编译失败、但【根因在其 scope 之外】(模块 pom 缺依赖 /
    上游文件签名不符)→ 该子任务 scope 改不到那些文件 → 重试永远编不过 → 死循环。

    重试前把根因文件纳入该子任务 writable scope,让重试能真正修：
      1. 模块 pom.xml(从子任务文件推断 <module>/pom.xml)——治"缺依赖/包不存在"(报错只点症状文件、
         不点 pom,故无条件补模块 pom)。
      2. 编译错误输出里【点名的项目文件】(.java/.xml,去 /workspace/ 前缀)——治"上游接口缺方法/缺类"。
    仅在确实是编译失败时加宽,返回新增文件列表(空=未加宽)。pom 多写者由 normalize 串行化,安全。
    """
    if not plan_obj or not getattr(plan_obj, "subtasks", None) or not details:
        return []
    build_ok = details.get("l1_2_1_build_ok", details.get("l1_2_compile_ok"))
    build_out = str(details.get("build_output") or "")
    is_compile_fail = (build_ok is False) or ("COMPILATION" in build_out) or ("cannot find symbol" in build_out)
    if not is_compile_fail:
        return []
    st = next((s for s in plan_obj.subtasks if getattr(s, "id", None) == fid), None)
    scope = getattr(st, "scope", None) if st else None
    if not scope:
        return []
    import re as _re
    cur = set(getattr(scope, "writable", []) or []) | set(getattr(scope, "create_files", []) or [])
    add: set[str] = set()
    # 1) 模块 pom：从已 scope 文件的 "<module>/src/" 推断 <module>/pom.xml
    for f in cur:
        m = _re.match(r"(.+?)/src/", f.replace("\\", "/"))
        if m:
            add.add(f"{m.group(1)}/pom.xml")
    # 2) 编译报错点名的项目文件(绝对沙箱路径去 /workspace/ 前缀)
    for m in _re.finditer(r"/workspace/([\w./\-]+\.(?:java|xml))", build_out):
        add.add(m.group(1))
    new = sorted(f for f in add if f not in cur)
    if new and st is not None:
        scope.writable = list(getattr(scope, "writable", []) or []) + new
    return new


# ── P0-B/P1-D：scope 不可满足的编译失败（缺依赖/缺符号）识别 + 定向恢复（task f9e38dae）──
# 现场：st-24 用 RedisTemplate 但 ruoyi-alarm/pom.xml 没声明依赖、pom 又不在 st-24 scope →
# 原地重试 N 次必败（数学上不可满足）→ 耗尽配额 → 落全量 replan 清空 23 个完成态。治本：识别
# 这类"缺符号/缺依赖"失败，给失败子任务补其【模块 pom】写权 + 重置徒劳的重试计数，只重派失败
# 子任务（保留成功兄弟），让 worker 拿到编译错误 + pom 写权后真正补依赖，而非推倒重来。
# 仅保留【缺依赖/缺符号】的特异信号，杜绝 "does not exist"/"无法访问" 这类宽串误伤
# （会命中 "User does not exist"/"table does not exist"/Java 模块可见性 "cannot access" 等
# 非依赖失败 → 误授 pom 写权、空烧定向恢复配额）。各语言 javac/go/rustc/py/node 的缺包特征：
def mass_abandon_cap(plan_subtasks_n: int) -> int:
    """R65C-T2 连坐规模闸阈值——四个 _transitive_abandon 消费点的单一事实源。

    R65D-W2 猎手 CRITICAL：消费边下推把 depends_on 图织密（fixture +176 边）后，
    任何未设防的放弃路径（重试耗尽部分交付/T3 基线修复混批/自愈混批）都可能让单个
    高扇出生产者一笔放弃全场闭包——round65c「102/107 静默清盘→假全部完成」死型从
    旁门复活。阈值语义与 R65C-T2 修③一致：一次新增放弃超 max(10, 25%×计划) 不是
    剪枝而是计划覆灭，必须 escalate 人工，绝不静默。"""
    return max(10, int(plan_subtasks_n * 0.25))


def _transitive_abandon(subtasks: list, abandoned: set[str],
                        completed_ids: set[str] | None = None) -> set[str]:
    """传递放弃闭包：把【依赖任一已放弃子任务】的子任务也并入放弃集（缺依赖永远跑不了）。

    单一事实源，供 revert 连坐 / 部分交付 / 上游放弃短路三处共用，杜绝"只放弃直接失败者、
    漏掉依赖链下游"致下游永留 remaining 被反复重派的无界循环。返回闭包后的放弃集（原地不改入参）。
    R51-1（round51 三连误杀真因）：completed_ids 里的子任务【绝不入闭包】——它已经跑完了，
    "缺依赖跑不了"对历史不成立（C9 动态边在完成后才补上是常态）。旧行为把已完成者卷进
    闭包 → 调用方 pop 其 subtask_results = 已交付工作静默丢弃 + 完成计数倒退（D14）→
    看守 progress 高水位锁死误杀健康轮。与 types._is_ready 的 T5 先例（completed 优先于
    放弃集）同一原则。种子集内的已完成者同样剔除（fail-safe：完成的工作永不弃）。

    R65REPLAY-T1（回放 C 路反事实：消费边把闭包 15→72）：闭包【不穿透软序边】
    （types.edge_is_soft，readable 驱动消费、非 seed 构建输入）——生产者死了，
    "只想读它文件"的消费者仍可尝试（幻影 readable R49-2 运行期剔、L1 裁决），
    绝不整链陪葬；ua 构建输入/零交集结构边照旧硬传递。"""
    from swarm.types import edge_is_soft
    _done = completed_ids or set()
    _by_id = {s.id: s for s in subtasks}
    closed = {a for a in abandoned if a not in _done}
    _spared: set[str] = set()
    _changed = True
    while _changed:
        _changed = False
        for s in subtasks:
            if s.id in closed or s.id in _done:
                continue
            _hard_dead = False
            for d in (getattr(s, "depends_on", []) or []):
                if d not in closed:
                    continue
                if edge_is_soft(s, _by_id.get(d)):
                    _spared.add(s.id)
                else:
                    _hard_dead = True
                    break
            if _hard_dead:
                closed.add(s.id)
                _spared.discard(s.id)
                _changed = True
    _spared -= closed
    if _spared:
        # 复核 F3：软边豁免必须留痕——否则规模闸 escalate 率下降与"图本来就小"在日志
        # 里不可分辨，软化放走烂货时无从审计。
        logger.warning(
            "[TRANSITIVE-ABANDON] R65REPLAY-T1 软序边豁免 %d 个子任务免于连坐"
            "（其到死产者的边为 readable 驱动消费，非构建输入；越过后 L1 裁决）: %s",
            len(_spared), sorted(_spared)[:8])
    return closed


# 治本 C：流式 stall（模型服务并发拥塞，_DualTimeoutChatOpenAI 抛 TransientInfraError 的特征词）。
_STREAM_STALL_MARKERS = ("stream stall", "解码中途", "首 token(prefill)", "stream stall timeout")


def _has_stream_stall(subtask_results: dict, ids: list) -> bool:
    """失败详情里是否有【流式 stall】特征——据此给更长退避，让模型服务并发拥塞散去再重试。"""
    for fid in ids or []:
        out = (subtask_results or {}).get(fid)
        if isinstance(out, WorkerOutput):
            det, extra = (out.l1_details or {}), (out.summary or "")
        elif isinstance(out, dict):
            det, extra = (out.get("l1_details", {}) or {}), (out.get("summary", "") or "")
        else:
            det, extra = {}, ""
        try:
            blob = json.dumps(det, ensure_ascii=False) + extra
        except (TypeError, ValueError):
            blob = str(det) + extra
        if any(m in blob for m in _STREAM_STALL_MARKERS):
            return True
    return False


# 顶层不是【模块目录】的常见前缀——取模块名时跳过，避免把 src/test 误当模块（MEDIUM-1）。
_NON_MODULE_TOP = ("src", "test", "target", "build", "dist", "out", "node_modules")


def _module_of(files: list) -> str | None:
    """从文件路径列表取顶层【模块目录】（首个含 '/' 且首段不是 src/test 等的路径）。"""
    for f in files or []:
        if "/" in f:
            top = f.split("/", 1)[0]
            if top and top not in _NON_MODULE_TOP:
                return top
    return None


def _reaches(by_id: dict, start: str, target: str) -> bool:
    """start 是否经 depends_on 链（传递）到达 target——用于加边前防环（HIGH-4）。"""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        st = by_id.get(cur)
        if st is not None:
            stack.extend(getattr(st, "depends_on", []) or [])
    return False


def _add_dep_safe(by_id: dict, dependent: str, dep: str) -> bool:
    """给 dependent 加 depends_on=dep，带传递防环（dep 已传递依赖 dependent 则不加）。"""
    if dependent == dep:
        return False
    cur = by_id.get(dependent)
    if cur is None:
        return False
    existing = list(getattr(cur, "depends_on", []) or [])
    if dep in existing:
        return False
    if _reaches(by_id, dep, dependent):  # dep 已能到达 dependent → 加边会成环
        return False
    cur.depends_on = existing + [dep]
    return True


# ── 治本 A2：缺依赖确定性补全（据项目自身 pom 自证坐标，不靠小模型、不臆造） ──
def _proj_path_from_state(state) -> str | None:
    pid = state.get("project_id") if isinstance(state, dict) else None
    if not pid:
        return None
    try:
        from swarm.project import store as _store
        proj = _store.get_project(pid)
        return proj.get("path") if proj else None
    except Exception:  # noqa: BLE001
        return None


def _grant_module_pom_writable(plan_obj, failed_ids: list) -> dict:
    """给失败子任务补其模块 <module>/pom.xml 写权，返回 {sid: mod_pom} 已授权映射。

    让重试能真正改 pom 补依赖（原本 pom 不在 scope，重试再多也修不了）。同时让失败子任务
    depends_on【该 pom 的既有 owner】（HIGH-2）：owner 可能是已 DONE 的脚手架子任务，二者都写
    同一 pom，必须靠拓扑序让 owner 的 pom-create 在前、coder 的 pom-modify 在后，MERGE 才不冲突。
    """
    granted: dict = {}
    if plan_obj is None or not hasattr(plan_obj, "subtasks"):
        return granted
    subs = list(plan_obj.subtasks)
    by_id = {st.id: st for st in subs}
    for st in subs:
        if st.id not in failed_ids:
            continue
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        files = list(getattr(sc, "create_files", []) or []) + list(getattr(sc, "writable", []) or [])
        mod = _module_of(files)
        if not mod:
            continue
        mod_pom = f"{mod}/pom.xml"
        w = list(getattr(sc, "writable", []) or [])
        cf = list(getattr(sc, "create_files", []) or [])
        if mod_pom not in w and mod_pom not in cf:
            w.append(mod_pom)
            sc.writable = w
        granted[st.id] = mod_pom
        # 串到该 pom 的既有 owner 后面（owner = create/writable 含 mod_pom 的另一子任务）。
        owner = next(
            (
                o for o in subs
                if o.id != st.id and mod_pom in (
                    list(getattr(getattr(o, "scope", None), "create_files", []) or [])
                    + list(getattr(getattr(o, "scope", None), "writable", []) or [])
                )
            ),
            None,
        )
        if owner is not None:
            _add_dep_safe(by_id, st.id, owner.id)
    return granted


def _serialize_pom_writers(plan_obj, pom_by_id: dict,
                           exclude_ids: set | None = None) -> None:
    """同一模块 pom 的多个失败写者按 id 序串成依赖链，杜绝并发写同一 pom 争抢。

    传递防环（HIGH-4）：经 _add_dep_safe 检查传递可达性，不止看直接边。
    exclude_ids（D2 复核 CONFIRMED）：无产出放弃者（abandoned/give_up-revert，已不在
    subtask_results）绝不入链——_is_ready 对该类依赖永不就绪，入链=把刚授权重派的
    任务用自己新加的边永久扣死。give_up 打桩路有 l1_passed 产出在 completed 集，
    依赖它无害，不在此列。本批 members 不受 exclude 影响（全员即将重派）。
    """
    if plan_obj is None or not hasattr(plan_obj, "subtasks"):
        return
    by_id = {st.id: st for st in plan_obj.subtasks}
    _excl = set(exclude_ids or ())
    groups: dict = {}
    for sid, pom in pom_by_id.items():
        groups.setdefault(pom, []).append(sid)
    for _pom, members in groups.items():
        # D2（round38c 主题D）：跨批串链——旧实现只串【本批 granted】，而 failure 每次
        # handle_failure 只传当次授权者（round38c 16:39/17:06/17:40/18:11 四批独立授权）
        # → 批间写者零依赖边天然并发竞写=20:23/22:08 rebase 冲突来源。改按全 plan 该
        # pom 的【全体现任写者】（writable∪create_files 命中，减无产出放弃者）∪ 本批
        # 成链，历史批/原生写者一并纳入顺序边。_add_dep_safe 传递防环，重复边幂等。
        _all_writers = sorted(({
            st.id for st in plan_obj.subtasks
            if _pom in (list(getattr(getattr(st, "scope", None), "writable", None) or [])
                        + list(getattr(getattr(st, "scope", None), "create_files", None) or []))
        } - _excl) | set(members))
        for i in range(1, len(_all_writers)):
            _add_dep_safe(by_id, _all_writers[i], _all_writers[i - 1])


def _insert_module_order_edge(plan_obj, registrant_id: str, scaffold_id: str) -> bool:
    """round29 A(b)：插「注册后于脚手架」规范边 registrant.depends_on += scaffold_id。

    先删既有【反向直边】（scaffold.depends_on 含 registrant——正是 d37a52a3 的病边，删它本身
    就是规范化），再经 _add_dep_safe 传递防环加正边。返回 True=规范边已在位（新加或本就有）；
    False=无法安全成立（id 缺失/自指/删直边后仍存在间接反向依赖，插边会成环 → fail-safe 跳过）。
    """
    if plan_obj is None or registrant_id == scaffold_id:
        return False
    by_id = {st.id: st for st in getattr(plan_obj, "subtasks", []) or []}
    reg, scaf = by_id.get(registrant_id), by_id.get(scaffold_id)
    if reg is None or scaf is None:
        return False
    deps_scaf = list(getattr(scaf, "depends_on", []) or [])
    _removed_reverse = False
    if registrant_id in deps_scaf:
        deps_scaf.remove(registrant_id)   # 单一规范方向：删反向直边（不叠边防 2-cycle）
        scaf.depends_on = deps_scaf
        _removed_reverse = True
    if scaffold_id in (getattr(reg, "depends_on", []) or []):
        return True                        # 幂等：规范边已在位
    if _add_dep_safe(by_id, registrant_id, scaffold_id):
        return True
    if _removed_reverse:
        # 猎人#1 观测缺口：删了反向直边、正向边却因【独立的间接反向路径】加不上（数学上该
        # 间接路径仍强制同一偏序，删直边无害=冗余边），但 plan 发生了 mutate 必须留痕可回放。
        logger.warning(
            "[HANDLE_FAILURE] 序边规范化部分生效：已删 %s→%s 反向直边，但正向边因间接反向"
            "路径未插入（既有间接路径仍保序，删除的是冗余边）", scaffold_id, registrant_id,
        )
    return False                           # 间接反向依赖仍在（加边成环）→ 跳过交常规阶梯


async def _targeted_redecompose(state: BrainState, failed_id: str) -> dict | None:
    """卡死子任务恢复阶梯·阶梯二：把【多文件】卡死子任务【定点拆小】（复用 _resplit_subtask），
    保留成功兄弟、只重派拆出的小块。每子任务最多 1 次。

    工程依据：本地小模型卡在一个子任务，最常见是【子任务太大】（一个子任务又建 entity 又写
    service 又拼 controller，7 个文件）→ 拆小真有用。单/双文件拆不动 → 返回 None 交阶梯三。
    复用 elaborate 同款 plan 变异：换节点 + _remap_dependents 把下游 depends_on 重映射到子链尾。"""
    plan_obj = state.get("plan")
    if plan_obj is None:
        return None
    st = next((s for s in getattr(plan_obj, "subtasks", []) if s.id == failed_id), None)
    if st is None:
        return None
    rd_counts = dict(state.get("subtask_redecompose_count", {}))
    if rd_counts.get(failed_id, 0) >= 1:
        return None  # 已拆过一次 → 不再拆（防无限拆）
    sc = getattr(st, "scope", None)
    n_files = len(getattr(sc, "writable", []) or []) + len(getattr(sc, "create_files", []) or [])
    if n_files <= 2:
        return None  # 单/双文件拆不动 → 交阶梯三
    try:
        from swarm.brain.planning_nodes import (
            _context_budget,
            _oversized_by_files,
            _rebuild_plan,
            _remap_dependents_to_terminals,
            _resplit_subtask,
            _split_oversized_by_files,
        )
        budget = _context_budget()
        children = (
            _split_oversized_by_files(st) if _oversized_by_files(st)
            else await _resplit_subtask(st, state, budget)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[HANDLE_FAILURE] 阶梯二 定点拆小异常(跳过): %s", exc)
        return None
    if not children or len(children) <= 1:
        return None  # 拆不动 → 交阶梯三
    new_subtasks = list(plan_obj.subtasks)
    idx = next((i for i, x in enumerate(new_subtasks) if x.id == failed_id), None)
    if idx is None:
        return None
    new_subtasks[idx:idx + 1] = children
    _remap_dependents_to_terminals(new_subtasks, failed_id, children)
    new_plan = _rebuild_plan(plan_obj, new_subtasks)
    subtask_results = dict(state.get("subtask_results", {}))
    subtask_results.pop(failed_id, None)
    dispatch_remaining = list(state.get("dispatch_remaining", []))
    for c in children:
        if c.id not in dispatch_remaining:
            dispatch_remaining.append(c.id)
    rd_counts[failed_id] = rd_counts.get(failed_id, 0) + 1
    logger.info(
        "[HANDLE_FAILURE] 阶梯二：卡死子任务 %s 定点拆小为 %d 块 %s，保留成功兄弟、只重派小块（不全盘）",
        failed_id, len(children), [c.id for c in children],
    )
    return {
        "plan": new_plan,
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": [],
        "failure_strategy": "retry",
        "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
        "subtask_redecompose_count": rd_counts,
    }


# 主干B 治本：子任务【超时】= 工作单元对执行预算太大的确定性信号（非模型瞬时抖动）。
# 这类失败的【第一恢复动作】必须是【确定性拆小】，而不是先换模型重试同样的大块——
# round10 实证：大单实体 900s 超时，系统反复 retry/retry_alternate 同样的大块、拆小靠后，
# 磨到用户取消。locating/coding 超时都源于"要做的活超出一个 worker 一次能干完的量"，拆小真
# 有用；preparing 超时是沙箱基础设施（坏镜像/envd）非尺寸问题，交给瞬时/常规阶梯，不在此拆。
_TIMEOUT_OVERSIZE_MARKERS = ("timeout_in_coding", "timeout_in_locating", "timeout_in_verifying")


def _is_timeout_oversize_failure(out: object) -> bool:
    """子任务失败是否为【尺寸超预算】型超时（coding/locating）。preparing/infra 超时不算。"""
    if isinstance(out, WorkerOutput):
        details = out.l1_details or {}
    elif isinstance(out, dict):
        details = out.get("l1_details") or {}
    else:
        return False
    err = str(details.get("error", "") or "")
    return any(marker in err for marker in _TIMEOUT_OVERSIZE_MARKERS)


async def _redecompose_timeout_subtasks(
    state: BrainState, timeout_ids: list[str]
) -> dict | None:
    """主干B 不变量·超时→强制拆小作第一恢复动作。

    把本批所有【可拆】的尺寸超时子任务一次性定点拆小、重派小块，保留成功兄弟与其余失败。
    不可拆的（≤2 文件 / 已拆过 1 次）留在 failed_subtask_ids 交常规阶梯（换模型/升级），
    绝不在此清空——清空会让失败子任务以 l1_passed=False 残留在 subtask_results 里被
    `completed_ids = set(subtask_results.keys())` 当成"已完成"静默漏到 MERGE（silent-fail）。
    全都不可拆 → 返回 None，交常规阶梯。每子任务最多拆 1 次（subtask_redecompose_count 熔断）。
    """
    plan_obj = state.get("plan")
    if plan_obj is None or not timeout_ids:
        return None
    rd_counts = dict(state.get("subtask_redecompose_count", {}))
    try:
        from swarm.brain.planning_nodes import (
            _oversized_by_files,
            _rebuild_plan,
            _remap_dependents_to_terminals,
            _split_oversized_by_files,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[HANDLE_FAILURE] 超时拆小：planning 辅助导入失败(跳过): %s", exc)
        return None
    new_subtasks = list(plan_obj.subtasks)
    split_children: dict[str, list] = {}  # failed_id -> [children]
    for fid in timeout_ids:
        if rd_counts.get(fid, 0) >= 1:
            continue  # 已拆过一次 → 不再拆（防无限拆），交常规阶梯
        st = next((s for s in new_subtasks if getattr(s, "id", None) == fid), None)
        if st is None:
            continue
        # 本预占通道【纯确定性、零 LLM、先于策略】：仅对文件数超界(_oversized_by_files)的超时块
        # 用确定性按文件/层拆（_split_oversized_by_files）。文件数未超界的超时（3-4 文件/单文件大
        # token）确定性拆不动——【不在此调 LLM 拆】，留给常规阶梯 ladder-2(_targeted_redecompose
        # 的 LLM 辅助拆)处理，避免在"先于 LLM"的预占通道里偷偷起 LLM（评审 HIGH：守不变量、不重复 LLM 路径）。
        if not _oversized_by_files(st):
            continue
        try:
            children = _split_oversized_by_files(st)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[HANDLE_FAILURE] 超时拆小 %s 异常(跳过): %s", fid, exc)
            continue
        if not children or len(children) <= 1:
            continue  # 拆不动 → 交常规阶梯
        idx = next((i for i, x in enumerate(new_subtasks) if getattr(x, "id", None) == fid), None)
        if idx is None:
            continue
        new_subtasks[idx:idx + 1] = children
        _remap_dependents_to_terminals(new_subtasks, fid, children)
        rd_counts[fid] = rd_counts.get(fid, 0) + 1
        split_children[fid] = children
    if not split_children:
        return None  # 没有任何可拆的超时子任务 → 交常规阶梯
    new_plan = _rebuild_plan(plan_obj, new_subtasks)
    subtask_results = dict(state.get("subtask_results", {}))
    dispatch_remaining = list(state.get("dispatch_remaining", []))
    for fid, children in split_children.items():
        subtask_results.pop(fid, None)
        for c in children:
            if c.id not in dispatch_remaining:
                dispatch_remaining.append(c.id)
    # 未拆的失败（不可拆超时 + 本批其它非超时失败）留在 failed_subtask_ids → 下一轮 handle_failure
    # 走常规阶梯处理（绝不清空，否则被 completed_ids 静默吞掉）。
    all_failed = list(state.get("failed_subtask_ids", []))
    leftover = [fid for fid in all_failed if fid not in split_children]
    logger.info(
        "[HANDLE_FAILURE] 主干B·超时强制拆小（第一恢复动作）：拆小 %d 个尺寸超时子任务 %s，"
        "保留成功兄弟、只重派小块；%d 个不可拆/其它失败 %s 交常规阶梯",
        len(split_children), list(split_children.keys()), len(leftover), leftover,
    )
    return {
        "plan": new_plan,
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": leftover,
        "failure_strategy": "retry",
        "failure_escalated": False,  # 批4c：非 escalate 决策清历史粘滞标记（取证 CONFIRMED，见 DEVLOG）
        "subtask_redecompose_count": rd_counts,
        # state 无 reducer(last-write-wins)：显式清 verification_failure，防上轮验证态残留串到下轮路由。
        "verification_failure": None,
    }


def _subtask_footprint(st) -> list[str]:
    """子任务在【本地树】可能留下的文件足迹（writable ∪ create_files），归一为相对 posix 路径。"""
    sc = getattr(st, "scope", None)
    files = list(getattr(sc, "writable", []) or []) + list(getattr(sc, "create_files", []) or [])
    out: list[str] = []
    for f in files:
        rel = str(f).strip().lstrip("/")
        if rel and rel not in out:
            out.append(rel)
    return out


def _files_owned_by_completed(subtasks, subtask_results: dict, exclude_ids: set) -> set[str]:
    """【已完成(l1_passed)且保留】子任务的 writable∪create_files 归属集（归一化相对路径）。
    供 revert 窄守卫：放弃子任务清足迹时，绝不删这些【兄弟有效产物】。"""
    owned: set[str] = set()
    for s in subtasks:
        sid = getattr(s, "id", None)
        if sid in exclude_ids:
            continue
        out = subtask_results.get(sid)
        passed = (isinstance(out, WorkerOutput) and out.l1_passed) or (
            isinstance(out, dict) and out.get("l1_passed"))
        if not passed:
            continue
        sc = getattr(s, "scope", None)
        for f in (list(getattr(sc, "writable", []) or [])
                  + list(getattr(sc, "create_files", []) or [])):
            owned.add(str(f).replace("\\", "/").lstrip("./"))
    return owned


def _local_tree_revert_subtask(project_path: str | None, st, protected_files: set | None = None,
                               base_ref: str | None = None) -> dict:
    """卡死子任务恢复阶梯·阶梯三(revert)：把子任务在【本地树】的足迹清干净。

    3rd#2：已跟踪文件 checkout 回【钉扎 base】版（None→HEAD 零回归），与交付链其余站点同源——
    避免运行期 HEAD 漂移后把文件复位到与 merged_diff 基线不符的版本。

    protected_files（H-exec2 窄守卫，round21）：被【其它已完成子任务】拥有为有效产物的文件集——
    即便落在本子任务 footprint 内也【跳过删除/回退】，杜绝放弃时误删兄弟已落盘产物(footprint 与兄弟
    scope 重叠场景)。纯加性守卫，不重构 round15 红线的桩+级联恢复逻辑。

    必要性（第六轮 + L2 源码实证）：worker 的坏文件经 pull-back 已写回本地 project_path
    （新建文件为 untracked）。L2 `run_integration_review` 的 `_reset_worktree_to_head` 只
    reset【merged_diff 内】的文件——放弃子任务空 diff 被 merge 排除 → 其坏 untracked 文件
    不在 diff 内 → 不被 reset → 仍留本地树 → `mvn compile`/下游 bootstrap 仍会带上 → `-am`
    整 reactor 中毒。故放弃时必须【主动清本地树足迹】，build 才真能保住。

    - 已被 git 跟踪的文件 → `git checkout HEAD --`（还原提交版，撤销 pull-back 脏改动）。
    - 未跟踪（新建产物）→ 删除文件。
    通用：纯 git/文件操作，与语言无关。返回 {"reverted":[...], "removed":[...]}。"""
    result: dict = {"reverted": [], "removed": [], "revert_failed": [], "skipped_protected": []}
    if not project_path:
        return result
    import subprocess
    from swarm.git_base import resolve_base_ref
    _base = resolve_base_ref(base_ref)
    root = Path(project_path)
    if not (root / ".git").exists():
        return result
    _protected = protected_files or set()
    for rel in _subtask_footprint(st):
        # H-exec2 窄守卫：该 footprint 文件是【其它已完成子任务】的有效产物 → 跳过删除/回退。
        if str(rel).replace("\\", "/").lstrip("./") in _protected:
            result["skipped_protected"].append(rel)
            continue
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", rel],
                cwd=str(root), capture_output=True, text=True, timeout=10,
            ).returncode == 0
        except Exception:  # noqa: BLE001
            tracked = False
        if tracked:
            try:
                proc = subprocess.run(
                    ["git", "checkout", _base, "--", rel],
                    cwd=str(root), capture_output=True, text=True, timeout=20,
                )
                if proc.returncode == 0:
                    result["reverted"].append(rel)
                else:
                    # E2 治本：checkout rc!=0 = 文件【未】还原，脏改动仍留本地树 → 下游 mvn `-am`
                    # 整 reactor 仍会带上中毒。绝不能记 reverted 假装已清（否则"放弃保 build"静默失效、
                    # 上游误判足迹已净）。记 revert_failed + 可观测，让调用方/诊断看得见真状态。
                    result["revert_failed"].append(rel)
                    logger.warning(
                        "[revert] git checkout 失败(rc=%s) 未还原 %s，脏改动仍在本地树"
                        "（下游 build 可能仍中毒）: %s",
                        proc.returncode, rel, (proc.stderr or "").strip()[:200],
                    )
            except Exception as exc:  # noqa: BLE001
                result["revert_failed"].append(rel)
                logger.warning("[revert] git checkout %s 异常，未还原（脏改动仍在树）: %s", rel, exc)
        else:
            abs_f = root / rel
            try:
                if abs_f.is_file():
                    abs_f.unlink()
                    result["removed"].append(rel)
            except OSError as exc:
                # 对称硬化：unlink 失败 = 未跟踪坏文件仍留本地树，与 checkout rc!=0 同类（足迹未清
                # → 毒 -am）。同样记 revert_failed + 可观测，不静默吞。
                result["revert_failed"].append(rel)
                logger.warning(
                    "[revert] 删除未跟踪足迹 %s 失败，仍在本地树（下游 build 可能仍中毒）: %s", rel, exc)
    return result


def _git_diff_for_paths(project_path: str, rel_paths: list[str], base_ref: str | None = None) -> str:
    """据本地树现状为给定文件生成 unified diff（相对钉扎 base，3rd#2）。

    新建文件用 `git add -N`（intent-to-add）让 `git diff` 能产出新增内容；产出后 `git reset`
    撤销 intent-to-add（保留工作区文件本身）。通用、与语言无关。失败返回空串。
    base_ref=None → "HEAD"（零回归）；给定则相对钉扎 base，与 merge base_reader 同源对齐。"""
    import subprocess
    from swarm.git_base import resolve_base_ref
    _base = resolve_base_ref(base_ref)
    if not rel_paths:
        return ""
    try:
        subprocess.run(["git", "add", "-N", "--", *rel_paths],
                       cwd=project_path, capture_output=True, text=True, timeout=20)
        # ★D44 sibling 治本★：reset 撤销 intent-to-add 放 finally——diff 抛异常（超时等）
        # 落外层 except 返回 ""，裸写顺序下 reset 被跳过，占位残留真仓 index 污染
        # git status/stash 消费者（与 executor_sync._try_local_git_diff 同类同修）。
        try:
            proc = subprocess.run(["git", "diff", _base, "--", *rel_paths],
                                  cwd=project_path, capture_output=True, text=True, timeout=30)
        finally:
            subprocess.run(["git", "reset", "-q", "--", *rel_paths],
                           cwd=project_path, capture_output=True, text=True, timeout=20)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


async def _generate_compile_stub(
    state: BrainState, st, project_path: str | None,
    protected_files: set[str] | None = None,
    required_files: set[str] | None = None,
) -> str | None:
    """卡死子任务恢复阶梯·阶梯三(stub)：为【被依赖】的卡死子任务生成可编译桩。

    聚焦 LLM 调用：据 X 的描述/契约/目标文件，生成各文件的【可编译桩】——保留 public 类型/
    签名让下游编译通过，方法体一律抛 not-implemented（语言对应：Java
    `throw new UnsupportedOperationException(...)`、TS `throw new Error(...)`、Go `panic(...)` 等），
    绝不留半成品坏代码。语言无关（prompt 让模型按文件后缀产出对应语言桩）。

    写入本地树后用 git 生成 diff 作为 X 的 WorkerOutput.diff（merge 纳入、L2 验证其可编译）。
    任何环节失败（无 LLM/无 project_path/解析失败/空产出）→ 返回 None，调用方回退 revert。
    桩可编译性的最终校验由下游 L2 全量编译兜底（桩编不过 → L2 失败 → 熔断升级，有界）。"""
    if not project_path:
        return None
    footprint = _subtask_footprint(st)
    if not footprint:
        return None
    # 只为【会产出代码的源文件】打桩（排除 pom/配置/资源等非代码足迹，避免乱改构建文件）。
    # R65C-T3 例外：下游 upstream_artifacts 明确声明的产物（含非代码，如模块构建清单）
    # 是种子闸的硬要求——缺一个下游必 BLOCKED 永堵，桩必须覆盖，故对声明项让路。
    _CODE_EXT = (".java", ".kt", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".cs", ".scala")
    code_files = [f for f in footprint if f.lower().endswith(_CODE_EXT)]
    _required = {f for f in (required_files or set()) if f in footprint}
    _req_extra = sorted(_required - set(code_files))  # 声明的非代码产物
    if not code_files and not _req_extra:
        return None
    # lazy import：_get_brain_llm 定义在 nodes/__init__（本模块被其 eager import 做 re-export，
    # 不可反向 eager import，否则重建 A6 环）；call-time import 也让 patch(nodes._get_brain_llm) 生效。
    # 放在下面 try 之外——ImportError 属编程错误(符号被删/改名)，应显式抛出，绝不能与 LLM 瞬时失败
    # 一起被 DEBUG 静默吞掉致全体桩生成静默降级为 revert/放弃（silent-failure-hunter MEDIUM）。
    from swarm.brain.nodes import _get_brain_llm
    try:
        llm = _get_brain_llm()
        contract = getattr(st, "contract", None)
        prompt = (
            "一个子任务多次实现失败、需被放弃，但有【下游子任务依赖它】。请为它生成"
            "【可编译的占位桩(stub)】，使下游能编译通过，而非半成品坏代码。严格要求：\n"
            "1. 保留每个文件应有的 public 类型/接口/方法签名（据描述与契约推断）。\n"
            "2. 所有方法体一律只抛“未实现”异常（按文件语言：.java→"
            "`throw new UnsupportedOperationException(\"TODO: 子任务未完成\");`；.ts/.js→"
            "`throw new Error(\"TODO: not implemented\");`；.go→`panic(\"TODO: not implemented\")`；"
            ".py→`raise NotImplementedError(...)`；其它语言用其惯用未实现抛错）。\n"
            "3. 桩必须能通过编译（import/包声明/类型完整），绝不留语法错误或未解析符号。\n"
            "4. 仅输出 JSON：{\"files\": {\"<相对路径>\": \"<完整文件内容>\"}}，不要解释。\n"
            + (("5. 【下游硬依赖清单】以下文件是下游子任务声明依赖的产物，无论类型必须"
                "全部产出：非代码文件（构建清单/配置等）给出该类型的最小合法完整内容"
                "（如构建清单坐标必须完整可被构建工具解析），绝不给空文件或占位注释：\n"
                f"{_req_extra}\n") if _req_extra else "")
            + "\n"
            f"子任务描述：{getattr(st, 'description', '')}\n"
            f"契约：{json.dumps(contract, ensure_ascii=False) if contract else '无'}\n"
            f"需打桩的文件：{sorted(set(code_files) | _required)}\n"
        )
        response = await llm.ainvoke([
            {"role": "system", "content": "你是资深工程师，生成最小可编译占位桩。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        parsed = _parse_json_from_llm(response.content)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[阶梯三·桩] LLM 生成异常 → 回退 revert: %s", exc)
        return None
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, dict) or not files:
        return None
    root = Path(project_path)
    _allowed = set(code_files) | _required
    written: list[str] = []
    for rel, content in files.items():
        rel_norm = str(rel).strip().lstrip("/")
        if not rel_norm or rel_norm not in _allowed or not isinstance(content, str) or not content.strip():
            continue  # 只接受落在 X 足迹内的代码文件/下游声明产物，杜绝越权写其它路径
        abs_f = root / rel_norm
        try:
            abs_f.parent.mkdir(parents=True, exist_ok=True)
            abs_f.write_text(content, encoding="utf-8")
            written.append(rel_norm)
        except OSError as exc:
            logger.debug("[阶梯三·桩] 写文件失败 %s: %s", rel_norm, exc)
    if not written:
        return None
    # R65C-T3 完备性闸：下游声明的 provenance 有缺 → 桩不完整（下游种子闸必 BLOCKED
    # 永堵，#53 修①后还会反复撞闸烧失败预算）→ 清理已写半桩回退 revert（诚实连坐），
    # 绝不产出 settled-with-product 的假桩。
    _missing_req = _required - set(written)
    if _missing_req:
        logger.warning(
            "[阶梯三·桩] 子任务 %s 桩缺下游声明的 provenance %s（LLM 未产出/内容为空）"
            "——桩不完整判失败，清理半桩回退 revert（下游诚实连坐，绝不留永堵假桩）",
            getattr(st, "id", "?"), sorted(_missing_req))
        _rev = _local_tree_revert_subtask(project_path, st, protected_files=protected_files or set(),
                                          base_ref=state.get("base_commit"))
        if _rev.get("revert_failed"):
            # 猎手 F1：清理失败=半桩仍在树上（会毒 build 且零 provenance）——硬告警留痕；
            # 调用方 revert 路会对同足迹再清一次，仍失败则记入其 l1_details.revert_failed。
            logger.error(
                "[阶梯三·桩] 子任务 %s 半桩清理失败 %s——文件仍在树上，等 revert 路重清/"
                "L2 终态闸兜底", getattr(st, "id", "?"), _rev["revert_failed"])
        return None
    diff = _git_diff_for_paths(project_path, written, base_ref=state.get("base_commit"))
    if not diff.strip():
        # diff 生成失败 → 清掉刚写的桩（防污染本地树）后回退 revert。
        # round27：revert 按 st 全足迹清，必须带 H-exec2 护栏 protected_files——否则足迹与
        # 已完成兄弟重叠时（normalize 后 _grant_module_pom_writable 等可引入重叠）误删其有效产物。
        _local_tree_revert_subtask(project_path, st, protected_files=protected_files or set(),
                                   base_ref=state.get("base_commit"))
        return None
    logger.info("[阶梯三·桩] 为卡死子任务 %s 生成可编译桩 %s（下游可编译，需人工补完）",
                getattr(st, "id", "?"), written)
    return diff


async def _give_up_preserve_build(state: BrainState, failed_ids: list[str]) -> dict | None:
    """卡死子任务恢复阶梯·阶梯三：保 build 放弃（替代直接 escalate 全盘 FAILED）。

    阶梯一(retry)→阶梯二(定点拆小)都耗尽仍失败、且有成功兄弟时调用。做法：
      1. 自动判依赖：`any(X in st.depends_on for st in plan.subtasks)`。
         - 被依赖 → 先试【可编译桩】(_generate_compile_stub)：下游可编译，不连坐放弃；
           桩生成失败 → 回退 revert（并传递放弃下游，缺依赖跑不了）。
         - 不被依赖 → revert（只丢 X，零连坐）。
      2. 两路都【清本地树足迹】(_local_tree_revert_subtask)，杜绝坏文件毒 -am reactor。
      3. 给 X 终态 WorkerOutput（计入 completed，让 dispatch 推进到 merge→L2），
         记入 give_up_isolated_ids；revert 路若 X 被依赖则其下游进 abandoned_subtask_ids。
      4. 返回 strategy=give_up_preserve（非 replan/escalate → 路由 DISPATCH → remaining 空 → merge），
         保留全部成功成果，终态由 runner 据 give_up/abandoned 判 PARTIAL（诚实列明需人工补完）。

    返回 None 表示无法保 build 放弃（无 plan / 无可放弃项），调用方回退 escalate。"""
    plan_obj = state.get("plan")
    if plan_obj is None or not failed_ids:
        return None
    project_path = _proj_path_from_state(state)
    subtasks = list(getattr(plan_obj, "subtasks", []))
    by_id = {s.id: s for s in subtasks}
    subtask_results = dict(state.get("subtask_results", {}))
    give_up = set(state.get("give_up_isolated_ids") or [])
    abandoned = set(state.get("abandoned_subtask_ids") or [])
    handled: list[tuple[str, str]] = []
    # 猎手 F2：连坐放弃的下游 WorkerOutput 会被 pop（无 l1_details 可挂账）——其足迹
    # 清理失败必须走 degraded_reasons（reducer 通道）留机读痕，绝不随 pop 消失。
    cascade_revert_failed: list[str] = []

    for fid in failed_ids:
        st = by_id.get(fid)
        if st is None:
            continue
        depended = any(fid in (getattr(s, "depends_on", []) or []) for s in subtasks)
        stub_diff = None
        if depended:
            # round27：桩生成内部的 diff 失败清理路径也按 H-exec2 护住已完成兄弟产物。
            _prot_stub = _files_owned_by_completed(subtasks, subtask_results, exclude_ids={fid})
            # R65C-T3：下游 upstream_artifacts 声明的、落在 X 足迹内的产物 = 桩的硬覆盖
            # 目标（种子闸 #12 的判据面，含非代码文件）。
            # 猎手 F2（CONFIRMED HIGH）整改：两侧都过权威归一器 _norm_scope_path
            # （R41 实证 './'/反斜杠口径漂移是真实病）——弱归一会让 required 静默算空，
            # 完备性闸退化 no-op 且零留痕；匹配结果收敛回【足迹原形】保持下游比较一致。
            from swarm.brain.contract_utils import _norm_scope_path
            _fp_by_norm = {_norm_scope_path(f): f for f in _subtask_footprint(st)}
            _required_by_downstream: set[str] = set()
            _declared_n = 0
            for s in subtasks:
                if fid in (getattr(s, "depends_on", []) or []):
                    for ua in (getattr(getattr(s, "scope", None), "upstream_artifacts", []) or []):
                        _declared_n += 1
                        hit = _fp_by_norm.get(_norm_scope_path(str(ua).strip()))
                        if hit is not None:
                            _required_by_downstream.add(hit)
            if _declared_n:
                # 声明→匹配计数留痕：matched=0 时完备性闸等于未启用（声明可能指向
                # 其它上游，也可能是口径漂移）——必须可观测，绝不静默 no-op。
                logger.info(
                    "[阶梯三·桩] %s 下游声明 upstream_artifacts %d 条，落在其足迹内 %d 条"
                    "（=桩硬覆盖目标）%s",
                    fid, _declared_n, len(_required_by_downstream),
                    "" if _required_by_downstream else
                    "——完备性闸无目标（若声明本应指向该上游，查路径口径）")
            stub_diff = await _generate_compile_stub(state, st, project_path,
                                                     protected_files=_prot_stub,
                                                     required_files=_required_by_downstream)
        if stub_diff:
            mode = "stub"
            subtask_results[fid] = WorkerOutput(
                subtask_id=fid, diff=stub_diff,
                summary=(f"[阶梯三·桩] {fid} 卡死 → 生成可编译桩（保留 public 签名、方法体抛 "
                         "UnsupportedOperationException），下游可编译集成，需人工补完实现"),
                l1_passed=True,
                l1_details={"given_up": True, "give_up_mode": "stub"},
                confidence=Confidence.LOW,
            )
        else:
            # H-exec2：清 fid 足迹前，护住【其它已完成子任务】拥有的有效产物(footprint 重叠不误删)。
            _prot = _files_owned_by_completed(subtasks, subtask_results, exclude_ids={fid})
            rev = _local_tree_revert_subtask(project_path, st, protected_files=_prot,
                                             base_ref=state.get("base_commit"))
            mode = "revert"
            # 猎手 F1（CONFIRMED HIGH）：revert_failed 非空=树仍脏（git checkout/unlink
            # 失败），账面绝不能写「已清」——摘要如实 + l1_details 机读留痕（L2 终态闸
            # 兜真毒面；这里保 settled 终态语义不翻 l1_passed，防重入失败处理空转）。
            _rev_failed = list(rev.get("revert_failed") or [])
            if _rev_failed:
                logger.error(
                    "[阶梯三·revert] %s 足迹清理不完整 revert_failed=%s——残留文件可能"
                    "毒 build，已机读留痕，L2 终态闸兜底", fid, _rev_failed)
            _l1d_rev = {"given_up": True, "give_up_mode": "revert"}
            if _rev_failed:
                _l1d_rev["revert_failed"] = _rev_failed
            subtask_results[fid] = WorkerOutput(
                subtask_id=fid, diff="",
                summary=((f"[阶梯三·revert] {fid} 卡死 → 足迹清理不完整"
                          f"(revert_failed={_rev_failed}, reverted={rev['reverted']}, "
                          f"removed={rev['removed']})，残留文件可能毒 build，需人工清理补完")
                         if _rev_failed else
                         (f"[阶梯三·revert] {fid} 卡死 → 已清本地树足迹"
                          f"(reverted={rev['reverted']}, removed={rev['removed']})，"
                          "build 不被毒、其余成果照常交付，需人工补完")),
                l1_passed=True,
                l1_details=_l1d_rev,
                confidence=Confidence.LOW,
            )
            # revert 路：X 被依赖 → 其下游缺依赖跑不了 → 传递放弃（清足迹防毒 + 出完成态）。
            if depended:
                # R51-1 边界：revert 路径【保留】完成者连坐——上游代码被主动抽离树，
                # 依赖它编译过的下游产出随之破碎（与 unrecoverable/部分交付不同：那两路
                # 上游本无产出，下游完成=未真依赖）。
                _closed = _transitive_abandon(subtasks, abandoned | {fid})
                for s in subtasks:
                    if (s.id in _closed and s.id != fid
                            and s.id not in abandoned and s.id not in give_up):
                        abandoned.add(s.id)
                        # H-exec2：级联放弃下游清足迹时，同样护住其它已完成兄弟的有效产物。
                        _prot_c = _files_owned_by_completed(
                            subtasks, subtask_results, exclude_ids=_closed | {fid})
                        _rev_c = _local_tree_revert_subtask(
                            project_path, s, protected_files=_prot_c,
                            base_ref=state.get("base_commit"))
                        if _rev_c.get("revert_failed"):
                            logger.error(
                                "[阶梯三·连坐] 放弃下游 %s 足迹清理不完整 revert_failed=%s"
                                "——残留文件可能毒 build，已入 degraded_reasons 机读账",
                                s.id, _rev_c["revert_failed"])
                            cascade_revert_failed.append(
                                "cascade_revert_failed:%s:%s"
                                % (s.id, ",".join(_rev_c["revert_failed"][:3])))
                        subtask_results.pop(s.id, None)
        give_up.add(fid)
        handled.append((fid, mode))

    if not handled:
        return None
    _drop = {h[0] for h in handled} | abandoned
    dispatch_remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in _drop]
    logger.warning(
        "[HANDLE_FAILURE] 阶梯三 保 build 放弃 %s（清本地树足迹防 reactor 中毒，保留全部成功成果，"
        "run 继续 merge→L2，终态将 PARTIAL 诚实列明需人工补完）；连坐放弃下游 %d 个",
        handled, len(abandoned),
    )
    out = {
        "plan": plan_obj,
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": [],
        "failure_strategy": "give_up_preserve",
        "give_up_isolated_ids": sorted(give_up),
        "abandoned_subtask_ids": sorted(abandoned),
    }
    if cascade_revert_failed:
        # degraded_reasons 是 reducer 通道（append+dedup）——被 pop 的连坐下游唯一账面
        out["degraded_reasons"] = cascade_revert_failed
    return out
