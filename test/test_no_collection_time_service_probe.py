"""#29-4 T-7 根因闸：`test/` 里**不得**在 collection 期探测外部服务。

## 为什么必须有这条（它是 H-1 的治法，不是 H-1 的修补）

T-7 第一轮我用 `grep _pg_available|_pg_up` 找落点，转了 9 个文件。
复核用别的路子又找出 **4 个文件 / 39 个用例** 同病未治：

| 文件 | 形态 | 为什么 grep 漏了 |
|---|---|---|
| `test_a2_command_blacklist.py` | `skipif(not _has_pg(), …)` | 函数名叫 `_has_pg` |
| `test_a2_sandbox_rbac.py` | 同上 | 同上 |
| `test_p0_10_multiuser_isolation…py` | `_PG_OK = _pg_available()` 顶层赋值 | 结果**先存变量**再用，字面量搜不到 |
| `test_p1_11_api_delivery_txn_stream…py` | 同上 | 同上 |

**按"某一种字面写法"数调用点必然漏**（血规 10①）。所以治法不能是"再 grep 一遍、把这
4 个也改了"——那样下一个 `_has_db()` / `_PG_UP = probe()` 还会漏，而且照旧无信号。
本文件用 **AST** 断"模块顶层求值面不得有连服务的调用"。

## 判据（刻意的取舍）

- **只管模块顶层求值面**，含复合语句（If/Try/With/For/While）的 body ——
  它们在 import 期同样执行。函数体内、fixture 内、测试体内的连库**全部合法**，
  那正是治法本身（`needs_service` 标记 + autouse fixture 建表）。
- `if __name__ == "__main__":` 块**排除**：pytest import 时不执行（实测全仓 25 处
  命中全在这种块里，不排除会误报一大批、逼人改掉合法的直跑入口）。
- 判"连服务"看符号名含 `connect` / `ping` / `from_url` / `ensure_*tables`，
  以及碰服务的本地函数的**传递闭包**（含 class 体内方法）。
- 扫描面 = `test/test_*.py` + **`conftest.py`**（见 `_scan_targets`）。

## 已知边界（★覆盖面就是这么大，不多不少★）

第一版这段只写了一条边界，并在上面自称把这一族「**整类**关掉」。复核 N-2 实测证伪：
当时有 **5 种形态**直接逃逸（顶层 try/if/with 体内赋值、一层间接、探针在 class 里）。
现已全部补上并进了元测试 shapes 表。留下的真实边界：

1. **跨模块间接调用**查不到（需跨文件符号解析）。若有人把探针搬进 `test/helpers.py`
   再在顶层调它，本闸看不见。
2. **符号名启发式**：判"连服务"靠名字含 `connect`/`ping`/`from_url`/`ensure_*tables`。
   自造名字（`def warm_up(): psycopg.Connection.connect(...)` 之外的奇异写法）可能漏；
   反向，名字含 `connect` 但其实不连服务的会误报（白名单 `_ALLOWED_TOP_CALLS` 兜）。
3. **动态调用**（`globals()["_has_pg"]()`、`getattr(mod, name)()`）静态判不了。

★写这段的纪律★：「N 处/整类」这种量词必须由**实测的覆盖面**定义，不能由我当时想到几种
定义 —— 否则闸的名字会给出比它实际覆盖更强的保证（"声称穷举必须指出权威来源"）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_TESTDIR = Path(__file__).resolve().parent

# 连服务的动作特征（符号名子串）
_SERVICE_CALL_HINTS = ("connect", "ping", "from_url")


def _is_service_symbol(name: str) -> bool:
    low = name.lower()
    if any(h in low for h in _SERVICE_CALL_HINTS):
        return True
    # ensure_tables / ensure_auth_tables / ensure_xxx_tables
    return low.startswith("ensure_") and low.endswith("tables")


# 顶层允许出现的调用（纯配置/纯数据，不碰网络与库）
_ALLOWED_TOP_CALLS = {
    "Path", "dirname", "abspath", "resolve", "parent", "getenv", "environ",
    "get", "setdefault", "join", "split", "compile", "frozenset", "set",
    "dict", "list", "tuple", "sorted", "len", "range", "str", "int", "float",
    "mark", "skipif", "parametrize", "fixture", "importorskip",
    "spec_from_file_location", "module_from_spec", "exec_module", "read_text",
    "loads", "dumps", "glob", "rglob", "exists", "mkdir", "getLogger",
    "needs_service", "usefixtures", "filterwarnings", "asyncio",
}


def _called_names(node: ast.AST) -> set[str]:
    """节点里出现的全部被调用符号名（`a.b.c()` 取 `c`）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def _local_service_functions(tree: ast.Module) -> set[str]:
    """碰服务的本地函数名 —— **传递闭包**，且含 class 体内的方法。

    ★复核 N-2 整改★ 第一版只收「顶层 `FunctionDef` 且**直接**含服务调用」，于是两种
    形态逃逸：
      · 一层间接：`def _a(): psycopg.connect(...)` / `def _b(): return _a()` → 顶层调 `_b()`
      · 探针写成 class 的 staticmethod
    现在：先收直接碰服务的（不限顶层，`ast.walk` 全扫），再迭代求闭包到不动点。
    """
    direct: set[str] = set()
    calls: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = _called_names(node)
            calls[node.name] = names
            if any(_is_service_symbol(n) for n in names):
                direct.add(node.name)
    # 传递闭包：调用了"碰服务的函数"的函数，自己也算碰服务
    changed = True
    while changed:
        changed = False
        for fn, names in calls.items():
            if fn not in direct and (names & direct):
                direct.add(fn)
                changed = True
    return direct


def _violations_in(py: Path) -> list[str]:
    tree = ast.parse(py.read_text(encoding="utf-8"))
    suspect = _local_service_functions(tree)
    bad: list[str] = []

    def _check(value: ast.AST, where: str) -> None:
        for name in _called_names(value):
            if name in _ALLOWED_TOP_CALLS:
                continue
            if _is_service_symbol(name):
                bad.append(f"{py.name}:{getattr(value, 'lineno', '?')} "
                           f"{where} 直接连服务：{name}()")
            elif name in suspect:
                bad.append(f"{py.name}:{getattr(value, 'lineno', '?')} "
                           f"{where} 调了内部碰服务的本地函数：{name}()")

    def _walk_top(body: list[ast.stmt], *, in_main: bool) -> None:
        """递归下钻**顶层求值面**：复合语句（If/Try/With/For/While）的 body 同样在
        import 期执行，必须进去看。遇 FunctionDef/ClassDef 停（那些体不在 import 期求值，
        但**装饰器实参**要看）。

        ★复核 N-2 整改★ 第一版只遍历 `tree.body` 的 Assign/Expr/AnnAssign，于是
        `try: _PG = _has_pg() except: _PG = False` 这种**在测试文件里极常见**的写法
        直接逃逸（模块级 try-import 到处都是）。

        `in_main`：`if __name__ == "__main__":` 块内的语句在 pytest import 时**不执行**，
        故不是 collection 期探测 —— 必须排除，否则会误报一大批（实测全仓 25 处命中
        **全部**在 `__main__` 块内）。
        """
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    _check(dec, "装饰器实参")
                continue
            if isinstance(node, ast.If):
                test_src = ast.unparse(node.test)
                sub_main = in_main or ("__main__" in test_src)
                _walk_top(node.body, in_main=sub_main)
                _walk_top(node.orelse, in_main=sub_main)
                continue
            if isinstance(node, (ast.Try, ast.With, ast.AsyncWith, ast.For,
                                 ast.AsyncFor, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    _walk_top(getattr(node, attr, []) or [], in_main=in_main)
                for handler in getattr(node, "handlers", []) or []:
                    _walk_top(handler.body, in_main=in_main)
                continue
            if in_main:
                continue
            if isinstance(node, ast.Assign):
                _check(node.value, "模块顶层赋值")
            elif isinstance(node, ast.Expr):
                _check(node.value, "模块顶层表达式")
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                _check(node.value, "模块顶层带注解赋值")

    _walk_top(tree.body, in_main=False)
    return bad


def _scan_targets() -> list[Path]:
    """扫描面 = `test/test_*.py` + **`conftest.py`**。

    ★复核 N-1 整改★ 第一版只 glob `test_*.py`，把 `conftest.py` 漏在外面 ——
    而它是**影响面最大**的落点：在所有测试之前 import，一个顶层探测会让全套 7000+ 用例
    在 collection 期就定生死；且它**恰恰是服务探测机制的所在地**（`_probe_pg`/`_probe_redis`
    就定义在那里），下一个人要加"顺手先探一下"最可能加在这里。
    判据本身认得这个形态（实测 True），缺的是**文件选择** —— 与"加机制先数调用点，
    且调用点有两层"同型：判据对了，喂给它的集合少了一个成员。
    """
    out = sorted(_TESTDIR.glob("test_*.py"))
    for cf in (_TESTDIR / "conftest.py", _TESTDIR.parent / "conftest.py"):
        if cf.exists():
            out.append(cf)
    return out


def test_scan_scope_matches_pytest_collection_scope():
    """★与孪生闸共用的 `norecursedirs` 一致性锁（复核 N-3）★

    本闸用**非递归** glob。这在今天是对的：`test/` 的子目录 `legacy/`、`sandbox/` 都在
    `norecursedirs` 里、压根不被收集。但这个前提原先**没有任何东西钉住**。
    孪生闸 `test_parametrize_sources_nonempty.py` 早就为同一个前提装了锁，本闸没有 ——
    **同一份诊断落在两个患者身上只治了一个**，而"另一个"的存在感恰恰因为第一个治好了
    而降低（本批第三次出现这个形状）。
    """
    import tomllib

    root = _TESTDIR.parent
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    norecurse = set(cfg["tool"]["pytest"]["ini_options"]["norecursedirs"])
    subdirs = {p.name for p in _TESTDIR.iterdir()
               if p.is_dir() and not p.name.startswith((".", "__"))}
    collected = subdirs - norecurse - {"fixtures", "benchmark"}
    assert not collected, (
        f"`test/` 下这些子目录会被 pytest 收集，但本闸只扫顶层：{sorted(collected)}。"
        "请把 `_scan_targets` 的 glob 改成 rglob。"
        "（`fixtures/` 是纯数据、`benchmark/` 无服务探测。）")


def test_no_module_level_service_probe():
    """★根因闸★ 全仓 `test/test_*.py` + `conftest.py` 顶层不得探测 PG/Redis/Qdrant。

    违反的后果（T-7 立论）：服务抖一下 → 整个文件的用例在 collection 期静默 skip，
    `SWARM_TEST_REQUIRE_SERVICES=1` 对它们**完全无效**，CI EXIT 0。
    """
    files = _scan_targets()
    assert files, "一个测试文件都没扫到 ⇒ 扫描面坏了（否则下面空集恒真）"
    assert any(p.name == "conftest.py" for p in files), (
        "扫描面里没有 conftest.py —— 它是影响面最大的落点（见 _scan_targets docstring）")
    bad: list[str] = []
    for py in files:
        bad.extend(_violations_in(py))
    assert not bad, (
        "以下位置在 **collection 期**探测外部服务：\n  "
        + "\n  ".join(bad)
        + "\n\n治法：改用 `pytestmark = pytest.mark.needs_service(\"pg\")`（判定推迟到 "
          "runtest setup），副作用（建表等）放 autouse fixture 且**不吞异常**。"
          "\n实现见 test/conftest.py::pytest_runtest_setup。")


def test_this_gate_catches_the_four_known_shapes():
    """★元测试：证明本闸真能抓住那 4 个已知形态★

    没有这条，`_ALLOWED_TOP_CALLS` 白名单放宽到把真病灶也放过时，上一条会静默变绿——
    而它是本批唯一防"下一个 `_has_db()` 再漏"的东西（血规 10②：把机制改坏，测试会不会红）。
    在内存里构造四种形态 + 三种**合法**写法，一并验判据方向。
    """
    import tempfile

    shapes = {
        # 病灶四形态（必须被抓）
        "A_has_pg_skipif": (
            "import pytest\n"
            "def _has_pg():\n"
            "    from x import bl\n"
            "    bl.ensure_tables()\n"
            "    return True\n"
            'pytestmark = pytest.mark.skipif(not _has_pg(), reason="no pg")\n', True),
        "B_pg_available_assign": (
            "import psycopg\n"
            "def _pg_available():\n"
            "    with psycopg.connect('x'):\n"
            "        return True\n"
            "_PG_OK = _pg_available()\n", True),
        "C_direct_connect_toplevel": (
            "import psycopg\n"
            "_conn = psycopg.connect('dsn')\n", True),
        "D_decorator_arg": (
            "import pytest\n"
            "def _db_up():\n"
            "    import redis\n"
            "    redis.from_url('x').ping()\n"
            "    return True\n"
            "@pytest.mark.skipif(not _db_up(), reason='x')\n"
            "def test_a():\n"
            "    pass\n", True),
        # ★复核 N-2 补入的五种逃逸形态★（第一版全部逃逸，实测证伪了"整类关掉"的说法）
        # 前三种是**复合语句 body 在 import 期同样执行**，而第一版只遍历 tree.body 的
        # Assign/Expr/AnnAssign；`try:` 那种在测试文件里极常见（模块级 try-import 到处都是）。
        "H_toplevel_try": (
            "import psycopg\n"
            "def _has_pg():\n"
            "    psycopg.connect('x')\n"
            "    return True\n"
            "try:\n"
            "    _PG = _has_pg()\n"
            "except Exception:\n"
            "    _PG = False\n", True),
        "I_toplevel_if": (
            "import os, psycopg\n"
            "def _has_pg():\n"
            "    psycopg.connect('x')\n"
            "    return True\n"
            "if os.environ.get('X'):\n"
            "    _PG = _has_pg()\n", True),
        "J_toplevel_with": (
            "import contextlib, psycopg\n"
            "def _has_pg():\n"
            "    psycopg.connect('x')\n"
            "    return True\n"
            "with contextlib.suppress(Exception):\n"
            "    _PG = _has_pg()\n", True),
        # 后两种要求 suspect 求**传递闭包** + 收 class 体
        "K_one_indirection": (
            "import psycopg\n"
            "def _a():\n"
            "    psycopg.connect('x')\n"
            "def _b():\n"
            "    return _a()\n"
            "_PG = _b()\n", True),
        "L_probe_in_class": (
            "import psycopg\n"
            "class P:\n"
            "    @staticmethod\n"
            "    def probe():\n"
            "        psycopg.connect('x')\n"
            "_PG = P.probe()\n", True),
        # 合法形态（不得误报）
        "E_marker_only": (
            "import pytest\n"
            'pytestmark = pytest.mark.needs_service("pg")\n', False),
        "F_connect_inside_fixture": (
            "import psycopg\n"
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def _ready():\n"
            "    psycopg.connect('dsn')\n", False),
        "G_connect_inside_test": (
            "import psycopg\n"
            "def test_x():\n"
            "    with psycopg.connect('dsn'):\n"
            "        pass\n", False),
        # ★`__main__` 块必须**不**报★：pytest import 时 `__name__ != "__main__"`，
        # body 不执行 ⇒ 不是 collection 期探测。实测全仓 25 处命中**全部**在这种块里，
        # 少了这条排除就会误报一大批，逼人把合法的直跑入口改掉。
        "M_main_block_is_legal": (
            "import psycopg\n"
            "def _has_pg():\n"
            "    psycopg.connect('x')\n"
            "    return True\n"
            "if __name__ == '__main__':\n"
            "    if _has_pg():\n"
            "        print('ok')\n", False),
    }

    with tempfile.TemporaryDirectory() as td:
        for tag, (src, should_flag) in shapes.items():
            p = Path(td) / f"test_{tag}.py"
            p.write_text(src, encoding="utf-8")
            got = _violations_in(p)
            if should_flag:
                assert got, (
                    f"形态 {tag} 是已知病灶却**没被抓到** ⇒ 本闸对它零覆盖，"
                    f"H-1 那 4 个文件的同类还会再漏一次")
            else:
                assert not got, (
                    f"形态 {tag} 是**合法**写法却被误报：{got}。"
                    f"误报会逼人把正确的治法（fixture 内连库）改回去")
