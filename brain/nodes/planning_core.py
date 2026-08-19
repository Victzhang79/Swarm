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
import re
import logging

from pathlib import Path

from swarm.brain.state import BrainState
from swarm.brain.nodes.shared import _parse_json_from_llm
from swarm.types import Confidence, WorkerOutput

logger = logging.getLogger(__name__)


def _widen_scope_for_compile_repair(plan_obj, fid: str, details: dict,
                                    subtask_results: dict | None = None) -> list[str]:
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
    # DR-01-F6(#51) 治本：加宽绝不纳入【其它已完成兄弟拥有的产物】。否则失败子任务重派后可
    # 直接改写/覆盖兄弟已交付的跨模块文件（如 st-B 拥有的 ruoyi-common/BaseEntity.java），
    # MERGE 时以失败子任务版本落地、兄弟工作被静默改写。此路径无依赖序守卫（区别于缺依赖恢复臂
    # _grant_module_pom_writable+_serialize_pom_writers），只能排除；被排除的跨模块修复应由
    # 缺依赖恢复臂按依赖序安置，而非在此授直接写权。
    if subtask_results:
        _owned_by_others = _files_owned_by_completed(
            plan_obj.subtasks, subtask_results, exclude_ids={fid})
        _blocked = [f for f in new if _norm_rel(f) in _owned_by_others]
        if _blocked:
            logger.warning(
                "[WIDEN-SCOPE] DR-01-F6(#51) 拒绝把兄弟已完成产物加入失败子任务 %s 的 writable"
                "（防越权改写/连坐）: %s", fid, _blocked)
            new = [f for f in new if f not in _blocked]
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
        # DR-01-F3(#48)：先归一（剥 './'/'\\'/前导 '/'）。否则 './ruoyi-alarm/X.java' → top='.'
        # （'.' 不在 _NON_MODULE_TOP）→ 调用方拼出 './pom.xml' 当【模块 pom】授写权，实为
        # 【根聚合 pom】→ 多恢复子任务双写根 pom = D1 rebase 循环根因。'.'/'..'/空串显式排除。
        nf = str(f).replace("\\", "/")
        while nf.startswith("./"):
            nf = nf[2:]
        nf = nf.lstrip("/")
        if "/" in nf:
            top = nf.split("/", 1)[0]
            if top and top not in _NON_MODULE_TOP and top not in (".", ".."):
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


def _grant_module_pom_writable(plan_obj, failed_ids: list, manifest: str = "pom.xml") -> dict:
    """给失败子任务补其模块 <module>/<manifest> 写权，返回 {sid: mod_manifest} 已授权映射。

    让重试能真正改构建清单补依赖（原本清单不在 scope，重试再多也修不了）。同时让失败子任务
    depends_on【该清单的既有 owner】（HIGH-2）：owner 可能是已 DONE 的脚手架子任务，二者都写
    同一清单，必须靠拓扑序让 owner 的 create 在前、coder 的 modify 在后，MERGE 才不冲突。

    ★#38 治本★ manifest 由调用方按项目栈传入（Maven=pom.xml 默认，byte-identical；Go=go.mod、
    npm=package.json…）——绝不在非 Maven 工程授【幻影 pom.xml】写权烧恢复预算。默认 pom.xml 保
    既有调用点/测试零改动。
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
        mod_pom = f"{mod}/{manifest}"
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
                # 猎手 R65TR-T3 F3：排除本批 failed_ids——多受害者同批授权时先授权者
                # 的 scope 已被就地 mutate 含 mod_pom，会被后续迭代当"owner"抓到 →
                # 首列受害者反被串到全体兄弟后面（恰是要治的病换个座次）。修复中的
                # 对等者绝不互为 owner；批内序交 _serialize_pom_writers 统一成链。
                if o.id != st.id and o.id not in failed_ids and mod_pom in (
                    list(getattr(getattr(o, "scope", None), "create_files", []) or [])
                    + list(getattr(getattr(o, "scope", None), "writable", []) or [])
                )
            ),
            None,
        )
        if owner is not None:
            _o_sc = getattr(owner, "scope", None)
            if mod_pom in (list(getattr(_o_sc, "create_files", []) or [])):
                # owner 是真 creator（脚手架建 pom）→ 注册序：pom 先在，grantee 在后（原语义）。
                _add_dep_safe(by_id, st.id, owner.id)
            else:
                # R65TR-T3 P1：owner 只是【对等 modify 写者】（基线既有清单，plan 无 creator）——
                # 旧码把被恢复者挂它后面，而它可能从未派发/困在别的死链（963d78da 实锤：
                # st-39-1 授权后被串到 st-2 连坐闭包里的 st-29-1 后面，1 小时零派发零日志，
                # 终态 dispatched_unaccounted）。序不变量只要求有序不要求方向：反转为
                # 被恢复者先行、停车的对等写者靠后；防环命中则不加边（并发写交 E3 写集锁+MERGE）。
                _rev = _add_dep_safe(by_id, owner.id, st.id)
                logger.info(
                    "[HANDLE_FAILURE] R65TR-T3 pom 写权串边方向：%s（恢复重派）先行，"
                    "对等写者 %s 靠后（owner 非 creator%s）",
                    st.id, owner.id, "" if _rev else "；防环守卫命中→不加边",
                )
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
            _nxt, _prv = _all_writers[i], _all_writers[i - 1]
            if _prv in (getattr(by_id.get(_nxt), "depends_on", []) or []):
                continue  # 既有边，幂等
            if not _add_dep_safe(by_id, _nxt, _prv):
                # 猎手 R65TR-T3 F4：防环丢边必须留痕——两套串链机制（本函数 id 序 vs
                # 写权授予串边）方向相抵时静默丢边=最终图形无人能解释。
                logger.info(
                    "[HANDLE_FAILURE] R65TR-T3 pom 写者串链边 %s→%s 被防环守卫拦下"
                    "（既有反向序在，保留既有方向）", _nxt, _prv)


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
        logger.warning("[HANDLE_FAILURE] 阶梯二 定点拆小异常(跳过): %s", exc)
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
        logger.warning("[HANDLE_FAILURE] 超时拆小：planning 辅助导入失败(跳过): %s", exc)
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
            logger.warning("[HANDLE_FAILURE] 超时拆小 %s 异常(跳过): %s", fid, exc)
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
            owned.add(_norm_rel(f))
    return owned


def _local_tree_revert_subtask(project_path: str | None, st, protected_files: set | None = None,
                               base_ref: str | None = None,
                               extra_files: list | None = None) -> dict:
    """卡死子任务恢复阶梯·阶梯三(revert)：把子任务在【本地树】的足迹清干净。

    3rd#2：已跟踪文件 checkout 回【钉扎 base】版（None→HEAD 零回归），与交付链其余站点同源——
    避免运行期 HEAD 漂移后把文件复位到与 merged_diff 基线不符的版本。

    protected_files（H-exec2 窄守卫，round21）：被【其它已完成子任务】拥有为有效产物的文件集——
    即便落在本子任务 footprint 内也【跳过删除/回退】，杜绝放弃时误删兄弟已落盘产物(footprint 与兄弟
    scope 重叠场景)。纯加性守卫，不重构 round15 红线的桩+级联恢复逻辑。

    extra_files（R67L-B4③，22号文批次4 T7 清扫洞）：scope 声明【之外】的额外足迹文件
    （典型来源=调用方从该子任务自身 result diff 解析的越权写入——round67l st-14 scope
    仅 create ruoyi-alarm/pom.xml 却越权写根 pom/ruoyi-framework/pom.xml，scope 驱动
    的 footprint 够不着 → 终态树残留毒改动）。与 scope footprint 取并集（归一化去重），
    同样过 protected_files 窄守卫。缺省 None=纯 scope 驱动（旧行为）。

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
    # R67L-B4③：footprint = scope 声明 ∪ extra_files（归一化去重，保序确定性）
    _footprint = list(_subtask_footprint(st))
    _seen_fp = {_norm_rel(rel) for rel in _footprint}
    for _ef in (extra_files or []):
        _n = _norm_rel(_ef)
        if _n and _n not in _seen_fp:
            _seen_fp.add(_n)
            _footprint.append(_n)
    for rel in _footprint:
        # H-exec2 窄守卫：该 footprint 文件是【其它已完成子任务】的有效产物 → 跳过删除/回退。
        if _norm_rel(rel) in _protected:
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


_VERSION_TAG_RE = re.compile(r"<version>\s*([^<>\s]+)\s*</version>", re.I)
_LEADING_DOTSLASH_RE = re.compile(r"^(?:\./)+")


def _norm_rel(p: object) -> str:
    """相对路径归一：只剥【前导 `./`】，绝不用 `lstrip("./")`。

    ★为什么必须有（判据 C 族，本会话实测在自己代码里咬人）★
    `lstrip("./")` 剥的是**字符集合**而非前缀：`.build/pom.xml` → `build/pom.xml`、
    `.mvn/wrapper/x` → `mvn/wrapper/x`、`.gitattributes` → `gitattributes`。
    后果实测：桩写 `.build/pom.xml` 时归一后路径在盘上不存在 ⇒ 自检把它当"上游契约异常"
    跳过 ⇒ 脏面查询坏掉时无人发现（本文件 F7 那条锁就是被这个 bug 打红的）。
    JVM 工程里 `.mvn/` 是真实且承重的目录，故这不是边角。

    ★本模块内【全部】8 个归一点都走本函数，一个不留（32 号文双复核 R4）★
    我起初只把本批新增的三处（`_verified_sibling_files` 两处 + `_clean_stub_residue`
    的 `_norm`）改过来，理由写的是"既有点两侧同源、单改一侧会让保护面失配"。
    **那条理由把链读断了**：`_clean_stub_residue`（新）产出的 `_prot`/`_extra` 正是
    `_local_tree_revert_subtask`（旧）的入参，`:637` 拿 `_protected` 比对、`:631` 再归一
    一次并把**归一后**的串 append 进 `_footprint` ⇒ 产者与消费者本就跨了新旧边界，
    分批改才是失配的来源。故 8 处必须**原子同改**。
    ★真危害不止比对失配，更重的一头是【归一后的串被拿去访问盘/git】★：
    `_local_tree_revert_subtask:650/669` 的 `git checkout -- rel` 与 `root / rel` 吃的就是
    `_footprint` 里的串。`.mvn/wrapper/maven-wrapper.properties` 被 lstrip 成
    `mvn/wrapper/...` ⇒ 盘上不存在 ⇒ `ls-files` 失败(tracked=False) → `is_file()` 也 False
    → `:672` 那个 `if` **没有 else** ⇒ 静默零清理，连 `revert_failed` 都不记，残留留树毒
    build 而交付面无痕（"缺席必须机读可辨"的教科书形态）。
    """
    return _LEADING_DOTSLASH_RE.sub("", str(p).replace("\\", "/"))


def _norm_rel_cmp(p: object) -> str:
    """相对路径归一【比较形】：`_norm_rel` 之外再剥前导 `/`（`.//x`、`/x` 都归到 `x`）。

    ★判据 C 清扫定案的两形契约（两个方向各有一个实证，缺一不可）★
    - **比较形＝本函数**：只用于【比对/集合成员/取 top 段/basename】，绝不用于盘/git
      访问——前导 `/` 被剥掉意味着绝对路径被静默改成相对（盘侧那是路径混淆）。
    - **盘形＝`_norm_rel`**（只剥 `./` 序列）：git/盘语义（git 路径永无前导 `/`）。
    两形分歧的实证链：A5-L4 把 `lstrip("./")` 直接换成 `_norm_rel`（盘形）⇒ `.//src/B.java`
    归一成 `/src/B.java`，provenance 匹配不上＝换个方向的同一个 bug（回归）；而裸
    `lstrip("./")` 本身是字符集剥 ⇒ `.mvn/x`→`mvn/x`（bug 本体，两处实证在 `_norm_rel`
    的 docstring）。⇒ 契约必须两形分立且各有名字——再手写 `lstrip("./")` 就是重新
    发明这两个 bug。
    ★同族不合并★ `contract_utils._norm_scope_path` 是 scope 专用超集（多剥尾 `/`，
    治 file_plan `x/pom.xml/` vs scope `x/pom.xml` 假孤儿）——尾维度会改变目录形路径
    语义，消费契约不同，刻意不并（血规：复用单一事实源 ≠ 复用其消费契约）。
    worker 侧比较形单一事实源＝`l1_error_drivers._norm_rel`（同语义；
    `test_criterion_c_norm_rel_contract` 用语料锁互钉漂移）。
    """
    return _norm_rel(p).lstrip("/")


def _strip_ungrounded_lines(
        diff_text: str, known: set[str]) -> tuple[str, list[str], dict[str, dict[str, str | None]]]:
    """逐行剥离查无实据的版本号，并**修好 modify 型的替换对**（R1 三轮整改）。

    返回 `(剩余 diff, 被剥的版本号, 盘侧动作)`。盘侧动作是
    `diff 里的文件路径 → {桩写的那行原文 → 替换成什么}`（**按文件分桶**），
    桶内值 `None` ＝ 该行直接删掉（纯新增行的既有语义）。

    ★32 号文双复核 HIGH-2 整改：键必须带【文件】这一维★
    原实现 `_disk` 以**裸行文本**为键（`dict[line, action]`），而 diff 跨多份清单：
    桩在两份 pom 里写**同一个**臆造版本（同步升版本是桩的典型形态）且动作档不同
    （modify 型=还原成**各自**的 base 行 / 纯新增=删除）时，dict 后写覆盖先写 ⇒
    轻则 A 文件被还原成 **B 的** base 版本（臆造坐标换个来源照样落地），
    重则「删除」盖掉「还原」⇒ `<parent>` 无 `<version>` 的 Maven 残骸——正是本闸
    存在要防的那一类。根因＝R1 三轮扩动作**值域**时没核**键**的分辨率。
    残留边界（如实登记，不硬治）：**同一文件内**同文本重复行仍塌缩（LOW-2）——
    那需要按行号寻址，远超本批范围；该形态在真实 pom 里罕见（同一 version 行
    重复出现且恰好一真一假）。

    ★为什么必须认"替换对"（本轮探针实测，两侧都中）★
    modify 型桩把既有版本行换掉时，`git diff` 产出的是一对：
        `-    <version>3.2.0</version>`   ← base 内容（按定义已验证）
        `+    <version>3.3.0</version>`   ← 桩臆造
    原实现只丢 `+` 行、**`-` 行照旧保留** ⇒ 合起来＝"把 base 那行删掉且不补回"：
    - diff 侧：陈旧 hunk 计数被 merge 的 `_recount_hunk_header` 重算 ⇒ 补丁 well-formed
      ⇒ **apply 成功** ⇒ 落地后 `<parent>` 无 `<version>`（实测）；
    - 盘侧：`_sync_disk` 精确删掉桩写的那行 ⇒ 同一残骸。
    即 `_drop_whole_manifests` 治的那个"Maven 解析不了的残骸"在 modify 型上原样存在——
    而那条治法的判据要求【零 base 证据】，modify 型文件 base 里**有**它 ⇒ 结构性抓不到。

    治法＝把这对**整对**丢掉：diff 侧两行都不出现 ⇒ 该行在 base 里是什么就保持什么；
    盘侧把桩写的那行**还原成 `-` 行的内容**（那是 base 内容，`git diff <base>` 的 `-` 侧
    按构造即已验证，无需另找证据）。两侧口径同源，都由本函数一处产出。

    ★配对窗口的边界（MED#2 整改后语义）★ 判据必须确定性且不过宽：
    - 连续的 `-<version>` 行进【FIFO 配对队列】；被剥的 `+<version>` 行从**队首**取配。
      git 对连续多行修改产出「先全部 `-`、再全部 `+`」的**按位替换**语义 ⇒ 第 k 个
      `+` 配第 k 个 `-`。旧实现只取 `_kept[-1]`（队尾）⇒ 成组时**映射交叉**
      （8.8.8→2.2.2 / 9.9.9→1.1.1，探针实证），`_sync_disk` 把两处的 base 版本互换
      写盘 ⇒ 依赖 A 顶 B 的版本＝臆造坐标经"互换"通道落地（32 号文双复核 reviewer
      MED#2，自验=[真]）。交替形态下队列在消费时恒只有 1 个元素 ⇒ 行为与旧实现
      **逐字一致**。
    - 任何其他行（context/头/保留下来的 `+`/非 version 的 `-`）都【冲刷】队列。
      放宽成"同 hunk 里任意 `-<version>` 行"会在"删一处旧依赖、另一处新增臆造版本"
      的 hunk 里把不相干的 `-` 行也丢掉＝改写了桩的真实意图；而保留的 `+` 行在 git
      按位语义下自己已经吃掉了一个 `-` 位，绝不允许后面的 `+` 再隔着它配。宁窄不宽：
      认不出配对时退回删档（只丢 `+` 行），那是本函数配对机制引入前的既有语义。
    """
    _kept: list[str] = []
    _dropped: list[str] = []
    _disk: dict[str, dict[str, str | None]] = {}
    _pending: list[str] = []  # 连续 `-<version>` 行的还原文本（FIFO 配对队列，冲刷=清空）
    _cur_file = ""          # 当前 `+++ b/` 头给的路径（原样保留，归一在盘侧一处做）
    for _ln in diff_text.splitlines(keepends=True):
        if _ln.startswith("+++"):
            _hdr = _ln[4:].strip()
            _cur_file = _hdr[2:] if _hdr.startswith("b/") else _hdr
            _pending.clear()                       # 头行冲刷配对窗口
        elif _ln.startswith("+"):
            _m = _VERSION_TAG_RE.search(_ln)
            if _m and not _m.group(1).startswith("${") and _m.group(1) not in known:
                _dropped.append(_m.group(1))
                _stub_text = _ln[1:].rstrip("\n")      # 去 `+` 前缀存原文
                if _pending:
                    # 配对的 `-<version>` ⇒ 这是**替换**，整对丢掉（FIFO＝git 按位语义）
                    _n = len(_pending)
                    _restore = _pending.pop(0)
                    _kept.pop(len(_kept) - _n)     # 配掉的那条 `-` 行也不采纳 ⇒ base 原地留住
                    _disk.setdefault(_cur_file, {})[_stub_text] = _restore
                else:
                    _disk.setdefault(_cur_file, {})[_stub_text] = None  # 纯新增 ⇒ 删该行
                continue
            _pending.clear()                       # 保留的 `+` 行自己吃掉了一个 `-` 位 ⇒ 冲刷
        elif (_ln.startswith("-") and not _ln.startswith("---")
                and _VERSION_TAG_RE.search(_ln)):
            _pending.append(_ln[1:].rstrip("\n"))  # 候选配对的 `-<version>` 行（入队尾）
        else:
            _pending.clear()                       # context/非 version `-` 行冲刷配对窗口
        _kept.append(_ln)
    return "".join(_kept), _dropped, _disk


def _record_degrade_safe_pc(key: str) -> None:
    """degrade 记账（本模块内统一入口）：记账自身失败绝不阻断业务路径。"""
    try:
        from swarm.infra.degrade import record_degrade
        record_degrade(key)
    except Exception:  # noqa: BLE001 — 可观测面失败不影响正确性路径
        logger.debug("[阶梯三] degrade 记账失败: %s", key)


def _strip_ungrounded_manifest_coords(
    stub_diff: str | None, project_path: str | None, fid: str,
    verified_files: set[str] | None = None,
    stub_written: list[str] | None = None,
    base_ref: str | None = None,
) -> str | None:
    """★阶梯三桩写构建清单时，剔掉基线里查无实据的版本号（26 号文 C-3）★

    桩是 LLM 产物且 `l1_passed=True` 零编译零验收就解锁下游——它是全流程里唯一一条
    "LLM 直接产出构建清单且不过任何确定性闸"的通路，与铁律"绝不猜依赖坐标"正面冲突。
    round67m2 实证：桩写的父 POM 版本 3.8.7 而 base 是 4.8.3，解析必失败。

    判据（确定性、纯文本、栈中立）：桩新增行里出现的每个 `<version>` 值，必须在**基线的
    构建清单**里真实出现过；查无实据即删除该行（属性/占位符 `${...}` 放行——那是引用不是坐标）。
    删行而不是删整份桩：桩的价值是让下游可编译，保住骨架、剔掉臆造坐标才是 fail-honest。
    非 JVM 栈（无 pom）自然零命中，不误伤。project_path 不可读 → 原样返回（fail-open：
    读不到基线就无从判定，绝不凭空剥离真坐标）。

    ★32 号文 A10-M2 治本：证据集必须排除【本轮未经确定性闸验证的树状态】★
    病根＝生产序是「桩先写盘(`_generate_compile_stub` 内 `write_text`)→ 本闸再校验」，而证据集
    靠 `os.walk` 扫【当前树】⇒ 桩刚写进 `mod/pom.xml` 的臆造版本被自己扫成"基线已存在"⇒ 放行。
    决定性实验（两组唯一差别＝盘上有没有那份桩 pom）：桩未写盘时 3.8.7 被正确剥离，
    桩已写盘时**同一个 diff 一字不改却放行**——即本闸对生产序恒失效，恰好在它要防的
    round67m2 st-3-1（桩写父 POM 3.8.7 而 base 4.8.3）形态上零作用。

    **不变量：一个坐标只能靠【已验证的树状态】接地。** 已验证＝二者之一：
      (a) 与钉扎 base 一致（未改动）——base 是任务启动时的既成事实；
      (b) 属于【已过 L1 的兄弟子任务】的产物——L1 是确定性闸，过了就是证据。
    未改动性用 `uncommitted_changed_files`（git 事实，非猜测）判。

    ★复核 MED-1 整改记录（第一版治法窄一层）★：初版只排除"本补丁涉及的路径"
    （`files_from_unified_diff`），复核实跑证伪其充分性——X 的 worker 越权写脏的**别的**
    模块清单（`mod-a/pom.xml`，不在桩 diff 里）照样能给桩的臆造坐标（`mod-b/pom.xml` 里的
    同一版本）当证据。缺的那一维是"**未验证 ≠ 已验证**"，不是"自己 ≠ 别人"。
    刻意**不用** base-only：兄弟已过 L1 引入的新版本是真证据，base-only 会误剥、
    闸过宽使用者会绕开（该边界有反向锁看着）。
    `verified_files` 由调用方传入（已过 L1 兄弟的产物路径）；缺省 None ⇒ 只认 (a)。
    """
    if not stub_diff or not project_path:
        return stub_diff
    try:
        import os as _os
        import subprocess

        from swarm.git_base import resolve_base_ref
        _base_for_cmp = resolve_base_ref(base_ref)

        def _cands_text(abs_path: str) -> str:
            try:
                with open(abs_path, encoding="utf-8", errors="ignore") as _f:
                    return _f.read()
            except OSError:
                return "\x00unreadable"      # 读不到 → 与任何 base 内容都不相等

        def _base_text_of(rel: str) -> str | None:
            """取该路径在钉扎 base 里的内容；不在 base / 读不到 → None。

            ★v4 复核 F2 整改：路径必须带 `./` 前缀★
            `git show <rev>:<path>` 的 `<path>` 是**仓根相对**，而本函数拿到的 `rel` 是
            **project_path 相对**。project_path 是仓库子目录时两者错位，git 直接致命错误
            （实测原文：`'backend/pom.xml' 路徑存在，但不是 'pom.xml'`，并提示应写
            `<rev>:./pom.xml`）。`./` 形式在仓根与子目录**都正确**，故统一加。
            讽刺点记一笔：这条口径失配正是本文件自检注释里列为"要抓的成因"之一，
            而自检自己当时用的就是错口径——同一份心智模型既写判据又写实现，缺口必然重合。
            """
            try:
                _pr = subprocess.run(
                    ["git", "-C", project_path, "show", f"{_base_for_cmp}:./{rel}"],
                    capture_output=True, text=True, timeout=15)
            except Exception:  # noqa: BLE001
                return None
            return _pr.stdout if _pr.returncode == 0 else None

        def _sync_disk(dropped_by_file: dict[str, dict[str, str | None]]) -> tuple[list[str], list[str]]:
            """★复核 R2-③ 整改：剥离必须同步落盘★

            病根＝本闸从诞生起只改 **diff 文本**，桩写盘的那份文件仍留着臆造坐标。
            后果正是本批要治的"验证树 ≠ 交付面"：merged_diff 干净，而 L2 **按本地树**
            构建（`integration_review` 原地 reset+apply）⇒ 照样中毒；且本批新加的
            `_kept` 保护把这份脏清单结构性护住，谁也清不掉（实测 diff 干净/盘上仍 3.8.7）。

            ★v4 复核 F1 整改：只删【diff 侧真被剥掉的那几行】，绝不重跑判据★
            初版对**盘上每一行**重施 drop 判据，而 diff 侧只对 `+` 行施判据——两者不等价：
            modify 型桩（改写既有清单）的 diff 里，base 原有 version 是 **context 行**、
            diff 不动它，而盘上那一行会被判据判"不在 _known"直接删。实测把 `<parent>` 的
            1.0.0 与既有依赖 2.5.0 双双删掉＝D3 铁律点名的 reactor 解析期崩。
            现在入参由 `_strip_ungrounded_lines` **一处产出**，盘上按**整行精确匹配**处理
            ⇒ 判据只有一处、口径不可能再分叉。

            ★R1 三轮：`None` 之外还有【替换】一档★ modify 型桩换掉既有版本行时，删该行＝
            把 base 那行也删了（`<parent>` 无 `<version>`＝Maven 非法，实测两侧都中）。
            该档把桩写的那行**还原成 `-` 行的内容**（`git diff <base>` 的 `-` 侧按构造即
            base 内容）。删档与替换档必须同一入参一处产出，否则 diff 侧与盘侧会再次分叉。

            ★32 号文双复核 HIGH-2 整改：入参按【文件】分桶，各文件只查自己的子表★
            原实现入参以裸行文本为键 ⇒ 跨文件同文本撞键（桩在两份 pom 写同一臆造版本
            且动作档不同时，后写覆盖先写）。现在键＝diff `+++ b/` 头给的路径（仓根相对），
            本函数用 `git rev-parse --show-prefix` 把它归一到 **project_path 相对**后与
            `stub_written` 对齐——与 v4 F2 那次「仓根相对 vs project 相对」错位同族，
            归一只做这一处，绝不在比较两侧各做一份。
            ★哑映射必须 fail-loud★：入参非空却没有任何一份 stub 文件匹配上键 ⇒ 落盘
            同步静默整体失效（正是 R2-③ 要防的形态复活）⇒ 独立机读键 + ERROR，
            绝不假装"无可删"。
            返回 (改动成功的路径, 写盘失败的路径)。
            """
            _fixed: list[str] = []
            _failed_w: list[str] = []
            if not dropped_by_file:
                return _fixed, _failed_w      # 无可删（与 diff 侧同源：那边也没剥）
            # diff 头路径（仓根相对）→ project_path 相对：剥 repo 前缀
            _prefix = ""
            try:
                _pp = subprocess.run(
                    ["git", "-C", project_path, "rev-parse", "--show-prefix"],
                    capture_output=True, text=True, timeout=10)
                if _pp.returncode == 0:
                    _prefix = _pp.stdout.strip()
            except Exception:  # noqa: BLE001 — 取不到前缀就按空前缀归一，哑映射有账
                pass
            _by_proj: dict[str, dict[str, str | None]] = {}
            for _h, _sub in dropped_by_file.items():
                _k = _h[len(_prefix):] if _prefix and _h.startswith(_prefix) else _h
                _by_proj.setdefault(_norm_rel(_k), {}).update(_sub)
            _matched_any = False
            for _p in {_norm_rel(x) for x in (stub_written or [])}:
                if _os.path.basename(_p).lower() not in (
                        "pom.xml", "build.gradle", "build.gradle.kts"):
                    continue
                _file_drops = _by_proj.get(_p)
                if not _file_drops:
                    continue
                _ap = _os.path.join(project_path, _p)
                try:
                    # ★F6★ errors="ignore" 与本函数其余两处读盘同源：非 utf-8 清单
                    #（GBK 中文注释的 JVM 工程是真实形态）抛 UnicodeDecodeError 不是
                    # OSError，会逃出内层 except 落到外层 ⇒ **整闸原样返回**（fail-open）。
                    with open(_ap, encoding="utf-8", errors="ignore") as _f:
                        _lines = _f.readlines()
                except OSError:
                    continue
                _matched_any = True
                _out_lines: list[str] = []
                _touched = False
                for _ln_d in _lines:
                    _bare = _ln_d.rstrip("\n")
                    if _bare not in _file_drops:
                        _out_lines.append(_ln_d)
                        continue
                    _touched = True
                    _repl = _file_drops[_bare]
                    if _repl is None:
                        continue                      # 删档：纯新增行，整行去掉
                    # 替换档：还原成 base 那行（保留原行尾，避免末行丢换行）
                    _eol = _ln_d[len(_bare):]
                    _out_lines.append(_repl + _eol)
                if not _touched:
                    continue
                try:
                    with open(_ap, "w", encoding="utf-8") as _f:
                        _f.writelines(_out_lines)
                    _fixed.append(_p)
                except OSError as _wexc:
                    _failed_w.append(_p)
                    logger.error(
                        "[阶梯三·桩] %s 臆造坐标落盘剥离失败 %s（盘上仍带臆造坐标，"
                        "L2 按本地树构建会中毒）: %s", fid, _p, _wexc)
            if not _matched_any:
                # 哑映射：有剥离动作却没有任何一份 stub 文件匹配上键 ⇒ 落盘同步整体
                # 失效（R2-③ 形态复活）。与写盘失败同后果（残留留树）但成因不同 ⇒ 独立键。
                _record_degrade_safe_pc("brain.stub_grounding.sync_disk_unmapped")
                logger.error(
                    "[阶梯三·桩] %s 落盘同步哑映射：%d 份文件有剥离动作但无一匹配 "
                    "stub_written（键=%s，stub_written=%s）——盘上仍带臆造坐标，"
                    "L2 按本地树构建会中毒", fid, len(dropped_by_file),
                    sorted(dropped_by_file)[:6], (stub_written or [])[:6])
            return _fixed, _failed_w

        def _ungrounded_zero_evidence_manifests(
                diff_text: str, known: set[str], no_evidence: set[str]) -> set[str]:
            """哪些【零 base 证据】清单的 diff 段里至少有一个查无实据的具体版本号。

            ★为什么判据是"零 base 证据 ∧ 至少一行会被剥"★
            - **零 base 证据**（`base` 里读不到 ⇒ 桩新建的文件）：逐行剥离没有安全网。
              有 base 版的清单被剥掉几个 `+` 行后，剩下的仍是 base 那份合法清单；
              而新建文件被剥掉 `<parent><version>`／自身 `<version>` 后**没有底可退**，
              产出的是 Maven 解析不了的残骸（＝D3 铁律点名的 reactor 解析期崩）。
            - **至少一行会被剥**：桩若把 base 里真实存在的坐标照抄进新建清单，一行都不会
              被剥、文件完全合法 ⇒ 此时整份丢弃就是**误杀**（把一份好清单删掉，下游种子闸
              直接 BLOCKED）。故必须先算"会不会真被剥"，不能一见零证据就丢。

            `known` 传**该臂的有效证据集**：正常路径传 `_known`，fail-closed 臂传 `set()`
            （那条臂的语义就是"没有任何已验证坐标"）⇒ 两臂共用同一判据，口径不可能再分叉。
            """
            if not no_evidence:
                return set()
            _hit: set[str] = set()
            from swarm.project.diff_apply import split_diff_by_file
            for _files, _sub in split_diff_by_file(diff_text):
                _rels = {_norm_rel(f) for f in _files} & no_evidence
                if not _rels:
                    continue
                for _ln in _sub.splitlines():
                    if not _ln.startswith("+") or _ln.startswith("+++"):
                        continue
                    _mm = _VERSION_TAG_RE.search(_ln)
                    if _mm and not _mm.group(1).startswith("${") \
                            and _mm.group(1) not in known:
                        _hit |= _rels
                        break
            return _hit

        def _drop_whole_manifests(
                diff_text: str, rels: set[str]) -> tuple[str, list[str], list[str]]:
            """★R1（A10-M2 双复核整改）整份不采纳零证据清单★

            返回 (剩余 diff, 盘上已删, 盘上删失败)。

            ★为什么不能沿用逐行剥离★ 剥离式治法对**零 base 证据的清单**产出 Maven 解析
            不了的残骸：greenfield 下桩新建 `mod/pom.xml`，`<parent><version>3.2.0` 与模块
            自身 `<version>1.0.0` 一起被剥（两者都"查无实据"，因为 base 里这文件根本不存在）
            ⇒ 盘上剩一个有 `<parent>` 却无 `<version>` 的 pom＝D3 铁律点名的 reactor
            解析期崩。已实跑复现（该臂因此**生产可达**，不是理论风险）。
            复核首选处方"剥离前把本文件 base 版坐标纳入 _known"在此不适用：greenfield
            下文件是桩新建的，`_base_text_of` 返 None，**没有 base 坐标可纳入**。

            治法（用户拍板方案②）：零 base 证据 ⇒ **该清单整份不采纳**。半份清单不是
            更安全的清单，是更难归因的清单——整份不采纳后 L2 失败归因是"模块无 pom"
            （下游种子闸/完备性闸的既有语义），而非"pom 语法坏"（无人能一眼看懂）。

            ★diff 侧与盘侧必须原子同改★ 只改一侧会留下"diff 与盘不一致"的第三种状态，
            比现状更坏（merged_diff 说没这文件、本地树里却有，L2 按本地树构建）。
            """
            if not rels:
                # ★没有可丢的 ⇒ 原文**逐字**返回★ 绝不走下面的拆分再拼接：
                # `split_diff_by_file` 会丢掉"提取不到文件的前言段"（它的既有契约），
                # 无损重组不是它的承诺 ⇒ 空操作也可能悄悄改写 diff。
                return diff_text, [], []
            _kept_secs: list[str] = []
            _dropped_rels: set[str] = set()
            from swarm.project.diff_apply import split_diff_by_file
            _secs = split_diff_by_file(diff_text)
            if not _secs:
                return diff_text, [], []      # 拆不出文件段 → 不动（fail-open，与外层一致）
            for _files, _sub in _secs:
                _hit = {_norm_rel(f) for f in _files} & rels
                if _hit:
                    _dropped_rels |= _hit
                    continue                  # 整段（含 ---/+++/@@ 全部 hunk）不采纳
                _kept_secs.append(_sub)
            _rm_ok: list[str] = []
            _rm_fail: list[str] = []
            for _r in sorted(_dropped_rels):
                _apr = _os.path.join(project_path, _r)
                try:
                    if _os.path.isfile(_apr):
                        _os.remove(_apr)
                        _rm_ok.append(_r)
                    else:
                        _rm_ok.append(_r)     # 盘上本就没有＝已达成"不采纳"终态
                except OSError as _rexc:
                    _rm_fail.append(_r)
                    logger.error(
                        "[阶梯三·桩] %s 零证据清单 %s 整份不采纳但删盘失败（diff 侧已移除而"
                        "本地树仍有该文件 ⇒ diff 与盘不一致，L2 按本地树构建会拿到未经"
                        "接地的清单）: %s", fid, _r, _rexc)
            return "".join(_kept_secs), _rm_ok, _rm_fail

        _verified = {_norm_rel(p) for p in (verified_files or set())}
        # ★复核 HIGH-1★ 桩本轮写盘的路径【一律不算已验证】——盘上那份内容就是桩刚写的，
        # 不管谁曾经写过/声明过它。少这一减，兄弟 scope 声明与桩写盘面重叠时
        #（`_grant_module_pom_writable`：owner 可能是已 DONE 脚手架，二者都写同一清单）
        # 桩会自己给自己当证据（实跑坐实：兄弟 l1_passed 一翻真，同一 diff 就放行）。
        _stub_w = {_norm_rel(p) for p in (stub_written or [])}
        _verified -= _stub_w
        # ① 先 walk 收全部构建清单路径（证据候选面）
        _cands: dict[str, str] = {}      # rel → abs
        for _root, _dirs, _files in _os.walk(project_path):
            _dirs[:] = [d for d in _dirs if not d.startswith(".")][:80]
            for _f in _files:
                if _f.lower() not in ("pom.xml", "build.gradle", "build.gradle.kts"):
                    continue
                _abs = _os.path.join(_root, _f)
                _cands[_os.path.relpath(_abs, project_path).replace("\\", "/")] = _abs
        if not _cands:
            return stub_diff          # 无清单可比 → 不判（fail-open，非 JVM 栈常态）
        # ② 问 git：这些候选里哪些有【未提交改动】（含 untracked 新建——桩产出恰是这类）。
        # ★口径注意★ `uncommitted_changed_files` 的契约是"【这些】文件里哪些脏"，
        # files 为空即返 []（git_base.py:115）——所以必须把候选清单显式传进去，
        # 传 None 会拿到空集 ⇒ 证据集剔不掉任何未验证状态 ⇒ 闸静默退回原缺陷。
        #（复用单一事实源 ≠ 复用其消费契约：这里要的是"按清单查"而非"扫全树"。）
        _dirty: set[str] = set()
        try:
            from swarm.git_base import uncommitted_changed_files
            _dirty = {_norm_rel(p)
                      for p in (uncommitted_changed_files(
                          project_path, sorted(_cands)) or [])}
        except Exception:  # noqa: BLE001 — 脏面判定失败不阻断校验（退旧行为 + 留痕）
            logger.warning(
                "[阶梯三·桩] %s 未提交改动面判定失败 → 证据集无法剔除未验证树状态"
                "（退化为旧行为，接地闸可能被本轮脏树自证）", fid)
        # ★复核 MED-2 整改：脏面查询【可信性自检】★
        # `uncommitted_changed_files` 把自己所有失败都吞成 `[]`（git 超时 20s / rc!=0
        # /index.lock 争用 / OSError，见 git_base.py:115-125 —— **不抛异常**）⇒ 上面那条
        # except 只接得住 ImportError，真实失败面走"正常返回空集"这条路 ⇒ `_dirty` 恒空
        # ⇒ 全部候选被当已验证 ⇒ 闸静默退回原缺陷。另一个同后果成因是**口径失配**：
        # porcelain 输出【仓根相对】路径，而 `_cands` 键是【project_path 相对】——
        # project_path 非仓根时两侧永不相等，`_dirty` 同样恒空。
        # 自检判据（一条盖住全部四种成因）：桩刚 `write_text` 过的清单**必然脏**，
        # 若它落在候选里却不在 `_dirty` 里 ⇒ 脏面结果不可信 ⇒ fail-closed：
        # 该轮一律按"无已验证证据"处理（下方 `_known` 空分支会 loud + 机读留痕）。
        # ★F7★ 判据不依赖 `_cands` 成员资格——`_cands` 的 walk 有 `[:80]` 目录截断且跳
        # dot 目录（`.mvn/` 之类），桩写的清单若落在那些位置就**结构性零覆盖**（脏面查询
        # 真坏了也照不出来）。桩写的清单必然存在，逐个查即可。
        _selfcheck = sorted(
            p for p in _stub_w
            if p not in _dirty
            and _os.path.basename(p).lower() in (
                "pom.xml", "build.gradle", "build.gradle.kts"))
        if _selfcheck:
            # ★复核 R2-② 整改（我这条自检原来会误杀，已实跑坐实）★
            # "桩写过却不脏"有**两种**成因，判据必须区分，否则把正常情形判成故障：
            #   (a) 桩写的内容**恰等于 base 版** ⇒ 该文件真的不脏（合法，不是故障）。
            #       原实现直接 fail-closed ⇒ 把 base 里真实存在的版本也剥掉（误杀实测）。
            #   (b) 脏面查询真不可信（git 超时/rc!=0/OSError 被 callee 吞成空集、
            #       porcelain 仓根相对路径与候选口径失配）。
            # 逐个问 git 要 base 版内容：读得到且与盘上逐字相同 ⇒ (a)；否则 ⇒ (b)。
            _untrusted: list[str] = []
            _absent: list[str] = []
            for _sc in _selfcheck:
                _ap_sc = _cands.get(_sc) or _os.path.join(project_path, _sc)
                if not _os.path.isfile(_ap_sc):
                    # ★"盘上根本没这个文件" ≠ "脏面查询坏了"★ 前者是上游契约破了
                    # （`written` 声明写过但盘上没有），由 `_generate_compile_stub` 的
                    # `_missing_req` 完备性闸负责；混进本自检会让 fail-closed 误触发、
                    # 把合法坐标一起剥掉（实测打红既有测试 test_stub_gate_is_wired...）。
                    _absent.append(_sc)
                    continue
                _bt = _base_text_of(_sc)        # ★F2★ 统一走带 ./ 的读取器
                _cur = _cands_text(_ap_sc)
                # base 里读不到（桩**新建**的清单）＝它本就该显示为 untracked 脏；
                # 既然不在 _dirty 里，说明脏面查询确实不可信。
                if _bt is None or _bt != _cur:
                    _untrusted.append(_sc)
            if _absent:
                logger.warning(
                    "[阶梯三·桩] %s 写盘集声明的清单 %s 盘上不存在——上游契约异常"
                    "（不据此判脏面查询故障，完备性闸另管）", fid, _absent[:4])
            if _untrusted:
                logger.error(
                    "[阶梯三·桩] %s 脏面查询不可信：桩刚写盘且与 base 不同的清单 %s 未出现在"
                    "未提交改动集里（成因可能是 git 超时/rc!=0/OSError 被 callee 吞成空集，"
                    "或 porcelain 仓根相对路径与候选口径失配）→ fail-closed：本轮按"
                    "【无已验证证据】处理，绝不让未验证树状态给臆造坐标背书",
                    fid, _untrusted[:4])
                _dirty = set(_cands)          # 全部候选按脏（未验证）处理
                _verified = set()
            else:
                logger.info(
                    "[阶梯三·桩] %s 桩写盘的清单 %s 与 base 逐字相同 ⇒ 真的不脏（非查询故障），"
                    "自检通过不降档", fid, _selfcheck[:4])
        # ③ 证据来源分三档（★v4 复核 F1 整改：脏清单改读 base 版，不再整份剔掉★）
        #   (a) 未改动 / 已过 L1 兄弟产物 → 读**工作树**（就是已验证内容）；
        #   (b) 本轮改脏且非已验证 → 读它的**base 版**（base 按定义已验证）。
        #       ★为什么必须这样★ 原实现把脏清单**整份剔掉**，于是"桩改写既有清单"这一形态下，
        #       该清单**自己 base 里的合法坐标**（`<parent><version>`、既有依赖版本）也一并
        #       失去证据资格 ⇒ 被判"查无实据" ⇒ 连 base 真坐标一起剥/删。实测：盘上
        #       `<parent>` 的 1.0.0 与既有依赖 2.5.0 双双被删——正是 D3 铁律点名的
        #       "毁 <parent> 让整棵 reactor 解析期崩"。读 base 版同时保住 M2 的防自证：
        #       桩**新增**的臆造坐标不在 base 里，拿不到背书。
        #   (c) base 版读不到（新建文件/base 不可达）→ 该文件不提供任何证据（诚实空）。
        _known: set[str] = set()
        _from_base: list[str] = []
        _no_evidence: list[str] = []
        for _rel, _abs in _cands.items():
            if _rel in _dirty and _rel not in _verified:
                _bt = _base_text_of(_rel)
                if _bt is None:
                    _no_evidence.append(_rel)     # base 里没有（桩新建的）→ 零证据
                    continue
                _known.update(_VERSION_TAG_RE.findall(_bt))
                _from_base.append(_rel)
                continue
            try:
                with open(_abs, encoding="utf-8", errors="ignore") as _fh:
                    _known.update(_VERSION_TAG_RE.findall(_fh.read(200_000)))
            except OSError:
                continue
        # `_excluded` 保留原语义（"没能提供已验证证据的候选"），供下方 fail-closed 判档；
        # 但现在只含真正零证据的那些（base 也读不到），不再含"改脏但 base 可读"的。
        _excluded = list(_no_evidence)
        if _from_base:
            logger.info(
                "[阶梯三·桩] %s 接地证据集对 %d 份【本轮改脏】清单改读 base 版"
                "（工作树内容未验证，base 按定义已验证；桩新增的臆造坐标不在 base 里拿不到"
                "背书，而该清单自身 base 里的合法坐标不再被误判查无实据）: %s",
                fid, len(_from_base), sorted(_from_base)[:6])
        if _no_evidence:
            logger.info(
                "[阶梯三·桩] %s 接地证据集剔除 %d 份【零证据】清单（本轮改脏且 base 里读不到"
                "——桩新建的清单属此类）: %s", fid, len(_no_evidence), sorted(_no_evidence)[:6])
        if not _known:
            # ★复核 MED-3 整改：`_known` 空现在有【两种】含义，必须分开★
            # 改动前只有一种="这项目没有构建清单"（非 JVM 栈常态，fail-open 正当）。
            # 改动后多出="候选全被剔成未验证"——那恰是最该起疑的状态（树越脏闸越接近
            # no-op，而"越脏"正是它要防的），绝不能与常态共用一条静默早返。
            if _excluded:
                # fail-closed：没有任何已验证证据时，"查无实据"就是全部坐标的真实状态。
                # ★R1 整改：先把【零 base 证据】的清单整份不采纳，再对其余走逐行剥离★
                # 顺序不可换：`_no_evidence` 里的清单 base 里根本没有，逐行剥离必然剥掉
                # 它的 `<parent><version>` 与自身 `<version>` ⇒ 产出 Maven 解析不了的残骸
                # （已实跑复现）。整份不采纳后 L2 归因是"模块无 pom"而非"pom 语法坏"。
                # 有效证据集＝空（这条臂的语义），故判据里 known 传 set()
                _diff0, _rm_ok0, _rm_fail0 = _drop_whole_manifests(
                    stub_diff,
                    _ungrounded_zero_evidence_manifests(
                        stub_diff, set(), set(_no_evidence)))
                if _rm_ok0 or _rm_fail0:
                    logger.error(
                        "[阶梯三·桩] %s 接地证据集全空 ⇒ %d 份【零 base 证据】清单整份不采纳"
                        "（剥离式治法会产出有 <parent> 却无 <version> 的残骸＝reactor 解析期崩；"
                        "零 base 证据时没有任何坐标可纳入证据集，故整份丢弃 fail-honest）："
                        "diff 侧已移除并删盘 %s%s",
                        fid, len(_rm_ok0), _rm_ok0[:4],
                        f"，删盘失败 {_rm_fail0[:3]}（diff 与盘不一致！）" if _rm_fail0 else "")
                if _rm_fail0:
                    # 与 sync_disk_failed 同后果类（残留留树、L2 按本地树构建中毒）但成因
                    # 不同，用独立键——共用一个键会让两种故障在账上分不开（血规 10④）。
                    _record_degrade_safe_pc("brain.stub_grounding.drop_manifest_failed")
                # 新增行里的非 ${} version 一律剥离（${} 是引用不是坐标，照旧放行）。
                # ★两臂共用 `_strip_ungrounded_lines`★ 这条臂的有效证据集＝空，故 known 传
                # `set()`；替换对的修复逻辑对两臂同样必需（缺陷与走哪条臂无关，R1 二轮的
                # 半落地就是这么来的），绝不在此复制粘贴第二份判据。
                _kept_text0, _drop0, _dropped_text0 = _strip_ungrounded_lines(_diff0, set())
                # ★F3★ 只在真有剥离时才动盘（原来无条件跑：diff 侧 0 个可剥时盘上仍被改，
                # 日志写"剥了 0 个 ... 已同步落盘 [x]"自相矛盾，读日志的人不会去查盘）。
                _synced0, _syncfail0 = _sync_disk(_dropped_text0)
                if _syncfail0:
                    _record_degrade_safe_pc("brain.stub_grounding.sync_disk_failed")
                logger.error(
                    "[阶梯三·桩] %s 接地证据集【全空】：%d 份候选清单零证据（本轮改脏且 base"
                    "里也读不到）⇒ 无任何已验证坐标可比 → fail-closed。"
                    "整份不采纳 %d 份零证据清单%s；其余段逐行剥离 %d 个具体版本号"
                    "（${} 引用不动），落盘同步 %s%s。"
                    "查：残留清理是否失败/脏面查询是否不可信: 零证据=%s 剥离=%s",
                    fid, len(_excluded), len(_rm_ok0),
                    f"（删盘失败 {_rm_fail0[:3]}）" if _rm_fail0 else "",
                    len(_drop0), _synced0,
                    f"，落盘失败 {_syncfail0[:3]}" if _syncfail0 else "",
                    sorted(_excluded)[:4], _drop0[:6])
                _record_degrade_safe_pc("brain.stub_grounding.no_verified_evidence")
                return _kept_text0
            return stub_diff          # 真无构建清单（非 JVM 栈常态）→ 不判（fail-open）
        # ★R1 二轮整改：正常路径【同型缺陷】——我第一版只接 fail-closed 臂＝半落地★
        # 自查探针实证（`_known` 非空、base 有干净根 pom、桩新建 mod/pom.xml）：
        # `<parent><version>3.2.0` 与自身 `<version>1.0.0` 双双被剥，盘上同样剩一个
        # 有 `<parent>` 却无 `<version>` 的残骸 —— 与 fail-closed 臂**一字不差**。
        # 缺陷根在"零 base 证据的清单逐行剥离没有安全网"，那与走哪条臂无关，
        # 故判据下沉为两臂共用（`_ungrounded_zero_evidence_manifests`）。
        # 「修复必须真到得了生产」「治法只落一半」是本项目反复出现的族，这次是自查逮到的。
        _diff_n, _rm_okn, _rm_failn = _drop_whole_manifests(
            stub_diff,
            _ungrounded_zero_evidence_manifests(stub_diff, _known, set(_no_evidence)))
        if _rm_okn or _rm_failn:
            logger.error(
                "[阶梯三·桩] %s %d 份【零 base 证据】清单整份不采纳（其 diff 段含查无实据的"
                "具体版本号；新建清单被逐行剥离后没有 base 可退，会剩下有 <parent> 却无 "
                "<version> 的 Maven 解析不了的残骸）：diff 侧已移除并删盘 %s%s",
                fid, len(_rm_okn), _rm_okn[:4],
                f"，删盘失败 {_rm_failn[:3]}（diff 与盘不一致！）" if _rm_failn else "")
        if _rm_failn:
            _record_degrade_safe_pc("brain.stub_grounding.drop_manifest_failed")
        _kept_text, _dropped, _dropped_text = _strip_ungrounded_lines(_diff_n, _known)
        if _dropped:
            # 同一批被剥行同步落盘（R2-③ + F1）：口径与 diff 侧同源，绝不重跑判据。
            _synced, _syncfail = _sync_disk(_dropped_text)
            if _syncfail:
                # ★F4★ 落盘失败与 `_clean_stub_residue` 的 revert_failed **同一后果类**
                #（残留留树、L2 按本地树构建中毒），必须同档可观测：本闸够不到
                # cascade_revert_failed（在调用方作用域），故落 degrade 键 + loud。
                _record_degrade_safe_pc("brain.stub_grounding.sync_disk_failed")
                logger.error(
                    "[阶梯三·桩] %s 臆造坐标落盘剥离失败 %s ⇒ diff 已剥而盘上仍带臆造坐标"
                    "（验证树≠交付面反向形态，L2 按本地树构建会中毒）", fid, _syncfail[:4])
            logger.warning(
                "[阶梯三·桩] %s 桩里 %d 个版本号在基线构建清单中查无实据 → 已剥离该行"
                "（铁律：绝不猜依赖坐标；桩零编译零验收，臆造坐标会一路交付），"
                "已同步落盘 %s: %s",
                fid, len(_dropped), _synced, _dropped[:6])
            return _kept_text
        # ★注意返回 `_diff_n` 而非 `stub_diff`★ 整份不采纳可能已摘掉若干段而**一行都没有
        # 逐行剥离**（新建清单整段被丢，剩下的段全合法）——返回 `stub_diff` 会把整份不采纳
        # 的结果**原地丢弃**，diff 侧复活已删文件 ⇒ 与盘不一致。无可丢时 `_diff_n` 逐字等于
        # `stub_diff`（见 `_drop_whole_manifests` 的空 `rels` 早返）。
        return _diff_n
    except Exception as exc:  # noqa: BLE001 — 剥离是加固面，失败保留原桩（fail-open）
        logger.warning("[阶梯三·桩] 坐标接地校验异常（保留原桩）: %s", exc)
        return stub_diff


async def _generate_compile_stub(
    state: BrainState, st, project_path: str | None,
    protected_files: set[str] | None = None,
    required_files: set[str] | None = None,
) -> tuple[str, list[str]] | None:
    """卡死子任务恢复阶梯·阶梯三(stub)：为【被依赖】的卡死子任务生成可编译桩。

    聚焦 LLM 调用：据 X 的描述/契约/目标文件，生成各文件的【可编译桩】——保留 public 类型/
    签名让下游编译通过，方法体一律抛 not-implemented（语言对应：Java
    `throw new UnsupportedOperationException(...)`、TS `throw new Error(...)`、Go `panic(...)` 等），
    绝不留半成品坏代码。语言无关（prompt 让模型按文件后缀产出对应语言桩）。

    写入本地树后用 git 生成 diff 作为 X 的 WorkerOutput.diff（merge 纳入、L2 验证其可编译）。
    任何环节失败（无 LLM/无 project_path/解析失败/空产出）→ 返回 None，调用方回退 revert。
    桩可编译性的最终校验由下游 L2 全量编译兜底（桩编不过 → L2 失败 → 熔断升级，有界）。

    ★32 号文 A10-M1 复核整改：返回 `(diff, written)` 而非裸 diff★
    `written` ＝**真实写盘清单**，是调用方做残留清理时"哪些文件必须护住"的**单一事实源**。
    此前调用方从 diff 反推写盘集（`files_from_unified_diff`），实测两种情形会漏而**后果是
    删掉桩自己的产出**：
      ① `.gitattributes` 把某路径标 `-diff`/`binary` ⇒ `git diff` 只出
         `Binary files ... differ`、无 `+++ b/` 行 ⇒ 反推漏掉它（已用命令坐实：桩写 2 个
         文件只认出 1 个；且**部分标记比全部标记更危险**——全漏时清理整块跳过，部分漏时
         那个文件不被 protected 就被清掉）；
      ② 桩内容恰等于 base 版 ⇒ 该文件 per-file diff 为空 ⇒ 同样漏。
    diff 是**派生物**，不能当写盘事实的第二事实源。"""
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
    return diff, list(written)     # written=写盘事实单一事实源（A10-M1 复核整改）


def _verified_sibling_files(subtasks, subtask_results: dict, exclude_ids: set,
                            *, face: str) -> set[str]:
    """兄弟子任务的产物路径集。`face` **必须显式传**——两个消费点后果不同，判据也不同。

    ★共享事实源但【分档消费】（血规：复用单一事实源 ≠ 复用其消费契约）。两次踩同一坑：
      第一次是拿 scope 声明当"已验证"（hunter HIGH-1），第二次是拿桩的合成 l1_passed
      当"过了确定性闸"（reviewer R2-HIGH2）。两次都由"共用一个集合"引起，故改成显式两档。★

    · `face="protect"` → **桩路残留清理的 protected 面**。要**宽**：宁多护不误删。
      收 scope 声明 ∪ 实际 diff 路径（runner F1 同款"双保险"口径 `runner.py:1628-1635`），
      且**包含 give-up 产出**——上一轮桩的产物是真交付物（在 merged_diff 里），
      本轮清理绝不能删它。
    · `face="evidence"` → **接地闸证据面**。必须**窄**：只认"确定性闸验过这份内容"。
        ① **剔 scope 面**：scope 声明只是"**允许**谁写"，不是"盘上这份内容是它写的且过了
           L1"。重叠在生产上真实存在——`_grant_module_pom_writable` docstring 原话：
           "owner 可能是已 DONE 的脚手架子任务，**二者都写同一清单**"。实测：兄弟
           l1_passed=False 时 3.8.7 被剥离，改 True 后同一 diff 一字不改却放行。
        ② **剔 give-up 产出**：桩的 `l1_passed=True` 是阶梯三为"解锁下游、不连坐"刻意
           硬写的**合成值**，零编译零验收。不剔 ⇒ **上一轮**的桩（臆造坐标仍在盘上、
           diff 非空）给**本轮**桩洗白同一版本，而 `stub_written` 只减得掉本轮写盘集、
           够不着跨轮（实测放行）。判据用既有单一口径 `give_up_mode`/`given_up`
           （与 failure.py `_stubbed_ok`、R65D-T1 铁律同源）。

    注：即便兄弟真写过该路径并过了 L1，桩已**覆写**它 ⇒ 盘上内容不再是兄弟那份。故调用方
    还须从证据面减掉【桩本轮写盘集】（`_strip_ungrounded_manifest_coords` 的 `stub_written`）。
    """
    if face not in ("protect", "evidence"):
        raise ValueError(f"face 必须显式为 'protect'/'evidence'，得到 {face!r}")
    _evidence = face == "evidence"
    out: set[str] = set()
    if not _evidence:
        out |= {_norm_rel(p)
                for p in _files_owned_by_completed(subtasks, subtask_results, exclude_ids)}
    try:
        from swarm.brain.nodes.dispatch import _changes_from_diff
    except Exception:  # noqa: BLE001 — 解析器不可用
        # protect 面退成 scope 面＝偏窄但方向安全（少护会误删，
        # 故调用方对该退化另有 fail-closed）；证据面（False）退成空集＝一切按未验证处理，
        # 方向也安全（宁可多剥不可误放）。两档都留 WARNING。
        logger.warning(
            "[阶梯三] 兄弟产物 diff 面解析器不可用 → %s（face=%s）",
            "证据面空集（全按未验证）" if _evidence else "仅 scope 面", face)
        return out
    for s in subtasks:
        sid = getattr(s, "id", None)
        if sid in exclude_ids:
            continue
        o = subtask_results.get(sid)
        passed = ((isinstance(o, WorkerOutput) and o.l1_passed)
                  or (isinstance(o, dict) and o.get("l1_passed")))
        if not passed:
            continue
        if _evidence:
            # ★证据面专有★ 剔"l1_passed 为真但这份内容没真过确定性闸"的两类。
            # protect 面**刻意保留**它们：上一轮桩的产物是真交付物，本轮清理不得删。
            _det = ((o.l1_details if isinstance(o, WorkerOutput)
                     else o.get("l1_details")) or {})
            if isinstance(_det, dict) and (
                    # ② give-up 产出：桩的 l1_passed 是阶梯三硬写的合成值（见 docstring）
                    _det.get("given_up") or _det.get("give_up_mode")
                    # ③ ★v4 复核 F5★ L1 被降级成 `mvn validate` 的兄弟：脚手架窗口刻意
                    # 把 build_cmd 降级（真编译交 L2 兜），故**本轮源码未经编译**——同样是
                    # "l1_passed 为真 ≠ 这份内容过了确定性闸"。判据是现成机读键
                    # （`worker/l1_pipeline.py` 写、`brain/runner.py` 已在消费）。
                    # 诚实边界：`mvn validate` 会解析 POM/parent，但**未必**拉依赖 artifact，
                    # 故"它能否为一个不存在的坐标背书"我没实测确证；这里按分档缺口处理
                    # （宁可少认证据也不放行臆造坐标），代价是该兄弟的合法新版本可能被误剥
                    # ——真发生时表现为桩里被剥掉一条本该保留的 version，可由日志定位。
                    or _det.get("build_cmd_downgraded_to_validate")):
                continue
        d = (getattr(o, "diff", None)
             or (o.get("diff") if isinstance(o, dict) else "") or "")
        for ch in (_changes_from_diff(d) if d else []):
            p = _norm_rel(getattr(ch, "file_path", "") or "")
            if p:
                out.add(p)
    return out


def _clean_stub_residue(
    project_path: str, st, fid: str, *,
    stub_written: list[str],
    protected_base: set[str],
    subtasks: list,
    subtask_results: dict,
    base_ref: str | None,
    cascade_revert_failed: list[str],
) -> dict:
    """★32 号文 A10-M1★ 桩路清【非桩产出】的足迹残留。返回机读账（并入 l1_details）。

    病根＝`_give_up_preserve_build` docstring 承诺"两路都清本地树足迹"，而 stub 成功路
    此前【没有】任何清理调用。桩的可写面只有 `code_files ∪ _required`（`_CODE_EXT` 刻意
    排除 pom/配置/资源），故"非代码扩展名 ∧ 未被下游 upstream_artifacts 声明"的足迹文件
    既不打桩、也不进 diff——而它【真会被改脏】：`_grant_module_pom_writable` 把
    `<mod>/<manifest>` 就地写进 scope.writable（A2 缺依赖恢复臂，跨轮留存），brain 自己
    `_inject_dep_into_pom` 直写它，worker 亦有手改腐化实测（nodes/failure.py D3 铁律：
    写成 `<group>`/毁 `<parent>` 让整棵 reactor 解析期崩）。

    残留后果双向（毒 L2/reactor；或 L2 验的树带它而交付不带＝验证树≠交付树），且【五处】
    脏树判据全按 merged_diff/out_files 取集，结构性看不见：
      ①L2/交付 `_reset_worktree_to_head` ②F5 `_l2_tree_dirty`
      ③`files_changed_since_base` ④`uncommitted_changed_files`
      ⑤runner 终态清扫——它本会清，但 `targets` 排除 l1_passed 为真者（`runner.py:1599`），
        而桩刻意写 `l1_passed=True` 以解锁下游 ⇒ 桩被当"完成态"豁免（够不着，非兜底）。

    ── 三个易错点（都是复核逼出来的，改这里前先读）──
    1. **`stub_written` 必须是调用方传来的真实写盘集**，绝不在此从 diff 反推：
       `.gitattributes` 标 `-diff` ⇒ 无 `+++ b/` 行 ⇒ 反推漏该文件 ⇒ 它不被 protected ⇒
       **桩自己的产出被这段清理删掉**（已用命令坐实，部分标记比全部标记更危险）。
    2. **清扫面必须含 X 的越权写入**（`extra_files`）：`_subtask_footprint` 纯 scope 驱动，
       round67l st-14 实锤 worker 越权写根 pom/别模块 pom（钉版本+注册不存在模块），
       scope 够不着 ⇒ 桩路零清理者。复用 runner F1 同款做法（自身失败 diff 解析路径）。
    3. **protected 必须与清扫面同步扩宽**：一旦清到 scope 外，`_files_owned_by_completed`
       的纯 scope 保护就不够——按 runner F1 口径（`runner.py:1628-1635`）补上【已过 L1
       兄弟的实际 diff 路径】，否则会误删兄弟越权但有效的产物。
    """
    led: dict = {}
    _norm = _norm_rel   # 与清理器内部（_local_tree_revert_subtask）逐字同源，见 _norm_rel
    if not stub_written:
        # 上游契约：桩成功必有写盘集。空＝契约被破坏，机读留痕（空返回必须可辨）+ 跳过清理
        #（宁可留残留，也绝不在"不知道该护谁"时动手删）。
        led["stub_residue_skipped"] = "empty_written_set"
        # ★复核 LOW-5 整改：分档一致★ "没清成"与"清失败"后果同类（残留留在树上，五处
        # 脏树判据全按 merged_diff 取集看不见它）⇒ 必须同档进 degraded_reasons，
        # 不能一个有下游读者一个只躺 l1_details。
        cascade_revert_failed.append("stub_residue_skipped:%s:empty_written_set" % fid)
        logger.warning(
            "[阶梯三·桩] %s 写盘集为空（上游契约异常）→ 跳过残留清理并机读留痕"
            "（宁可留残留也绝不在不知该护谁时删）；残留可能毒 build", fid)
        return led
    _kept = {_norm(p) for p in stub_written}
    # 清扫面 = scope footprint ∪ X 自身失败 diff 里的路径（越权写入，scope 够不着）
    _extra: list[str] = []
    try:
        from swarm.brain.nodes.dispatch import _changes_from_diff
        _x_res = subtask_results.get(fid)
        _x_diff = (getattr(_x_res, "diff", None)
                   or (_x_res.get("diff") if isinstance(_x_res, dict) else "") or "")
        if _x_diff:
            _extra = [_norm(getattr(ch, "file_path", "") or "")
                      for ch in _changes_from_diff(_x_diff)]
            _extra = [p for p in _extra if p and p not in _kept]
    except Exception:  # noqa: BLE001 — 越权面解析失败不阻断 scope 面清理（机读留痕）
        led["stub_residue_extra_parse_failed"] = True
        cascade_revert_failed.append("stub_residue_skipped:%s:extra_parse_failed" % fid)
        logger.warning(
            "[阶梯三·桩] %s 越权写入面解析失败 → 本次只清 scope 足迹面"
            "（scope 外残留可能仍在树上）", fid, exc_info=True)
    # protected = 兄弟 scope 归属 ∪ 兄弟【实际 diff 路径】∪ 桩本轮产出（runner F1 口径，
    # 与接地闸 verified_files 同一事实源 `_verified_sibling_files`）。
    try:
        # protected 面（分档 face="protect"）：要**宽**（宁多护不误删）——
        # scope 声明也护住（兄弟越权但有效的产物 + runner F1 双保险口径）。
        _prot = ({_norm(p) for p in (protected_base or set())} | _kept
                 | _verified_sibling_files(subtasks, subtask_results, exclude_ids={fid},
                                           face="protect"))
    except Exception:  # noqa: BLE001 — 保护面算不全时绝不动手（fail-closed）
        led["stub_residue_skipped"] = "sibling_protect_unresolved"
        cascade_revert_failed.append(
            "stub_residue_skipped:%s:sibling_protect_unresolved" % fid)
        logger.warning(
            "[阶梯三·桩] %s 兄弟产物保护面解析失败 → 跳过残留清理（fail-closed，"
            "绝不在保护面不全时删文件）；残留可能毒 build", fid, exc_info=True)
        return led
    rev = _local_tree_revert_subtask(project_path, st, protected_files=_prot,
                                     base_ref=base_ref, extra_files=_extra)
    _cleaned = (rev.get("reverted") or []) + (rev.get("removed") or [])
    _failed = list(rev.get("revert_failed") or [])
    if _cleaned:
        led["stub_residue_cleaned"] = _cleaned[:12]
        logger.info(
            "[阶梯三·桩] %s 已清桩产出之外的残留 %d 个（典型=brain 授写权的模块构建清单/"
            "worker 越权写入：既不打桩也不进 diff，留树会毒 L2/reactor 且交付面无痕）: %s",
            fid, len(_cleaned), _cleaned[:8])
    if _failed:
        # 降级路径必须机读可辨 + 至少一次 WARNING，且键有消费者
        #（cascade_revert_failed → out["degraded_reasons"] reducer → blocking_degraded_reasons
        #  ⇒ 落阻断档拦 L6 成功学习）。
        led["stub_residue_revert_failed"] = _failed[:12]
        logger.error(
            "[阶梯三·桩] %s 桩路残留清理不完整 revert_failed=%s——残留仍在本地树，会毒 "
            "L2/reactor 且五处脏树判据均按 merged_diff 取集看不见它，已入 degraded_reasons",
            fid, _failed[:6])
        cascade_revert_failed.append(
            "stub_residue_revert_failed:%s:%s" % (fid, ",".join(_failed[:3])))
    return led


async def _give_up_preserve_build(state: BrainState, failed_ids: list[str]) -> dict | None:
    """卡死子任务恢复阶梯·阶梯三：保 build 放弃（替代直接 escalate 全盘 FAILED）。

    阶梯一(retry)→阶梯二(定点拆小)都耗尽仍失败、且有成功兄弟时调用。做法：
      1. 自动判依赖：`any(X in st.depends_on for st in plan.subtasks)`。
         - 被依赖 → 先试【可编译桩】(_generate_compile_stub)：下游可编译，不连坐放弃；
           桩生成失败 → 回退 revert（并传递放弃下游，缺依赖跑不了）。
         - 不被依赖 → revert（只丢 X，零连坐）。
      2. 两路都【清本地树足迹】(_local_tree_revert_subtask)，杜绝坏文件毒 -am reactor。
         口径差异（32 号文 A10-M1 起）：revert 路清【整足迹】；stub 路清【足迹 − 桩产出】
         ——桩刚写的文件经 protected 守住，其余（典型=brain 授写权的模块构建清单，
         `_CODE_EXT` 不打桩、也不进 diff）必须清掉，否则留树毒 build 而交付面无痕。
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
        # ★每个 fid 循环开头重置★：条件写的账若跨 fid 粘滞，会把上一个 fid 的清理结果
        # 记到本 fid 名下（always-emit/防粘滞纪律）。
        _resid_led: dict = {}
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
            _stub_gen = await _generate_compile_stub(state, st, project_path,
                                                     protected_files=_prot_stub,
                                                     required_files=_required_by_downstream)
            if _stub_gen is not None:
                stub_diff, _stub_written = _stub_gen
                # ★A10-M1（复核 MED-1/MED-2 整改）★ 残留清理必须排在接地闸【之前】：
                # 闸的证据集扫【当前树】，若脏残留（X 越权/腐化写的构建清单）还在盘上，
                # 它会把臆造坐标"自证"成基线已存在 ⇒ 两条治法在同一循环里彼此错身
                # （复核实跑：脏残留在场→3.8.7 放行；已清→剥离）。故先清、后判。
                _resid_led = _clean_stub_residue(
                    project_path, st, fid,
                    stub_written=_stub_written,
                    protected_base=_prot_stub,
                    subtasks=subtasks, subtask_results=subtask_results,
                    base_ref=state.get("base_commit"),
                    cascade_revert_failed=cascade_revert_failed)
                # ★桩里的构建清单必须过"绝不猜依赖坐标"铁律（26 号文 C-3）★
                # R65C-T3 例外让桩去写 pom（下游种子闸的硬要求，理由正当），但桩是 LLM 产物、
                # 零编译零验收就 l1_passed=True 解锁下游。round67m2 实证 st-3-1：桩写的父 POM
                # 版本 3.8.7 而 base 是 4.8.3 → 解析必失败，且四道闸全瞎一路交付。
                # 治法不是禁止写清单（那会让下游永堵），而是**把 LLM 臆造的坐标确定性剔掉**：
                # 桩里出现的 version 必须在基线的构建清单里真实存在过，否则该行剥离并留痕。
                # 证据面（分档 face="evidence"）：只认 diff 面且剔 give-up 产出——
                # scope 声明只是"允许谁写"，拿它当"已验证内容"会让桩自证；再由闸内
                # `stub_written` 减掉桩本轮写盘集（盘上那份就是桩刚写的）。
                stub_diff = _strip_ungrounded_manifest_coords(
                    stub_diff, project_path, fid,
                    verified_files=_verified_sibling_files(
                        subtasks, subtask_results, exclude_ids={fid},
                        face="evidence"),
                    stub_written=_stub_written,
                    base_ref=state.get("base_commit"))
        if stub_diff:
            mode = "stub"
            _l1d_stub: dict = {"given_up": True, "give_up_mode": "stub"}
            _l1d_stub.update(_resid_led)   # 残留清理账（清理已在接地闸前执行，见上）
            subtask_results[fid] = WorkerOutput(
                subtask_id=fid, diff=stub_diff,
                summary=(f"[阶梯三·桩] {fid} 卡死 → 生成可编译桩（保留 public 签名、方法体抛 "
                         "UnsupportedOperationException），下游可编译集成，需人工补完实现"),
                l1_passed=True,
                l1_details=_l1d_stub,
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
