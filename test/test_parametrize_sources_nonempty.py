"""#29-4 T-8：**参数化源不得为空** —— 空 `parametrize` 生成 0 用例却计绿。

## 病理

`@pytest.mark.parametrize("x", _source())` 里的 `_source()` 在 **collection 期**求值。
若它返回空列表，pytest 生成 **0 个用例**，而 `-q` 下 0 个用例既不红也不 skip ——
整条守卫**静默消失**。本仓惯用命令还带 `-p no:warnings`，连 pytest 可能给的结构性
告警一起吞掉，于是没有任何信号。

最贵的一处：`test_plan_quality_bench.py:29` 的 `_params()` 在 collection 期调
`run_all()` 读 manifest+夹具。夹具读不到 → 返回空 → **整个规划质量基准静默产出 0 用例**，
而它守的是"每改 brain 规划期确定性 pass，真实 E2E 失败 plan 必须仍被治好"。

## 诚实边界

当前**未发生**（4 个夹具都 tracked、`known_gap` 计数 0，实测 4/92/17 用例）。
这是**配置层潜伏面**，本文件是给它装地板，不是修一个正在流血的伤口。

## 为什么不改 `-p no:warnings`

那是命令行惯用参数（CI 与本地都带），改它会影响全部 7000+ 用例的输出面，属独立
决策。装地板是**局部且确定**的治法：直接断"源函数非空"，不依赖告警是否可见。

## 枚举来源（不是凭印象列的）

`grep -rnE "@pytest\\.mark\\.parametrize\\([^)]*_[a-z_]+\\(\\)" test/` +
`grep -rn "parametrize(" test/ | grep -E "\\(\\)\\)|\\(\\),"` 两条并集，全仓命中
**5 处 / 3 文件**，逐条收录在下面。★新增一处 parametrize-from-function 必须补进本表★
——否则本闸就成了"为漏项造的兜底网与主判据共享缺口"（本仓已实证过那种失效）。
"""
from __future__ import annotations

import pytest


def test_plan_quality_bench_source_nonempty():
    """`run_all()` 必须回非空 —— 它是 `test_plan_quality_bench.py:29` 的参数源。"""
    import json
    from pathlib import Path

    from test.benchmark.plan_quality.plan_quality_bench import MANIFEST, run_all
    results = run_all()
    assert results, (
        f"run_all() 返回空 ⇒ 规划质量基准生成 0 用例、静默计绿。"
        f"检查 manifest 是否可读: {MANIFEST}")
    # 与 manifest 声明的夹具数逐一对齐：读到了但少读几个同样是静默缩水。
    # （`MANIFEST` 是 str 不是 Path —— 别假设类型，实测过才这么写。）
    manifest = json.loads(Path(MANIFEST).read_text(encoding="utf-8"))
    assert len(results) == len(manifest["fixtures"]), (
        f"run_all() 产出 {len(results)} 条，manifest 声明 "
        f"{len(manifest['fixtures'])} 个夹具 —— 有夹具被静默跳过")


@pytest.mark.parametrize("fn_name", [
    "root_aggregate_manifests",
    "build_manifest_basenames",
    "structural_manifests",
])
def test_stack_spec_parametrize_sources_nonempty(fn_name):
    """`test_b3_stack_spec_single_source.py` 的三个参数源（:91/:525/:849）。

    这三个是 STACK_SPEC 的派生视图；返回空集时那 92 个用例会塌成 0 而无人知晓。
    """
    from swarm.stacks import spec
    fn = getattr(spec, fn_name)
    got = fn()
    assert got, f"stacks.spec.{fn_name}() 返回空 ⇒ 对应 parametrize 生成 0 用例"


def test_shape_matrix_source_nonempty():
    """`test_b1_empty_diff_l2_gates.py:336/:350` 的 `range(len(_SHAPE_MATRIX))`。

    ★这条是本闸自己抓出来的★：我最初的 covered 表只有 5 条（grep 得来），AST 扫描
    报出第 6 处 `range(len(...))` —— `range` 是内置的，我第一版把它当"包装器"过滤掉了。
    但 `range(len(x))` 在 `x` 为空时**恰好**塌成 0 个用例，正是本闸要防的形态；
    它不该被过滤，该被登记。**"我 grep 出来的清单"与"机器扫出来的清单"不一致时，
    先假设机器对**（本仓已多次实证 grep 漏项：`as _rc` 别名那层就是 AST 才找全的）。
    """
    import importlib
    mod = importlib.import_module("test.test_b1_empty_diff_l2_gates")
    matrix = mod._SHAPE_MATRIX
    assert matrix, (
        "_SHAPE_MATRIX 为空 ⇒ range(len(...)) 生成 0 用例 ⇒ 那两条"
        "「逐格相等锁」静默消失（它们守的是 helper 抽取前后结论逐字相同）")


def test_zero_warning_states_source_nonempty():
    """`test_round67m2_batch3_polish.py:179` 的参数源。

    它守的是"warnings 键在**每条** return 路径上恒发"——族守卫塌成 0 用例，
    等于那 11 个恒发点全部失去覆盖（该文件注释里记着：改回条件发射时曾全绿假绿）。
    """
    import importlib
    mod = importlib.import_module("test.test_round67m2_batch3_polish")
    states = mod._zero_warning_states()
    assert states, "_zero_warning_states() 返回空 ⇒ 族守卫生成 0 用例"
    # ★不设"至少 N 条"的下界★：我第一版写了 `>= 5`，理由是那个文件的注释提到
    # "11 个恒发点"——实际语料是 **4** 条（11 个恒发点由 4 条语料覆盖，不是一一对应）。
    # 凭注释里的数字推断另一个量的基数，就是"声称穷举必须指出权威来源"的反面。
    # 本闸要守的命题只是"非空"；基数变化属被测文件自身的事，不在这里替它设门。


def test_enumeration_matches_repo_scan():
    """★元断言★：本文件收录的参数源数量 = 全仓实际的 parametrize-from-function 数。

    治的是"兜底网与主判据共享缺口"：本文件的 5 条是我 grep 出来的，但**新增一处**
    parametrize-from-function 时没人会想起补这里。这条断言让"漏登记"直接变红。

    判据用 AST 而非正则：正则数不清跨行写法与嵌套调用（本仓已实证 grep 漏掉
    `as _rc` 别名那一层，AST 才找全）。
    """
    import ast
    from pathlib import Path

    covered = {
        ("test_plan_quality_bench.py", "_params"),
        ("test_b3_stack_spec_single_source.py", "root_aggregate_manifests"),
        ("test_b3_stack_spec_single_source.py", "build_manifest_basenames"),
        ("test_b3_stack_spec_single_source.py", "structural_manifests"),
        ("test_round67m2_batch3_polish.py", "_zero_warning_states"),
        # 第 6 处：AST 扫出来的，不在我最初 grep 的清单里（见
        # test_shape_matrix_source_nonempty 的 docstring）
        ("test_b1_empty_diff_l2_gates.py", "range"),
    }

    # 内置容器/序列包装器：它们本身不是"源"，要往里再看一层。
    # ★`range` 刻意**不**在此表★：`range(len(x))` 在 x 空时正是本闸要抓的形态
    # （已实证一处），过滤掉它就等于给自己的兜底网留下与主判据重合的缺口。
    _WRAPPERS = {"sorted", "list", "tuple", "set", "reversed"}

    def _source_calls(arg: ast.expr) -> set[str]:
        """取出「**产出整个参数集合**的那个调用」的函数名。

        ★关键区分（第一版这里判错了）★：只认参数**本身**是调用（或被 sorted/list 等
        包装的调用）的形态，**不**递归进 List/Tuple 字面量里面 ——
        `parametrize("kw,expect", [({"exc": TimeoutError("slow")}, None), …])` 里的
        `TimeoutError(...)` 是**参数值**，不是数据源；它返回空集这件事根本不存在。
        第一版对整个 arg `ast.walk` 一把梭，把它误报成"没有非空地板的参数源"。
        判据：源函数的返回值决定**用例条数**；字面量里的调用只决定某一条的**取值**。
        """
        if isinstance(arg, ast.Call):
            f = arg.func
            if isinstance(f, ast.Name):
                if f.id in _WRAPPERS:
                    out: set[str] = set()
                    for a in arg.args:
                        out |= _source_calls(a)
                    return out
                return {f.id}
            if isinstance(f, ast.Attribute):
                return {f.attr}
        return set()

    found: set = set()
    unparseable: list[str] = []
    # 只扫本目录：`test/` 的子目录是 `legacy/` 与 `sandbox/`，都在 pyproject 的
    # `norecursedirs` 里（压根不被收集），且实测零 parametrize。若将来新增**被收集的**
    # 子目录，这里要改成 rglob —— 已在下面用 norecursedirs 一致性断言钉住。
    for py in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            # ★自查发现（#29-4）★ 原实现 `continue` **静默**跳过：文件一旦语法坏掉，本闸就少扫
            # 一个而毫无信号 —— "漏扫"与"扫过且没问题"不可分（血规 10④）。
            # 收集起来在下面一并断言（不当场 raise，好让一次运行报出全部坏文件）。
            unparseable.append(f"{py.name}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # 匹配 @pytest.mark.parametrize(...) 形态
            if not (isinstance(f, ast.Attribute) and f.attr == "parametrize"):
                continue
            for arg in node.args[1:]:
                for name in _source_calls(arg):
                    found.add((py.name, name))
            # ★自查发现（#29-4）★ 关键字形态也必须扫：
            # `parametrize("x", argvalues=_src())` / `parametrize(argnames=…, argvalues=…)`
            # 与位置形态**语义完全相同**，但只扫 `node.args` 会静默漏掉它。
            # 仓内当前 0 个实例——正因为如此才必须现在补：等有人第一次这么写时，
            # 本闸会**看不见**它，而它的名字（"参数源全枚举"）会让人以为已经覆盖了。
            # 这就是"为漏项造的兜底网不能与主判据共享缺口"。
            for kw in node.keywords:
                if kw.arg in (None, "argvalues", "params"):
                    for name in _source_calls(kw.value):
                        found.add((py.name, name))

    # 先断"扫全了"，再断扫的结果——顺序很重要：漏扫会让下面两条变宽松而非变红
    assert not unparseable, (
        f"这些测试文件 ast.parse 失败 ⇒ 本闸对它们**漏扫且无信号**：{unparseable}。"
        "先修语法（或若确属预期，把它排除并在此说明理由）。")
    assert found, (
        "全仓一个 parametrize-from-function 都没扫到 ⇒ 扫描逻辑坏了或目录搞错了。"
        "没有这条，`found` 为空时下面两条断言会**空集恒真**（vacuous 绿）。")

    missing = found - covered
    assert not missing, (
        f"这些 parametrize 参数源没有非空地板：{sorted(missing)}。"
        "新增 parametrize-from-function 必须同时补进本文件的 covered 表 —— "
        "空集会让那些用例静默塌成 0 个而全程无信号（血规 10④）。")
    # 反向：登记了但已不存在的，说明表过期（留着会给人虚假的覆盖感）
    stale = covered - found
    assert not stale, (
        f"covered 表里这些已不在仓内：{sorted(stale)} —— 请删除过期登记")


def test_scan_scope_matches_pytest_collection_scope():
    """★扫描面与 pytest 实际收集面同源★

    上一条只扫 `test/*.py`（不递归）。这在今天是对的：`test/` 的子目录 `legacy/`、
    `sandbox/` 都在 `norecursedirs` 里、压根不被收集。但**这个前提没有任何东西钉住** ——
    将来新增一个被收集的子目录（或把 legacy 从 norecursedirs 摘掉），元断言就会
    漏扫它而毫无信号，且它的名字仍然自称"全仓扫描"。
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    norecurse = set(cfg["tool"]["pytest"]["ini_options"]["norecursedirs"])

    subdirs = {p.name for p in (root / "test").iterdir()
               if p.is_dir() and not p.name.startswith((".", "__"))}
    collected = subdirs - norecurse - {"fixtures", "benchmark"}
    assert not collected, (
        f"`test/` 下这些子目录会被 pytest 收集，但元断言只扫顶层 `test/*.py`：{sorted(collected)}。"
        "请把上一条的 glob 改成 rglob（并复核 covered 表）。"
        "（`fixtures/` 是纯数据、`benchmark/` 的参数源已由 run_all() 那条单独守。）")

