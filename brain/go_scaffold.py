"""go 构建脚手架叶簇（自 contract_utils.py 拆出，纪律#9 god-file 不再喂肥）。

机制编号与行为逐字保留（N-2b 前缀取证 / cr#1-#3 owner-backfill 与 MODIFY replace /
hunter R2 H-1 held 扣下 / R58-3 backfill 告警 / #31-P2c 注入），本批是纯结构移动：

- 8 函数本体零改动；与 contract_utils 的共享助手（_contract_dep_entries /
  _prune_scaffold_contract_entry / _p2_wrap / _wire_scaffold_ownership 等同簇消费、
  npm/python/cargo/gradle 各 driver 也在用的）留在 contract_utils，本模块【函数级
  import】反向取之——因此 contract_utils 可以顶层 import 本模块做 re-export
  （保可寻址，既有调用点/测试零改动），无循环 import。
- `_go_relpath` 因 npm 侧（contract_utils :4293 一带）共享，留在 contract_utils。
- 等值锁：test/test_go_scaffold_extract.py 逐元素对象同一性 + 注册表接线。
"""
from __future__ import annotations

import logging
from pathlib import Path

from swarm.stacks import DEPENDENCY_TREE_DIRS

logger = logging.getLogger(__name__)

def _go_root_module_path(project_path: str | None) -> str | None:
    if not project_path:
        return None
    f = Path(project_path) / "go.mod"
    try:
        if f.is_file():
            import re as _re
            m = _re.search(r"^module\s+(\S+)", f.read_text("utf-8", errors="replace"), _re.M)
            return m.group(1) if m else None
    except OSError:
        pass
    return None


def _go_root_directive(project_path: str | None) -> str:
    """工作区级 `go X.Y` 指令（读真值，不猜）：根 `go.mod` → 根 `go.work` → 保守 '1.21'。

    ★N-2b 配套★ go.work 多模块仓根上**没有** go.mod，而 `go.work` 自己带 `go 1.22` 指令
    （`go work init` 的产物）——那就是工作区权威版本。旧实现只认根 go.mod ⇒ 这类仓恒落
    '1.21' 兜底：比工作区真值低时，成员 go.mod 写 1.21 而工作区要求 1.22，go 会报
    `go.work requires go >= 1.22`。指令是**语言版本**不是依赖版本，磁盘上有真值就该读。
    """
    if project_path:
        import re as _re
        for name in ("go.mod", "go.work"):
            f = Path(project_path) / name
            try:
                if f.is_file():
                    m = _re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)",
                                   f.read_text("utf-8", errors="replace"), _re.M)
                    if m:
                        return m.group(1)
            except OSError:
                continue
    return "1.21"


def _go_work_use_dirs(project_path: str | None) -> list[str]:
    """根 `go.work` 的 `use` 成员目录（相对根，已归一）。无 go.work/读不到 → []。

    块形式 `use (\\n\\t./a\\n\\t./b\\n)` 与单行 `use ./a` 都收——★块内只捕获首成员是
    C4 那条治本的病灶形状★（`worker/workspace_manifest._reconcile_go_work` 同款逐行解析）。
    """
    if not project_path:
        return []
    f = Path(project_path) / "go.work"
    try:
        if not f.is_file():
            return []
        text = f.read_text("utf-8", errors="replace")
    except OSError:
        return []
    import re as _re

    def _norm(entry: str) -> str:
        e = entry.split("//", 1)[0].strip().strip('"')
        if e.startswith("./"):
            e = e[2:]
        return e.strip("/")

    out: list[str] = []
    for blk in _re.finditer(r"use\s*\((.*?)\)", text, _re.S):
        for line in blk.group(1).splitlines():
            e = _norm(line)
            if e:
                out.append(e)
    for m in _re.finditer(r"(?m)^\s*use\s+(?!\()(\S+)", text):
        e = _norm(m.group(1))
        if e:
            out.append(e)
    return list(dict.fromkeys(out))


def _go_module_path_prefix(project_path: str | None) -> str | None:
    """内部模块 import 路径的**前缀**（新模块 `<prefix>/<reldir>` 即其 module path）。

    ★N-2b 治本（B-0 夹具落地当场抓到）★ 旧实现只认根 `go.mod` 的 module 行，而 **go.work
    多模块仓根上没有 go.mod**（`go work init` 只产 go.work）⇒ prefix=None ⇒ 每个新模块
    `self_path=None` ⇒ `continue` ⇒ **整栈零脚手架**，回到 R47/R53 病（派 worker 手写清单 +
    臆造版本）。而前缀并非推不出：兄弟 `auth/go.mod` 写着 `example.com/app/auth`、它躺在
    `auth/` ⇒ 前缀 `example.com/app` 是**磁盘上的确定性事实**，只是没接这条证据源
    （L4："栈中立 ≠ 一律跳过"）。

    证据序（都不成立才 None）：
      ① 根 `go.mod` 的 module 行——单根仓/带根模块的工作区，最强证据；
      ② 兄弟成员 `<dir>/go.mod` 的 module 行**去掉自己的 reldir 尾巴**后的公共前缀。
         成员集来自 `go.work` 的 `use`（权威），无 go.work 时退化为根下一层子目录扫描。

    ★歧义即 None（fail-closed，绝不挑边）★ 成员给出**多个不同**前缀（真多仓合并、或某成员
    module 路径与目录名无关）→ WARNING + None：宁可这一轮不建脚手架（下游 VALIDATE 打回、
    worker 拿不到清单模板），也绝不臆造一个假 module 路径——假路径会让 import 全仓对不上，
    且盖着"权威模板"章发出去（R47 血泪）。成员 module 路径不以自己 reldir 结尾时该成员
    **不产前缀证据**（它没说任何关于兄弟的事），不因此把整体判成歧义。
    """
    if not project_path:
        return None
    root_mod = _go_root_module_path(project_path)
    if root_mod:
        return root_mod
    members = _go_work_use_dirs(project_path)
    if not members:
        try:
            members = sorted(p.name for p in Path(project_path).iterdir()
                             if p.is_dir() and (p / "go.mod").is_file())
        except OSError:
            return None
    # 依赖树目录的剔除**只在下面这一处**（本循环的 `_SKIP_DIRS` 判据），两条成员来源都过它。
    # 此前上面那个分支里还过滤了一遍 ⇒ 两处过滤同一件事 ⇒ 任一处单独突变都被另一处兜住 ⇒
    # 两条都不可证伪（突变 harness 当场逮到两条零区分力）。冗余防御看着"更安全"，实际是让
    # 机制失效时无人知晓。
    prefixes: dict[str, str] = {}      # 前缀 → 首个给出它的成员目录（诊断用）
    for rel in members:
        rel_n = str(rel).replace("\\", "/").strip("/")
        # ★只剔【依赖树】目录，不剔产物目录（复核整改）★ 这一档问的是"谁的 module 声明能用来推
        # 兄弟约定"：`vendor/x` 是第三方命名（不能），而 `build/tool` 是**本仓自己的**模块
        # （monorepo 把工具放 build/ 不违法，其 module 路径照样是本仓约定的证据）。原实现读
        # `sandbox_spec._SKIP_DIRS`（依赖树 ∪ 产物）＝把合法证据也剔了 → 前缀推不出 → 整栈零
        # 脚手架（误杀，比不治更坏）。两表关系见 `stacks.DEPENDENCY_TREE_DIRS` 的 docstring。
        # `..` 开头＝go.work 指向工程外（合法但越界）：不读工程外文件，也不拿它当前缀证据。
        if (not rel_n or rel_n.startswith("..")
                or any(seg in DEPENDENCY_TREE_DIRS for seg in rel_n.split("/"))):
            continue
        mp = _go_module_path(project_path, rel_n, None)   # 只读磁盘事实，禁递归推导
        if not mp:
            continue
        if mp == rel_n or not mp.endswith("/" + rel_n):
            # 该成员的 module 路径与它的落点无关（合法：go 不要求两者一致）→ 无前缀证据。
            continue
        prefixes.setdefault(mp[: -(len(rel_n) + 1)], rel_n)
    if len(prefixes) == 1:
        prefix = next(iter(prefixes))
        logger.info(
            "[SCAFFOLD-INJECT] N-2b 无根 go.mod → 从工作区成员确定性推出 module 前缀 %r"
            "（证据：%s/go.mod 的 module 行）", prefix, prefixes[prefix])
        return prefix
    if len(prefixes) > 1:
        logger.warning(
            "[SCAFFOLD-INJECT] N-2b 工作区成员给出 %d 个互斥 module 前缀 %s → 歧义，"
            "拒绝推导（绝不挑边臆造 module 路径：假路径让全仓 import 对不上，还盖着权威模板章）",
            len(prefixes), sorted(prefixes))
    return None


def _go_module_path(project_path: str | None, mdir: str, mod_prefix: str | None) -> str | None:
    """内部 module import 路径：磁盘 go.mod module 行（事实来源）→ `mod_prefix + reldir`
    （go 惯例，可推导非猜）→ None（无从确定 import 路径，绝不臆造一个假路径）。

    `mod_prefix` 由 `_go_module_path_prefix` 统一取证（根 go.mod → 工作区成员反推）；
    ★本函数内部传 None 用于"只读磁盘事实"★（前缀取证自身必须不递归，否则循环论证）。
    """
    if project_path:
        f = Path(project_path) / mdir / "go.mod"
        try:
            if f.is_file():
                import re as _re
                m = _re.search(r"^module\s+(\S+)", f.read_text("utf-8", errors="replace"), _re.M)
                if m:
                    return m.group(1)
        except OSError:
            pass
    if mod_prefix:
        return f"{mod_prefix}/{mdir.strip('/')}"
    return None


def _render_go_mod(module_path: str, go_directive: str, kept,
                   internal: list[tuple[str, str]]) -> str:
    lines = [f"module {module_path}", "", f"go {go_directive}"]
    if kept:
        lines.append("")
        lines.append("require (")
        lines += [f"\t{d.module} {d.version}" for d in kept]
        lines.append(")")
    for mp, target in internal:
        lines.append("")
        lines.append(f"require {mp} v0.0.0")
        lines.append(f"replace {mp} => {target}")
    return "\n".join(lines) + "\n"


def _go_dep_block(manifest_rel: str, self_path: str, go_directive: str,
                  kept, replaces: list[tuple[str, str]], exists: bool) -> str:
    """go 清单机器块：CREATE→权威 go.mod 模板（含 require+replace）；MODIFY→修改铁律 + 缺失
    require 片段 **+ 内部 replace 指令**。★cr#3 CONFIRMED HIGH★：replace 此前只在 CREATE 落，
    MODIFY（既有 go.mod）内部依赖的 replace 被整段丢 → 只依赖内部 module 的模块零指引。"""
    if not exists:
        return (f"\n【权威 go.mod 模板（确定性生成，原样写入 {manifest_rel}）】"
                f"\n```\n{_render_go_mod(self_path, go_directive, kept, replaces)}\n```")
    parts: list[str] = []
    if kept:
        reqs = "\n".join(f"\t{k.module} {k.version}" for k in kept)
        parts.append(f"require (\n{reqs}\n)")
    for mp, target in replaces:   # cr#3：MODIFY 路径也必须落 replace（内部 module 相对路径）
        parts.append(f"require {mp} v0.0.0\nreplace {mp} => {target}")
    snip = ("\n```\n" + "\n\n".join(parts) + "\n```") if parts else ""
    return (f"\n【既有 go.mod 修改铁律（{manifest_rel} 已存在）】只做最小增量：绝不整体替换/重写，"
            "绝不删除既有 require/replace，仅追加下列缺失项（内部 module 必须带 replace 指向本地"
            "相对路径，绝不去 proxy 拉）：" + snip)


def _inject_go_scaffolds(plan, project_path, file_plan, dirs,
                         unverified_out: dict | None = None) -> list[dict]:
    """go per-go.mod driver（对抗双复核整改版）：内部 module 标识取【全物理模块集】(dirs)、同源剪
    shared_contract、已认领 go.mod 走 owner-backfill、unclaimed 注入脚手架。第三方 require 版本经
    go proxy 解析（vX.Y.Z），内部 module → replace 指向本地相对路径（零网络）。解析不到如实丢弃；
    无根 go.mod 时 import 路径不可推导 → 跳过（绝不臆造假路径）。"""
    from swarm.brain.go_registry import resolve_go_deps
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskIntent
    # 叶簇拆分（纪律#9）：共享助手留 contract_utils（npm/python/cargo/gradle driver
    # 同用），函数级 import 反向取——本模块顶层不 import contract_utils ⇒
    # contract_utils 顶层 re-export 本模块无循环。
    from swarm.brain.contract_utils import (
        _contract_dep_entries, _contract_module_labels,
        _go_relpath, _manifest_owner_subtask, _p2_wrap,
        _prune_scaffold_contract_entry, _record_unverified_deps,
        _refresh_scaffold_owner_contract, _upsert_owner_manifest_block,
        _wire_scaffold_ownership,
    )

    mods_all = _contract_dep_entries(plan, dirs)
    if not mods_all:
        return []
    # ★N-2b★ 前缀取证走 `_go_module_path_prefix`（根 go.mod → go.work 成员反推），不再只认
    # 根 go.mod——go.work 仓根上没有 go.mod，旧口径下这类仓 100% 零脚手架。
    mod_prefix = _go_module_path_prefix(project_path)
    go_directive = _go_root_directive(project_path)
    # ★内部 import 路径全集从【全 dirs】取★（磁盘 go.mod module 或 根 module+reldir 推导）；含已
    # 认领/跨轮模块，绝不让内部 module 被当第三方去 proxy 误解析（cr#2/hunter#2）。不可推导者不入集。
    internal_paths: dict[str, str] = {}   # module_label → canonical import path
    path_to_dir: dict[str, str] = {}      # canonical import path → physical dir
    for m, d in dirs.items():
        p = _go_module_path(project_path, d, mod_prefix)
        if p:
            internal_paths[m] = p
            path_to_dir[p] = d
    internal_ids = set(path_to_dir)       # 只用【规范 import 路径】做内部判定，避免裸标签泄进 replace
    # ★hunter R2 H-1★ 契约声明了但解析不出物理落点的模块【也是内部模块】——pre-split 扣下：
    # 绝不送 proxy（同名公网 module 会被物化进权威 go.mod），也不生成 replace（无落点=
    # 臆造路径），留契约 + WARNING（python 不物化同律）。
    _labels = _contract_module_labels(plan)
    injected: list[dict] = []
    backfilled: list[str] = []
    existing_ids = {st.id for st in plan.subtasks}
    for entry in mods_all:
        mod, mdir, arts = entry["module"], entry["dir"], entry["artifacts"]
        manifest_rel = f"{mdir}/go.mod"
        exists = bool(project_path) and (Path(project_path) / manifest_rel).is_file()
        # 契约可能用【模块标签】或【import 路径】引用内部 module → 统一归一成规范 import 路径，
        # 令 resolve_go_deps 的内部判定与下方 replace 生成同用一套规范键（杜绝裸标签泄进 go.mod）。
        held = [a for a in arts if a in _labels and a not in internal_paths]
        if held:
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2c 模块 %s 的 %d 个内部 go 依赖无物理落点 → 不送"
                " proxy 也不生成 replace（留契约，物理落点补齐后再物化）: %s",
                mod, len(held), held)
        _norm_arts = [internal_paths.get(a, a) for a in arts if a not in held]
        kept, internal_mods, dropped = resolve_go_deps(
            _norm_arts, internal_modules=internal_ids, project_path=project_path)
        # F-2：先于任何 continue 记账（go driver 同样三条出口，含 self_path=None 那条）
        _record_unverified_deps(unverified_out, mod, kept)
        if dropped:
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2c 模块 %s 的 %d 个 go 依赖无法确定性解析版本 → 三处剔除: %s",
                mod, len(dropped), dropped)
        # 内部依赖 → replace <import路径> => <相对路径>（本模块目录视角看目标模块目录）
        replaces = [(im, _go_relpath(mdir, path_to_dir[im]))
                    for im in internal_mods if im in path_to_dir]
        final_names = [k.module for k in kept] + list(internal_mods) + held   # 契约=第三方+内部路径+held
        self_path = _go_module_path(project_path, mdir, mod_prefix)
        if not self_path:
            # hunter LOW：无 self_path=整个 go.mod 脚手架跳过 → 契约**不剪**（本轮该模块无任何清单
            # 工作，剪了会让契约与"没做的事"错位；下一轮 self_path 可推导时再同源剪）。
            logger.warning(
                "[SCAFFOLD-INJECT] #31-P2c 模块 %s 无 module 前缀可推导 import 路径（根 go.mod 与"
                " go.work 成员两条证据源都不成立/互斥）→ 跳过 go.mod 脚手架"
                "（绝不臆造一个假 module 路径污染构建）", mod)
            continue
        _prune_scaffold_contract_entry(plan, mod, final_names, dropped)   # hunter#3 同源剪契约
        block = _go_dep_block(manifest_rel, self_path, go_directive, kept, replaces, exists)
        owner = _manifest_owner_subtask(plan.subtasks, manifest_rel)
        if owner is not None:   # cr#1：已认领 → backfill 进 owner（含 MODIFY 的 replace，cr#3）
            _refresh_scaffold_owner_contract(owner, mod, mdir, final_names)   # 2a 闸同步
            if _upsert_owner_manifest_block(owner, manifest_rel, block):
                backfilled.append(owner.id)
            continue
        sid = f"st-scaffold-{mod}"
        if sid in existing_ids:
            continue
        scaffold = SubTask(
            id=sid,
            description=(f"【构建脚手架】为模块 {mod} " + ("补齐" if exists else "创建")
                        + f" go 清单 {manifest_rel}：声明契约依赖全部 module"
                        "（写代码的子任务碰不到构建清单，缺一个=整模块编译失败）"
                        + _p2_wrap(manifest_rel, block)),
            intent=TaskIntent.MODIFY if exists else TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.TRIVIAL,
            scope=FileScope(writable=[manifest_rel] if exists else [],
                            create_files=[] if exists else [manifest_rel]),
            contract={"dependencies": [{"module": mod, "dir": mdir, "artifacts": final_names}]},
            acceptance_criteria=[f"{manifest_rel} 声明契约依赖全部 module，`go build ./...` 通过"],
        )
        plan.subtasks.append(scaffold)
        existing_ids.add(sid)
        _wire_scaffold_ownership(plan, sid, mdir, manifest_rel)
        if plan.parallel_groups:
            plan.parallel_groups.insert(0, [sid])
        injected.append({"module": mod, "subtask_id": sid, "artifacts": final_names,
                         "manifest_exists": exists, "stack": "go"})
    if injected:
        logger.info("[SCAFFOLD-INJECT] #31-P2c go 脚手架注入 %d 个: %s",
                    len(injected), [e["module"] for e in injected])
    if backfilled:
        logger.warning("[SCAFFOLD-INJECT] #31-P2c R58-3 go：%d 个 owner 自认领 go.mod → 已把确定性"
                       "清单块嵌进其 description（有 owner≠有模板）: %s", len(backfilled), backfilled[:8])
    return injected
