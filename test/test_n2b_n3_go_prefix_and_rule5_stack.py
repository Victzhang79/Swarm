#!/usr/bin/env python3
"""N-2b / N-3（27 号文 §7.8 B-0 夹具落地当场抓到的两条，归 B-6）。

- **N-2b**：`_inject_go_scaffolds` 只认**根** `go.mod` 推 import 路径 ⇒ `go work init` 产的
  多模块仓（根上只有 `go.work`）**整栈零脚手架**，回到 R47/R53 病（派 worker 手写清单 + 臆造
  版本）。前缀并非推不出——兄弟 `auth/go.mod` 写着 `example.com/app/auth`、它躺在 `auth/`
  ⇒ 前缀 `example.com/app` 是磁盘上的确定性事实。治法＝`_go_module_path_prefix` 两条证据源，
  **互斥前缀 → 歧义 fail-closed**（绝不挑边臆造：假 module 路径让全仓 import 对不上，还盖着
  "权威模板"章发给 worker）。
- **N-3**：规则5（契约依赖 → 模块清单 owner）**三个消费点各自**写死 `f"{mod}/pom.xml"`
  ⇒ 任何非 Maven 栈里"无 owner"恒真。治法＝`_rule5_manifests(stack)` 单一事实源 +
  `_module_manifest_candidates` 补 R57-1 物理落点，**两个消费者按后果分档**（告警面要最宽
  认定；注入面 Maven 专属，维持逐字今日行为——那侧漏报=整模块没人建构建文件=编译失败）。

★本文件的判据是"把机制整块删掉/改坏，这条测试会不会红"★，不是"实现细节长什么样"。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    _go_module_path_prefix,
    _go_root_directive,
    _go_work_use_dirs,
    _module_manifest_candidates,
    _module_manifest_owners,
    _rule5_manifests,
    inject_build_scaffold_subtasks,
    normalize_plan_scopes,
    unclaimed_contract_deps,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan  # noqa: E402


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _st(sid, create=None, writable=None, acc=None):
    return SubTask(id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or []),
                   acceptance_criteria=acc or ["ok"])


def _plan(subtasks, contract):
    return TaskPlan(subtasks=subtasks, shared_contract=contract,
                    parallel_groups=[[s.id for s in subtasks]])


# ══════════════════════════════════════════════
# ① N-2b：module 前缀取证（两条证据源 + 歧义 fail-closed）
# ══════════════════════════════════════════════

def test_skip_dirs_split_is_element_equal_to_the_pre_split_set():
    """★拆表是纯结构改动 → 集合必须与拆前**逐元素相等**★

    `_SKIP_DIRS` 被拆成 `DEPENDENCY_TREE_DIRS ∪ _PRODUCT_DIRS`（前者供"谁的模块声明可信"这一档
    单独消费）。拆表时顺手增删一项，就是在"行为不变"的幌子下改了闸的作用域——这条把拆前的
    字面集合钉死在测试里，任何增删都必须走自己的判据与测试，而不是搭便车。
    """
    from swarm.project.sandbox_spec import _PRODUCT_DIRS, _SKIP_DIRS
    from swarm.stacks import DEPENDENCY_TREE_DIRS

    pre_split = {
        "node_modules", "target", "build", "dist", ".git", ".idea", ".vscode",
        "__pycache__", ".venv", "venv", "vendor", ".gradle", ".mvn",
        "third_party", "third-party", "3rdparty", ".yarn", ".pnpm-store",
        "bower_components", "Pods", "packages_cache", ".tox", ".eggs",
        "site-packages", ".mypy_cache", ".pytest_cache", ".next", ".nuxt",
    }
    assert set(_SKIP_DIRS) == pre_split, (
        f"拆表改变了作用域：多了 {sorted(set(_SKIP_DIRS) - pre_split)}，"
        f"少了 {sorted(pre_split - set(_SKIP_DIRS))}")
    assert set(DEPENDENCY_TREE_DIRS) | _PRODUCT_DIRS == set(_SKIP_DIRS)
    # 两半不重叠：一个目录同时被两种语义声称 ⇒ 分表的意义就没了
    assert not (set(DEPENDENCY_TREE_DIRS) & _PRODUCT_DIRS)
    # 产物目录**不得**混进依赖树表（那正是误杀 `build/tool/go.mod` 的成因）
    assert not ({"target", "build", "dist", ".gradle", ".mvn"} & set(DEPENDENCY_TREE_DIRS))


def test_n2b_root_go_mod_is_the_strongest_evidence(tmp_path):
    """根 `go.mod` 在 ⇒ 前缀就是它的 module 行（单根仓/带根模块工作区，行为不变）。"""
    root = _tree(tmp_path / "r", {"go.mod": "module example.com/root\n\ngo 1.22\n",
                                  "auth/go.mod": "module other.example/unrelated\n"})
    assert _go_module_path_prefix(str(root)) == "example.com/root", (
        "根 go.mod 存在时不该去看成员（它是最强证据，且成员可能刻意用无关路径）")


def test_n2b_prefix_derived_from_workspace_members_when_no_root_go_mod(tmp_path):
    """★N-2b 本体★ 根上只有 `go.work` ⇒ 前缀从成员 `go.mod` 反推。

    把 `_go_module_path_prefix` 的成员反推整块删掉（return None），这条立刻红。
    """
    root = _tree(tmp_path / "w", {
        "go.work": "go 1.22\n\nuse (\n\t./auth\n\t./shared\n)\n",
        "auth/go.mod": "module example.com/app/auth\n\ngo 1.22\n",
        "shared/go.mod": "module example.com/app/shared\n\ngo 1.22\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_nested_member_dir_strips_full_reldir(tmp_path):
    """成员在嵌套目录（`svc/auth`）时，前缀是去掉**整段** reldir 的结果，不是只去掉尾段。

    只去尾段会得到 `example.com/app/svc` ⇒ 新模块 module 路径多一层 `svc` ⇒ import 全仓对不上。
    """
    root = _tree(tmp_path / "n", {
        "go.work": "go 1.22\n\nuse ./svc/auth\n",
        "svc/auth/go.mod": "module example.com/app/svc/auth\n\ngo 1.22\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_conflicting_member_prefixes_are_ambiguous_not_a_guess(tmp_path):
    """★fail-closed★ 成员给出互斥前缀 ⇒ None（宁可这轮不建脚手架，绝不挑边臆造）。

    把歧义分支改成"取第一个/取多数"，这条立刻红。
    """
    root = _tree(tmp_path / "c", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t./b\n)\n",
        "a/go.mod": "module example.com/one/a\n",
        "b/go.mod": "module example.com/two/b\n",
    })
    assert _go_module_path_prefix(str(root)) is None


def test_n2b_member_whose_module_path_ignores_its_dir_gives_no_evidence(tmp_path):
    """成员 module 路径与自己落点无关（go 合法）⇒ 该成员**不产证据**，也不把整体判成歧义。

    否则一个"module 路径与目录不一致"的成员就能把整仓的脚手架毒死（过度 fail-closed）。
    """
    root = _tree(tmp_path / "m", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t./weird\n)\n",
        "a/go.mod": "module example.com/app/a\n",
        "weird/go.mod": "module totally.different/thing\n",   # 不以 /weird 结尾 → 无证据
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_vendored_go_mod_is_not_prefix_evidence(tmp_path):
    """无 go.work 时退化为扫一层子目录，但 `vendor/` 等依赖树目录**不算成员**。

    ★诱饵必须放在**直接子目录**（`vendor/go.mod`）★ 这条测试第一版把诱饵写成
    `vendor/x/go.mod`（两级深），而 `iterdir` 只看直接子目录的 `go.mod` ⇒ 诱饵**从未进入
    候选集**，测试为一个与命题无关的理由而绿（突变 harness 当场逮到：把 `_SKIP_DIRS` 过滤
    删掉它照旧绿）。夹具形状决定被测命题——本仓已吃过同型的亏。

    模块路径刻意造成 `github.com/third/vendor`（以 `/vendor` 结尾）⇒ 若被采信就会产出第二个
    前缀 `github.com/third` ⇒ 歧义 ⇒ 整栈零脚手架（把好仓拖死，比不治更坏）。
    """
    root = _tree(tmp_path / "v", {
        "a/go.mod": "module example.com/app/a\n",
        "b/go.mod": "module example.com/app/b\n",
        "vendor/go.mod": "module github.com/third/vendor\n",
        "node_modules/go.mod": "module github.com/npm/node_modules\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_go_work_declared_vendored_member_gives_no_prefix_evidence(tmp_path):
    """`go.work` 显式 `use ./vendor` 时，该成员**也不产前缀证据**。

    ★为什么这里可以用 `_SKIP_DIRS`（消费契约核对）★ 那张表的原契约是"哪个清单算**构建
    入口**"，而 `use` 成员按定义就是构建入口——所以**不能**拿它去决定"这个成员算不算工作区
    成员"。本处消费的是另一个问题："**哪个成员的 module 路径值得用来推别人的前缀**"。依赖树
    目录里的 module 路径来自第三方（`github.com/...`），拿它推兄弟前缀必然错。两者后果不同，
    故只用在**前缀取证**这一档，绝不用它把成员从工作区里剔掉（那会改变构建语义）。
    """
    root = _tree(tmp_path / "vw", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t./vendor\n)\n",
        "a/go.mod": "module example.com/app/a\n",
        "vendor/go.mod": "module github.com/third/vendor\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


@pytest.mark.parametrize("pdir", ["build", "target", "dist", ".gradle", ".mvn"])
def test_n2b_product_dir_member_is_still_valid_prefix_evidence(tmp_path, pdir):
    """★误杀面（自查复核整改）★ 产物/工具目录名**不**剔除。

    原实现读 `sandbox_spec._SKIP_DIRS`（依赖树 ∪ 产物/工具）。但这一档问的是"谁的 module 声明
    能用来推**兄弟**约定"：`build/tool` 是**本仓自己的**模块（monorepo 把工具放 `build/` 不
    违法），它的 module 路径照样是本仓约定的证据。整表过滤 ⇒ 唯一证据被剔 ⇒ 前缀推不出 ⇒
    整栈零脚手架。这正是"复用单一事实源 ≠ 复用其消费契约"在本批的形态。

    ★夹具刻意只放**一个**成员★ 第一版放了 `build/tool` + `dist/pkg` 两个，于是"只把 build
    加进依赖树表"这条突变被 `dist` 兜住照旧绿（突变 harness 当场逮到）——多余的成员会替被测
    代码背书。逐个 parametrize 才让每个名字都是**独自**承重的。
    """
    root = _tree(tmp_path / f"prod-{pdir}", {
        "go.work": f"go 1.22\n\nuse ./{pdir}/tool\n",
        f"{pdir}/tool/go.mod": f"module example.com/app/{pdir}/tool\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_dependency_tree_segment_anywhere_in_path_is_rejected(tmp_path):
    """依赖树目录出现在**中间段**也剔（`libs/node_modules/x`），不只首段。"""
    root = _tree(tmp_path / "mid", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t./libs/node_modules/x\n)\n",
        "a/go.mod": "module example.com/app/a\n",
        "libs/node_modules/x/go.mod": "module github.com/third/libs/node_modules/x\n",
    })
    assert _go_module_path_prefix(str(root)) == "example.com/app"


def test_n2b_go_work_member_outside_project_is_never_read(tmp_path, monkeypatch):
    """`use ../外部目录`（go 合法）⇒ **不读工程外的文件**。

    ★这条断"读边界"，不断前缀值（诚实说明为什么）★ 前缀值本身已被"module 路径必须以自己
    reldir 结尾"那条规则挡住了（`../outside/lib` 的 module 路径不可能以 `/../outside/lib`
    结尾）⇒ 拿前缀值当断言，把 `..` 守卫删掉照旧绿＝零区分力（突变 harness 实测）。所以这里
    直接断**它有没有去 stat/读那个路径**：那是一条独立的边界（越出工程树取证），值得自己被锁。
    """
    _tree(tmp_path / "outside", {"lib/go.mod": "module other.example/outside/lib\n"})
    root = _tree(tmp_path / "inside", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t../outside/lib\n)\n",
        "a/go.mod": "module example.com/app/a\n",
    })
    import swarm.brain.go_scaffold as gs
    seen: list[str] = []
    real = gs._go_module_path

    def _spy(project_path, mdir, mod_prefix):
        seen.append(mdir)
        return real(project_path, mdir, mod_prefix)

    # ★叶簇拆分（brain/go_scaffold.py）后 _go_module_path_prefix 的 __globals__
    # 在新模块——patch 必须打在【定义它的模块】上。打在 contract_utils 上 spy 永不
    # 触发、seen 恒空、下方断言 vacuous 绿=假绿（拆分批 reviewer CRITICAL 逮到）。
    monkeypatch.setattr(gs, "_go_module_path", _spy)
    assert gs._go_module_path_prefix(str(root)) == "example.com/app"
    assert seen, "spy 零触发 ⇒ patch 打错模块（本断言让假绿不可再现）"
    assert not [m for m in seen if m.startswith("..")], (
        f"取证越出了工程树（读了工程外的 go.mod）：{seen}")


def test_n2b_no_evidence_at_all_stays_none(tmp_path):
    """两条证据源都不成立 ⇒ None（调用方跳过脚手架并 WARNING，绝不编一个前缀）。"""
    root = _tree(tmp_path / "e", {"README.md": "x", "a/main.go": "package a\n"})
    assert _go_module_path_prefix(str(root)) is None


@pytest.mark.parametrize("text,want", [
    ("go 1.22\n\nuse (\n\t./a\n\t./b\n)\n", ["a", "b"]),          # 块形式（C4 病灶形状）
    ("go 1.22\n\nuse ./only\n", ["only"]),                        # 单行形式
    ('go 1.22\n\nuse (\n\t"./q"   // 注释\n\t./b\n)\n', ["q", "b"]),  # 引号 + 行内注释
    ("go 1.22\n", []),
])
def test_n2b_go_work_use_parsing_covers_both_forms(tmp_path, text, want):
    """`use` 块**逐行**收全成员——块内只捕获首成员正是 C4 那条治本的病灶形状。"""
    root = _tree(tmp_path / f"p{len(text)}", {"go.work": text})
    assert _go_work_use_dirs(str(root)) == want


def test_n2b_go_directive_reads_go_work_when_no_root_go_mod(tmp_path):
    """`go` 指令读工作区真值：根 go.mod → go.work → '1.21'。

    go.work 仓恒落 '1.21' 兜底时，成员写 1.21 而工作区要 1.22 ⇒ go 报
    `go.work requires go >= 1.22`（确定性构建失败）。
    """
    ws = _tree(tmp_path / "d1", {"go.work": "go 1.22\n\nuse ./a\n"})
    assert _go_root_directive(str(ws)) == "1.22"
    both = _tree(tmp_path / "d2", {"go.mod": "module x\n\ngo 1.23\n",
                                   "go.work": "go 1.22\n"})
    assert _go_root_directive(str(both)) == "1.23", "根 go.mod 优先（最强证据）"
    assert _go_root_directive(str(_tree(tmp_path / "d3", {"README.md": "x"}))) == "1.21"


def test_n2b_go_work_repo_actually_gets_scaffolds_end_to_end(tmp_path):
    """★接线判据：整条注入链在 go.work 仓上真的产出脚手架★（单测过≠生产可达）。

    这里走 `inject_build_scaffold_subtasks` 顶层入口（不是直接调 `_inject_go_scaffolds`），
    覆盖 `_should_fabricate_maven_scaffold` 分流 → go driver 这一整条路。
    """
    root = _tree(tmp_path / "e2e", {
        "go.work": "go 1.22\n\nuse (\n\t./auth\n\t./shared\n)\n",
        "auth/go.mod": "module example.com/app/auth\n\ngo 1.22\n",
        "auth/token.go": "package auth\n",
        "shared/go.mod": "module example.com/app/shared\n\ngo 1.22\n",
        "shared/util.go": "package shared\n",
    })
    plan = _plan([_st("st-1", create=["billing/handler.go"], writable=["go.work"])],
                 {"dependencies": [{"module": "billing", "artifacts": ["shared"]}]})
    injected = inject_build_scaffold_subtasks(
        plan, str(root), [{"module": "billing", "path": "billing/handler.go"}])
    assert [e["module"] for e in injected] == ["billing"], f"go.work 仓零脚手架：{injected}"
    assert injected[0]["stack"] == "go"
    desc = next(st.description for st in plan.subtasks if st.id == injected[0]["subtask_id"])
    assert "module example.com/app/billing" in desc, f"module 行不是推出的前缀：{desc}"
    assert "go 1.22" in desc, "go 指令没读 go.work 真值（会低于工作区要求 → 构建失败）"


# ══════════════════════════════════════════════
# ② N-3：规则5 栈驱动化（清单名 + 物理落点）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("stack,want", [
    ("maven", ("pom.xml",)),
    ("npm", ("package.json",)),
    ("go", ("go.mod",)),
    ("cargo", ("Cargo.toml",)),
    ("gradle", ("build.gradle", "build.gradle.kts")),   # 别名必须在全集里
    ("unknown", ("pom.xml",)),                          # back-compat
    (None, ("pom.xml",)),
])
def test_n3_rule5_manifests_are_stack_driven_with_maven_backcompat(stack, want):
    """清单名单一事实源。★gradle 必须带 `.kts` 别名★——只读单数字段=别名整列落空
    （本仓已实测过这个形态：F-1）。"""
    assert _rule5_manifests(stack) == want


def test_n3_candidates_cover_both_label_and_physical_dir():
    """候选路径两条源缺一不可：契约标签当目录（Maven 惯例）+ R57-1 物理落点。

    只保标签 ⇒ npm 侧恒 miss（`alarm` 的包真身在 `packages/alarm/`）；
    只保落点 ⇒ 落点取证失败时 Maven 行为回归。
    """
    cands = _module_manifest_candidates("alarm", ("package.json",), {"alarm": "packages/alarm"})
    assert cands == ["alarm/package.json", "packages/alarm/package.json"]
    assert _module_manifest_candidates("mod-a", ("pom.xml",), None) == ["mod-a/pom.xml"]


def test_n3_manifest_owners_are_stack_aware_and_skip_root_manifest():
    """owner 表按栈找清单，且**根**清单不算模块 owner（A5 归并判据依赖这一点）。"""
    sts = [_st("st-root", create=["package.json"]),
           _st("st-a", create=["packages/alarm/package.json"]),
           _st("st-b", writable=["packages/notify/package.json"])]
    npm_owners = _module_manifest_owners(sts, ("package.json",))
    assert set(npm_owners) == {"alarm", "notify"}, "根 package.json 被当成模块 owner 了"
    assert _module_manifest_owners(sts, ("pom.xml",)) == {}, "pom 口径不该认 npm 清单"


def test_n3_npm_plan_no_longer_reports_false_unclaimed():
    """★N-3 本体★ npm 清单已建 ⇒ 栈感知口径报空；Maven 缺省口径仍报落空。

    第二个断言是**区分力锚**：没有它，"把函数改成无条件返 []"也能让第一条绿。
    """
    plan = _plan(
        [_st("st-1", create=["packages/alarm/package.json", "packages/alarm/src/index.ts"]),
         _st("st-2", create=["packages/notify/package.json", "packages/notify/src/index.ts"])],
        {"dependencies": [{"module": "alarm", "artifacts": ["axios"]},
                          {"module": "notify", "artifacts": ["alarm"]}]})
    dirs = {"alarm": "packages/alarm", "notify": "packages/notify"}
    assert unclaimed_contract_deps(plan, stack="npm", dirs=dirs) == []
    assert [e["module"] for e in unclaimed_contract_deps(plan)] == ["alarm", "notify"], (
        "缺省口径也返空 ⇒ 不是'按 npm 口径找到 owner'，而是函数被拧成 fail-open")


def test_n3_truly_unclaimed_module_is_still_reported_under_its_own_stack():
    """★不许把闸拧成 fail-open★ 该栈口径下真的没人建清单 ⇒ 照旧报落空。

    ★三个清单 owner★：A5 归并（既有行为）在**唯一** owner 时恒返空——那是"逻辑模块全落进
    单物理模块"的场景。要测"真落空仍报"，plan 里必须有 2+ 个 distinct owner。
    """
    plan = _plan(
        [_st("st-1", create=["packages/alarm/package.json"]),
         _st("st-2", create=["packages/audit/package.json"]),
         _st("st-3", create=["packages/notify/src/index.ts"])],   # notify 无清单 owner
        {"dependencies": [{"module": "alarm", "artifacts": ["axios"]},
                          {"module": "notify", "artifacts": ["axios"]}]})
    dirs = {"alarm": "packages/alarm", "audit": "packages/audit",
            "notify": "packages/notify"}
    got = unclaimed_contract_deps(plan, stack="npm", dirs=dirs)
    assert [e["module"] for e in got] == ["notify"]


def test_n3_maven_call_is_byte_identical_to_the_old_hardcoded_behavior():
    """★注入面 back-compat★ 不传 stack/dirs 时结果与旧 `f"{mod}/pom.xml"` 口径逐字一致。

    注入面漏报=该模块没人建 pom=整模块编译失败，所以这一侧刻意**不**享受宽认定：
    第二个断言里 `mod-b` 的 pom 真身在 `nested/mod-b/`，缺省口径**照旧报落空**（会去建
    `mod-b/pom.xml`）——这正是今日行为，本批不动它。
    """
    plan = _plan(
        [_st("st-1", create=["mod-a/pom.xml"]),
         _st("st-2", create=["nested/mod-b/pom.xml"]),
         _st("st-3", create=["mod-c/src/X.java"])],
        {"dependencies": [{"module": "mod-a", "artifacts": ["x:y"]},
                          {"module": "mod-b", "artifacts": ["p:q"]},
                          {"module": "mod-c", "artifacts": ["r:s"]}]})
    assert [e["module"] for e in unclaimed_contract_deps(plan)] == ["mod-b", "mod-c"]
    # 告警面（传 dirs）才认物理落点：mod-b 的 pom 确已有 owner ⇒ 不再刷它的假警报
    assert [e["module"] for e in unclaimed_contract_deps(
        plan, stack="maven", dirs={"mod-b": "nested/mod-b"})] == ["mod-c"]


def test_n3_npm_go_driver_does_not_depend_on_unclaimed():
    """★纠正原登记的一句话★ npm/go driver 不经过 `unclaimed_contract_deps`。

    原文说"异栈能进 driver 只因'无 pom owner'对非 Maven 模块恒真（歪打正着）"——不准确：
    分流在 `_should_fabricate_maven_scaffold` 处就早返，driver 走的是 `_contract_dep_entries`。
    这条测试钉住那个事实：**即使把 unclaimed 打成恒空**，npm 脚手架照旧注入。
    """
    import swarm.brain.contract_utils as cu

    plan = _plan([_st("st-1", create=["packages/alarm/src/index.ts"], writable=["package.json"])],
                 {"dependencies": [{"module": "alarm", "artifacts": []}]})
    orig = cu.unclaimed_contract_deps
    cu.unclaimed_contract_deps = lambda *a, **k: []      # 拧成恒空
    try:
        injected = cu.inject_build_scaffold_subtasks(
            plan, None, [{"module": "alarm", "path": "packages/alarm/src/index.ts"}])
    finally:
        cu.unclaimed_contract_deps = orig
    assert [e["module"] for e in injected] == ["alarm"], (
        f"npm 脚手架依赖 unclaimed（原登记的隐性耦合成真了）：{injected}")
    assert injected[0]["stack"] == "npm"


def test_n3_validator_warns_are_stack_aware(tmp_path):
    """★接线判据（第三个调用点）★ `plan_validator` 侧必须真的按栈判，不只是原语支持。

    "加机制先数调用点，一个不落地列出来"——本批三个消费点：unclaimed 机读面 / A5 归并 /
    验收行注入。这条钉住**告警面**：npm 清单已建 ⇒ 规则5 那条 warn 不再出现。
    把 `validate_contract_ownership` 里的 `stack=`/`dirs=` 传参删掉，这条立刻红。
    """
    from swarm.brain.plan_validator import validate_contract_ownership

    root = _tree(tmp_path / "pv", {
        "package.json": '{"workspaces":["packages/*"]}',
        "packages/alarm/package.json": "{}", "packages/alarm/src/index.ts": "export {}\n",
        "packages/audit/package.json": "{}", "packages/audit/src/index.ts": "export {}\n",
    })
    contract = {"dependencies": [{"module": "alarm", "artifacts": ["axios"]},
                                 {"module": "audit", "artifacts": ["zod"]}]}
    plan = _plan([_st("st-1", create=["packages/alarm/src/index.ts"],
                      writable=["packages/alarm/package.json"]),
                  _st("st-2", create=["packages/audit/src/index.ts"],
                      writable=["packages/audit/package.json"])], contract)
    res = validate_contract_ownership(plan, contract, project_path=str(root))
    rule5 = [w for w in res.warnings if "规则5" in w]
    assert rule5 == [], f"npm 清单已建仍刷规则5 假警报（N-3）：{rule5}"

    # 区分力锚：真的没人建清单时**照旧**报（否则上一条的绿可能来自"整条 warn 被删掉"）。
    # ★仍需 2+ distinct owner★：A5 归并在唯一 owner 时恒返空（既有行为，非本批引入）。
    _tree(root, {"packages/extra/package.json": "{}",
                 "packages/extra/src/index.ts": "export {}\n"})
    contract2 = {"dependencies": [{"module": "alarm", "artifacts": ["axios"]},
                                  {"module": "extra", "artifacts": ["lodash"]},
                                  {"module": "audit", "artifacts": ["zod"]}]}
    plan2 = _plan([_st("st-1", create=["packages/alarm/src/index.ts"],
                       writable=["packages/alarm/package.json"]),
                   _st("st-2", create=["packages/extra/src/index.ts"],
                       writable=["packages/extra/package.json"]),
                   _st("st-3", create=["packages/audit/src/index.ts"])], contract2)
    res2 = validate_contract_ownership(plan2, contract2, project_path=str(root))
    assert [w for w in res2.warnings if "规则5" in w and "audit" in w], (
        f"audit 无清单 owner 却不报 ⇒ 闸被拧成 fail-open：{res2.warnings}")


def test_n3_rule5_acceptance_note_uses_the_real_manifest(tmp_path):
    """规则5 验收行注入面：npm 工程拿到 `packages/alarm/package.json`，不是 `alarm/pom.xml`。

    治前 owner 恒 None ⇒ **一条验收都不注入**（npm 模块清单 owner 明明在 plan 里）。
    """
    root = _tree(tmp_path / "npm", {"package.json": '{"workspaces":["packages/*"]}',
                                    "packages/alarm/package.json": "{}",
                                    "packages/audit/package.json": "{}"})
    plan = _plan(
        [_st("st-1", create=["packages/alarm/src/index.ts"],
             writable=["packages/alarm/package.json"]),
         _st("st-2", create=["packages/audit/src/index.ts"],
             writable=["packages/audit/package.json"])],   # 2+ owner：绕开 A5 单 owner 归并
        {"dependencies": [{"module": "alarm", "artifacts": ["axios"]}]})
    normalize_plan_scopes(plan, project_path=str(root))
    acc = plan.subtasks[0].acceptance_criteria
    notes = [a for a in acc if "必须声明依赖" in a]
    assert notes, f"npm owner 一条规则5 验收都没拿到：{acc}"
    assert "packages/alarm/package.json 必须声明依赖" in notes[0], notes
    assert "pom.xml" not in notes[0], f"npm 工程的验收行提到了 pom：{notes[0]}"


def test_n3_unresolved_physical_dir_degrades_toward_reporting_not_silence(tmp_path):
    """★诚实边界的方向性★ 落点取证不出（歧义/无证据）⇒ 退回标签口径 ⇒ **照旧报**落空。

    两个新调用点（plan_validator 的告警面、normalize 规则5）都**没有** `file_plan`——而
    `file_plan` 是 `_resolve_module_dirs` 里覆盖名字匹配的权威证据源（注入面有，它传）。所以
    这两处的落点证据**弱于**注入面：某些模块会解析不出来，退回"标签当目录"。

    这条钉住那个退化的**方向**：退化后行为 = 治前行为（可能多刷一条噪声警报），**不是**静默
    放行。fail-open 才是事故——那会让"该模块没人建构建文件"这件事没人知道。
    """
    plan = _plan(
        [_st("st-1", create=["packages/alarm/package.json"]),
         _st("st-2", create=["packages/audit/package.json"])],
        {"dependencies": [{"module": "ghost", "artifacts": ["axios"]}]})   # ghost 无任何落点
    got = unclaimed_contract_deps(plan, stack="npm", dirs={})   # 取证失败 → 空 dirs
    assert [e["module"] for e in got] == ["ghost"], (
        f"落点取证失败时静默放行了（fail-open）：{got}")


def test_n3_note_names_the_path_the_owner_actually_writes(tmp_path):
    """验收行必须点名 owner **真写的那条**候选，不是候选表里的某个固定位。

    R57-1 错位现场：契约标签 `mod-a`，物理落点取证给 `nested/mod-a`，而 plan 里的 owner 写的
    是扁平的 `mod-a/pom.xml`。此时验收行若点名 `nested/mod-a/pom.xml`，就是叫 owner 去改一个
    它 scope 里根本没有的文件（scope_guard 拦 → empty_diff），把落点错误再传播一次。
    """
    root = _tree(tmp_path / "skew", {"pom.xml": "<project/>", "mod-a/pom.xml": "<project/>",
                                     "nested/mod-a/src/main/java/com/x/A.java": "class A{}",
                                     "mod-b/pom.xml": "<project/>"})
    plan = _plan([_st("st-1", create=["nested/mod-a/src/main/java/com/x/B.java"],
                      writable=["mod-a/pom.xml"]),
                  _st("st-2", create=["mod-b/src/main/java/com/x/C.java"],
                      writable=["mod-b/pom.xml"])],
                 {"dependencies": [{"module": "mod-a", "artifacts": ["x:y"]}]})
    normalize_plan_scopes(plan, project_path=str(root))
    notes = [a for a in plan.subtasks[0].acceptance_criteria if "必须声明依赖" in a]
    assert notes, f"owner 一条规则5 验收都没拿到：{plan.subtasks[0].acceptance_criteria}"
    assert notes[0].startswith("mod-a/pom.xml 必须声明依赖"), (
        f"验收行点名的不是 owner 真写的 `mod-a/pom.xml`：{notes[0]}")


def test_n3_maven_rule5_acceptance_note_text_unchanged(tmp_path):
    """★Maven 措辞逐字不变★（既有测试断的就是这串字面量，本批不许动它）。"""
    root = _tree(tmp_path / "mvn", {"pom.xml": "<project/>",
                                    "mod-a/pom.xml": "<project/>"})
    plan = _plan([_st("st-1", create=["mod-a/src/main/java/com/x/A.java"],
                      writable=["mod-a/pom.xml"])],
                 {"dependencies": [{"module": "mod-a", "artifacts": ["x:y"]}]})
    normalize_plan_scopes(plan, project_path=str(root))
    notes = [a for a in plan.subtasks[0].acceptance_criteria if "必须声明依赖" in a]
    assert notes == ["mod-a/pom.xml 必须声明依赖: ['x:y']（缺一即整模块 mvn compile 失败）"], notes
