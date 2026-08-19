"""判据 C 清扫的契约锁：路径归一【两形契约】+ 字符集 lstrip 负扫描。

32 号文批2 判据 C：`lstrip("./")` 剥的是**字符集合**而非前缀（`.mvn/x`→`mvn/x`、
`.gitattributes`→`gitattributes`），全仓 7 文件 22 处活代码已清扫。两形契约：
- 盘形 `planning_core._norm_rel`：只剥 `(\./)+` 序列——git/盘访问专用（git 路径永无
  前导 `/`，剥了它＝绝对路径被静默改成相对＝路径混淆）。
- 比较形 `planning_core._norm_rel_cmp`＝盘形再剥前导 `/`——只用于比对/集合成员/
  top 段/basename，绝不用于盘/git。
- worker 侧比较形单一事实源＝`l1_error_drivers._norm_rel`（同语义，本文件语料锁互钉）。

锁的形态（纪律：断"接线事实/单一事实源"，不断字面量实现细节）：
1. 语料真值表——每条期望值由契约语义手工推导，非从实现抄（推导记录在各注释）。
2. 两形分歧锁——`/a`、`.//a`、`//a` 三个输入两形**必须**产出不同（钉住"两形不可合并"，
   任一侧被并掉都会红）。
3. AST 负扫描——7 个已清扫文件中零个活代码 `lstrip("./")` 调用（注释/字符串不算，
   故用 ast 而非 grep；those 文件里确实留有提到该写法的注释）。
4. 接线锁——7 个文件每个都至少一处引用契约归一函数（清完旧写法却没接新事实源
   ＝半落地，本仓已发生多次）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from swarm.brain.nodes.planning_core import _norm_rel, _norm_rel_cmp
from swarm.worker.l1_error_drivers import _norm_rel as _w_norm_rel

ROOT = Path(__file__).resolve().parents[1]

# 语料真值表：(输入, 盘形期望, 比较形期望)。期望值按契约语义推导：
# 盘形＝replace("\\","/") 后剥 `^(\./)+`；比较形＝盘形再 `lstrip("/")`。
_CORPUS: list[tuple[str, str, str]] = [
    (".mvn/wrapper/x", ".mvn/wrapper/x", ".mvn/wrapper/x"),  # 段首点必须保住（bug 本体）
    ("./a/b", "a/b", "a/b"),
    ("././a", "a", "a"),                # `(\./)+` 重复序列
    (".//a", "/a", "a"),                # 盘形剥 `./` 剩 `/a`；比较形再剥 `/`
    ("/a", "/a", "a"),                  # 两形分歧样本①
    ("//a", "//a", "a"),                # 两形分歧样本②（盘形不动前导 `/`）
    ("./.github/x.yml", ".github/x.yml", ".github/x.yml"),
    ("a/b", "a/b", "a/b"),
    ("", "", ""),
    (".", ".", "."),                    # 裸 `.` 不是 `./` 前缀，两形都不动
    ("./", "", ""),
    (".build/pom.xml", ".build/pom.xml", ".build/pom.xml"),  # 实测咬人样本（F7 锁）
    (".gitattributes", ".gitattributes", ".gitattributes"),  # 字符集剥会吃掉它
    ("x/./y", "x/./y", "x/./y"),        # 中段 `./` 不剥
    ("././/z", "/z", "z"),              # `./` 序列 + `/` 混合
    ("C:\\a\\b", "C:/a/b", "C:/a/b"),   # 反斜杠归一
]

# 判据 C 清扫的 7 个文件（与 HANDOFF §判据 C 台账同一份清单）。
_SWEPT_FILES = [
    "brain/contract_utils.py",
    "brain/nodes/recovery.py",
    "brain/planning_nodes.py",
    "brain/runner.py",
    "brain/nodes/failure.py",
    "brain/nodes/adversarial.py",
    "worker/l1_pipeline.py",
]


@pytest.mark.parametrize("raw,want_disk,want_cmp", _CORPUS)
def test_disk_form_matches_contract(raw, want_disk, want_cmp):
    assert _norm_rel(raw) == want_disk


@pytest.mark.parametrize("raw,want_disk,want_cmp", _CORPUS)
def test_compare_form_matches_contract(raw, want_disk, want_cmp):
    assert _norm_rel_cmp(raw) == want_cmp


@pytest.mark.parametrize("raw,want_disk,want_cmp", _CORPUS)
def test_worker_and_brain_compare_forms_agree(raw, want_disk, want_cmp):
    """worker 侧事实源（l1_error_drivers._norm_rel）与 brain 比较形逐条相等——
    两侧任一漂移都红（互钉，防"两个都自称正确"再发生）。"""
    assert _w_norm_rel(raw) == want_cmp == _norm_rel_cmp(raw)


def test_two_forms_actually_diverge():
    """两形契约的存在性锁：前导 `/` 样本上两形必须产出不同——若有人把两形并成一个
    （或让 cmp 退化为 disk），本锁红。"""
    for raw in ("/a", ".//a", "//a"):
        assert _norm_rel(raw) != _norm_rel_cmp(raw)


def _lstrip_dotslash_calls(path: Path) -> list[int]:
    """活代码中 `*.lstrip("./")` 调用的行号（ast 层，注释/字符串天然排除）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[int] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "lstrip"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "./"):
            out.append(node.lineno)
    return out


@pytest.mark.parametrize("rel", _SWEPT_FILES)
def test_no_live_charset_lstrip_in_swept_files(rel):
    hits = _lstrip_dotslash_calls(ROOT / rel)
    assert hits == [], f"{rel} 仍有活代码 lstrip(\"./\") 字符集剥（行 {hits}）——判据 C 回潮"


@pytest.mark.parametrize("rel", _SWEPT_FILES)
def test_swept_files_reference_contract_normalizer(rel):
    """接线锁：清扫不是删完旧写法就完——每个文件必须真的接上契约归一函数
    （`_norm_rel` 或 `_norm_rel_cmp`）。删掉契约函数定义会让本锁全红。"""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    names = {
        (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (isinstance(n.func, ast.Name) or isinstance(n.func, ast.Attribute))
    }
    imported = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for a in n.names
    }
    wired = {"_norm_rel", "_norm_rel_cmp"} & (names | imported)
    assert wired, f"{rel} 未接线任何契约归一函数（半落地）"
